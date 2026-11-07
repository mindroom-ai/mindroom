"""MindRoom Chat's routes for call transcripts: join, leave, Pause, speech, the live call and its event stream.

Every route answers 404 while call transcripts are off, and then needs a client session. The service decides the rest;
each refusal answers with its status, its sentence and its code.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, ValidationError
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from mindroom.api.app_state import app_state
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.call_transcripts.service import (
    MAX_SPEECH_BYTES,
    CallEnded,
    CallRefusedError,
    CallUpdated,
    LineAdded,
    iso_time,
)
from mindroom.plugin_http import require_client_user

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Mapping

    from starlette.types import Receive, Scope, Send

    from mindroom.call_transcripts.service import Call, CallEvent, CallTranscriptService, LiveCall, RefusalCode
    from mindroom.call_transcripts.store import StoredLine

_STATUS: Mapping[RefusalCode, int] = {
    "invalid": 400,
    "forbidden": 403,
    "not-found": 404,
    "paused": 409,
    "too-large": 413,
    "too-many-requests": 429,
    "unavailable": 503,
}
# Room for the form's boundaries, its parts' headers and `startedAt` around the audio.
_MAX_SPEECH_BODY_BYTES = MAX_SPEECH_BYTES + 16 * 1024
_MAX_PAUSE_BODY_BYTES = 1024
# A quiet stream writes a comment this often, and checks each time that its reader may still read it.
_KEEPALIVE_SECONDS = 20.0
_STREAMS_PER_USER = 10
_STREAMS_RETRY_AFTER_SECONDS = 20
# How many changes wait for a reader that stopped reading; past that its stream ends and its app starts a fresh one.
_STREAM_BACKLOG = 1000


class _CallRoute(APIRoute):
    """A route whose refusals answer with their status, their sentence and their code."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def answer(request: Request) -> Response:
            try:
                return await handler(request)
            except CallRefusedError as refusal:
                return _refusal_response(refusal)

        return answer


router = APIRouter(prefix="/api/call-transcripts/v1", route_class=_CallRoute)


@dataclass(frozen=True, slots=True)
class _Caller:
    """The service while call transcripts are on, and the signed-in person asking it."""

    service: CallTranscriptService
    user_id: str


async def _caller(request: Request) -> _Caller:
    """Answer 404 while call transcripts are off, and then 401 without a client session."""
    service = app_state(request.app).runtime.call_transcripts()
    if service is None:
        raise CallRefusedError(code="not-found", message="Call transcripts are off on this MindRoom.")
    user = await require_client_user(request)
    return _Caller(service, user.user_id)


_SignedIn = Annotated[_Caller, Depends(_caller)]


class _PauseRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    holding: bool


@router.post("/rooms/{room_id}/calls.join")
async def join_call(room_id: str, caller: _SignedIn) -> dict[str, object]:
    """Join the room's live call, starting it if there is none; from someone already in it, this is the heartbeat."""
    return {"call": _call_json(await caller.service.join(room_id, caller.user_id))}


@router.post("/calls/{call_id}/leave")
async def leave_call(call_id: str, caller: _SignedIn) -> dict[str, object]:
    """Take the person's app out of the call."""
    await caller.service.leave(call_id, caller.user_id)
    return {}


@router.post("/calls/{call_id}/pause")
async def pause_call(call_id: str, request: Request, caller: _SignedIn) -> dict[str, object]:
    """Take (``{"holding": true}``) or release (``{"holding": false}``) the person's hold on Pause."""
    body = await _read_body(request, max_bytes=_MAX_PAUSE_BODY_BYTES)
    try:
        holding = _PauseRequest.model_validate_json(body).holding
    except ValidationError:
        raise CallRefusedError(code="invalid", message='Send {"holding": true} or {"holding": false}.') from None
    return {"call": _call_json(await caller.service.pause(call_id, caller.user_id, holding=holding))}


@router.post("/calls/{call_id}/speech")
async def upload_speech(call_id: str, request: Request, caller: _SignedIn) -> dict[str, object]:
    """Transcribe one stretch of the person's speech: a form with the WAV as ``audio`` and when it began as ``startedAt``.

    The form is read into memory only, refused as too large from its declared length before it is read, and as soon
    as it passes the limit while it is read.
    """
    audio, started_at = await _speech_form(request)
    line = await caller.service.speech(call_id, caller.user_id, audio=audio, started_at=started_at)
    return {"line": None if line is None else _line_json(line)}


