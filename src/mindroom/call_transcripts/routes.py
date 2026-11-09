"""The HTTP routes MindRoom Chat uses for call transcripts: the room's call, leaving, Pause and speech.

While call transcripts are off every route answers 404; otherwise each needs a client session. The service makes
every other decision, and a refusal answers with its HTTP status, a sentence and a code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, ValidationError
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from mindroom.api.app_state import app_state
from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.call_transcripts.service import MAX_SPEECH_BYTES, CallRefusedError, iso_time
from mindroom.plugin_http import require_client_user

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from mindroom.call_transcripts.service import Call, CallTranscriptService, LiveCall, RefusalCode
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

    paused: bool


@router.post("/rooms/{room_id}/call")
async def join_call(room_id: str, caller: _SignedIn) -> dict[str, object]:
    """Put the person in the room's live call, starting one if needed; sent again while in it, it is the heartbeat."""
    return {"call": _call_json(await caller.service.join(room_id, caller.user_id))}


@router.post("/calls/{call_id}/leave")
async def leave_call(call_id: str, caller: _SignedIn) -> dict[str, object]:
    """Take the person's app out of the call; the last one out ends it."""
    await caller.service.leave(call_id, caller.user_id)
    return {}


@router.post("/calls/{call_id}/pause")
async def pause_call(call_id: str, request: Request, caller: _SignedIn) -> dict[str, object]:
    """Pause (``{"paused": true}``) or resume (``{"paused": false}``) transcription for everyone in the call."""
    body = await _read_body(request, max_bytes=_MAX_PAUSE_BODY_BYTES)
    try:
        paused = _PauseRequest.model_validate_json(body).paused
    except ValidationError:
        raise CallRefusedError(code="invalid", message='Send {"paused": true} or {"paused": false}.') from None
    return {"call": _call_json(await caller.service.pause(call_id, caller.user_id, paused=paused))}


@router.post("/calls/{call_id}/speech")
async def upload_speech(call_id: str, request: Request, caller: _SignedIn) -> dict[str, object]:
    """Transcribe one stretch of the person's speech: a form with the WAV as ``audio`` and when it began as ``startedAt``.

    Who sends it is checked, and their one upload slot taken, before the form is read. The form is read into memory
    only, refused as too large from its declared length before it is read, and as soon as it passes the limit while it
    is read.
    """
    taken_at = await caller.service.reserve_upload(call_id, caller.user_id)
    try:
        audio, started_at = await _speech_form(request)
        line = await caller.service.speech(call_id, caller.user_id, audio=audio, started_at=started_at)
    finally:
        caller.service.release_upload(caller.user_id, taken_at)
    return {"line": None if line is None else _line_json(line)}


@router.get("/rooms/{room_id}/call")
async def live_call(room_id: str, caller: _SignedIn, after: str | None = None) -> dict[str, object]:
    """Return the room's live call with its lines, only those after the line ``after`` when given, and names."""
    return _live_json(await caller.service.live(room_id, caller.user_id, after=after))


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


def _call_json(call: Call) -> dict[str, object]:
    return {
        "id": call.id,
        "roomId": call.room_id,
        "rowEventId": call.row_event_id,
        "startedAt": iso_time(call.started_at),
        "endedAt": None if call.ended_at is None else iso_time(call.ended_at),
        "members": list(call.members),
        "pausedBy": call.paused_by,
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