@router.get("/rooms/{room_id}/calls.live")
async def live_call(room_id: str, caller: _SignedIn) -> dict[str, object]:
    """Return the room's live call, its lines so far and the names of everyone in either."""
    return _live_json(await caller.service.live(room_id, caller.user_id))


@router.get("/rooms/{room_id}/calls.events")
async def call_events(room_id: str, request: Request, caller: _SignedIn) -> Response:
    """Stream the room's changes as server-sent events, first ``ready`` with what ``calls.live`` answers."""
    return _EventStream(request, caller, room_id)


class _EventStream(Response):
    """One reader's stream of a room's changes, until they hang up, may no longer read it, or MindRoom stops."""

    def __init__(self, request: Request, caller: _Caller, room_id: str) -> None:
        super().__init__()
        self._request = request
        self._service = caller.service
        self._user_id = caller.user_id
        self._room_id = room_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse a reader at their cap of open streams, or whom the live call refuses; otherwise stream until the end."""
        state = app_state(self._request.app)
        open_streams = state.call_event_streams
        if open_streams.get(self._user_id, 0) >= _STREAMS_PER_USER:
            refusal = CallRefusedError(
                code="too-many-requests",
                message=f"You already have {_STREAMS_PER_USER} call event streams open.",
                retry_after=_STREAMS_RETRY_AFTER_SECONDS,
            )
            await _refusal_response(refusal)(scope, receive, send)
            return
        open_streams[self._user_id] = open_streams.get(self._user_id, 0) + 1
        pending = _Pending()
        stopping = None if state.shutdown_requested is None else asyncio.ensure_future(state.shutdown_requested.wait())
        if stopping is not None:
            stopping.add_done_callback(lambda _: pending.ending.set())
        try:
            # Listening starts before the call is read, so no change falls between the two.
            with self._service.listen(self._room_id, pending.add):
                try:
                    live = await self._service.live(self._room_id, self._user_id)
                except CallRefusedError as refusal:
                    await _refusal_response(refusal)(scope, receive, send)
                    return
                stream = StreamingResponse(
                    self._events(live, pending),
                    media_type="text/event-stream",
                    headers={"X-Accel-Buffering": "no"},
                )
                await stream(scope, receive, send)
        finally:
            if stopping is not None:
                stopping.cancel()
            open_streams[self._user_id] -= 1
            if not open_streams[self._user_id]:
                del open_streams[self._user_id]

    async def _events(self, live: LiveCall, pending: _Pending) -> AsyncIterator[str]:
        """Yield ``ready``, then each change, and every 20 seconds, however busy the room, a check and a comment."""
        yield _server_sent_event("ready", _live_json(live))
        loop = asyncio.get_running_loop()
        check_at = loop.time() + _KEEPALIVE_SECONDS
        while not pending.ending.is_set():
            event = await pending.next(check_at - loop.time())
            if pending.ending.is_set():
                return
            if event is not None:
                yield _server_sent_event(*_event_json(event))
            if loop.time() >= check_at:
                if not await self._may_still_read():
                    return
                yield ": still here\n\n"
                check_at = loop.time() + _KEEPALIVE_SECONDS

    async def _may_still_read(self) -> bool:
        """Return whether the reader is still signed in and in the room, and call transcripts still on.

        A room that cannot be read right now keeps the stream open; only a reader known to have left ends it.
        """
        embedded = app_state(self._request.app).embedded
        if embedded is None or embedded.call_transcripts() is not self._service:
            return False
        try:
            await require_client_user(self._request)
            await self._service.require_member(self._room_id, self._user_id)
        except HTTPException:
            return False
        except CallRefusedError as refusal:
            return refusal.code != "not-found"
        return True


@dataclass(eq=False)
class _Pending:
    """The changes waiting to be written to one stream, and whether it must end."""

    events: asyncio.Queue[CallEvent] = field(default_factory=lambda: asyncio.Queue(_STREAM_BACKLOG))
    ending: asyncio.Event = field(default_factory=asyncio.Event)

    def add(self, event: CallEvent) -> None:
        """Queue a change; a stream already that far behind ends instead."""
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            self.ending.set()

    async def next(self, seconds: float) -> CallEvent | None:
        """Return the next change, None if none came within ``seconds`` or the stream must end.

        Waiting ends without cancelling a change already taken from the queue, so none is lost.
        """
        change = asyncio.ensure_future(self.events.get())
        ending = asyncio.ensure_future(self.ending.wait())
        try:
            await asyncio.wait((change, ending), timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            change.cancel()
            ending.cancel()
        return change.result() if change.done() and not change.cancelled() else None


def _refusal_response(refusal: CallRefusedError) -> JSONResponse:
    headers = {} if refusal.retry_after is None else {"Retry-After": str(refusal.retry_after)}
    return JSONResponse(
        {"error": refusal.message, "code": refusal.code},
        status_code=_STATUS[refusal.code],
        headers=headers,
    )


async def _read_body(request: Request, *, max_bytes: int) -> bytes:
    """Return the request body, read into memory, refusing one larger than ``max_bytes`` before or while reading it."""
    declared = request.headers.get("content-length", "")
    too_large = CallRefusedError(code="too-large", message=f"The request is larger than {max_bytes:,} bytes.")
    if declared.isdigit() and int(declared) > max_bytes:
        raise too_large
    try:
        return await collect_bounded_bytes(request.stream(), max_bytes=max_bytes)
    except ByteLimitExceededError:
        raise too_large from None


async def _speech_form(request: Request) -> tuple[bytes, float]:
    """Return a stretch's audio and when it began, in seconds since the epoch, from its multipart form."""
    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        raise CallRefusedError(code="invalid", message="Send speech as multipart/form-data.")
    fields = _form_fields(await _read_body(request, max_bytes=_MAX_SPEECH_BODY_BYTES), boundary)
    audio = fields.get("audio")
    started_at = fields.get("startedAt")
    if audio is None or started_at is None:
        raise CallRefusedError(code="invalid", message="Send the speech as `audio` and when it began as `startedAt`.")
    return audio, _seconds(started_at)


def _form_fields(body: bytes, boundary: bytes) -> dict[str, bytes]:
    """Return each part of a multipart form by its name, the first of each name, parsed in memory."""
    fields: dict[str, bytes] = {}
    header_name = bytearray()
    header_value = bytearray()
    data = bytearray()
    name: str | None = None

    def begin_part() -> None:
        nonlocal name
        name = None
        data.clear()

    def end_header() -> None:
        nonlocal name
        if bytes(header_name).strip().lower() == b"content-disposition":
            name = parse_options_header(bytes(header_value))[1].get(b"name", b"").decode("utf-8", "replace")
        header_name.clear()
        header_value.clear()

    def end_part() -> None:
        if name is not None:
            fields.setdefault(name, bytes(data))

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": begin_part,
            "on_header_field": lambda chunk, start, end: header_name.extend(chunk[start:end]),
            "on_header_value": lambda chunk, start, end: header_value.extend(chunk[start:end]),
            "on_header_end": end_header,
            "on_part_data": lambda chunk, start, end: data.extend(chunk[start:end]),
            "on_part_end": end_part,
        },
    )
    try:
        parser.write(body)
        parser.finalize()
    except MultipartParseError:
        raise CallRefusedError(code="invalid", message="The speech's form could not be read.") from None
    return fields


def _seconds(value: bytes) -> float:
    """Return an ISO 8601 time that names its time zone as seconds since the epoch."""
    try:
        moment = datetime.fromisoformat(value.decode())
    except (UnicodeDecodeError, ValueError):
        raise CallRefusedError(code="invalid", message="`startedAt` must be an ISO 8601 time.") from None
    if moment.tzinfo is None:
        raise CallRefusedError(code="invalid", message="`startedAt` must name its time zone, such as Z for UTC.")
    return moment.timestamp()


def _server_sent_event(name: str, data: object) -> str:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _event_json(event: CallEvent) -> tuple[str, dict[str, object]]:
    match event:
        case CallUpdated(call=call):
            return "call", _call_json(call)
        case LineAdded(line=line):
            return "line", _line_json(line)
        case CallEnded(call_id=call_id):
            return "ended", {"callId": call_id}


def _call_json(call: Call) -> dict[str, object]:
    return {
        "id": call.id,
        "roomId": call.room_id,
        "rowEventId": call.row_event_id,
        "startedAt": iso_time(call.started_at),
        "endedAt": None if call.ended_at is None else iso_time(call.ended_at),
        "members": list(call.members),
        "holds": list(call.holds),
        "transcribedBy": list(call.transcribed_by),
    }


def _line_json(line: StoredLine) -> dict[str, object]:
    said = {
        "id": line.id,
        "callId": line.call_id,
        "userId": line.user_id,
        "saidAt": iso_time(line.said_at),
        "seconds": line.seconds,
        "kind": line.kind,
    }
    return {**said, "text": line.text} if line.kind == "speech" else {**said, "reason": line.reason}


def _live_json(live: LiveCall) -> dict[str, object]:
    return {
        "call": None if live.call is None else _call_json(live.call),
        "lines": [_line_json(line) for line in live.lines],
        "names": dict(live.names),
    }
