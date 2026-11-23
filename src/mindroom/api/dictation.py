"""HTTP routes for streaming composer dictation over ``voice.stt``."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import OrderedDict, deque
from typing import TYPE_CHECKING, Any, Literal

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api import config_lifecycle
from mindroom.api.app_state import app_state, ensure_app_state
from mindroom.api.auth import is_administrator
from mindroom.api.bounded_form import InvalidMultipartFormError, RequestBodyTooLargeError, parse_multipart_form
from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.entity_resolution import prepared_entity_names_by_user_id
from mindroom.helper_usage import record_system_usage
from mindroom.logging_config import get_logger
from mindroom.plugin_http import require_client_user
from mindroom.requester_identity import is_access_checked_requester_id, resolve_human_requester_alias
from mindroom.voice.handler import resolve_stt_connection, transcribe_speech
from mindroom.voice.wav import WavError, wav_seconds

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence

    from fastapi import FastAPI

    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_MAX_SEGMENT_SECONDS = 30
_MAX_SEGMENT_BYTES = 1_000_000
_MAX_FORM_BODY_BYTES = 1_016_384
_EXPECTED_SAMPLE_RATE = 16_000
_MAX_PROMPT_BYTES = 1000
_MAX_LANGUAGE_CHARS = 16
_LANGUAGE_RE = re.compile(r"^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$")
_BODY_READ_TIMEOUT_SECONDS = 15.0
_TRANSCRIBE_TIMEOUT_SECONDS = 25.0
_ACCESS_TIMEOUT_SECONDS = 20.0
_MAX_IN_FLIGHT_PER_USER = 2
_MAX_IN_FLIGHT_TOTAL = 8
_WINDOW_SECONDS = 60.0
_MAX_REQUESTS_PER_MINUTE = 30
_MAX_AUDIO_SECONDS_PER_MINUTE = 180.0
_CONCURRENCY_RETRY_AFTER_SECONDS = 2

_DICTATION_OFF = "Dictation is not available on this MindRoom."
_FORBIDDEN = "You are not authorized to use dictation on this MindRoom."
_MEMBERSHIP_UNAVAILABLE = "Room membership is currently unavailable."

type _RefusalCode = Literal["forbidden", "not-found", "invalid", "too-many-requests", "unavailable", "too-large"]
_STATUS: Mapping[_RefusalCode, int] = {
    "invalid": 400,
    "forbidden": 403,
    "not-found": 404,
    "too-large": 413,
    "too-many-requests": 429,
    "unavailable": 503,
}


class _DictationRefusedError(Exception):
    """Refusal mapped to the standard dictation error JSON envelope."""

    def __init__(self, *, code: _RefusalCode, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code: _RefusalCode = code
        self.message = message
        self.retry_after = retry_after


class DictationLimiter:
    """In-flight concurrency and sliding-window rate limits for dictation uploads."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_in_flight_per_user: int = _MAX_IN_FLIGHT_PER_USER,
        max_in_flight_total: int = _MAX_IN_FLIGHT_TOTAL,
        window_seconds: float = _WINDOW_SECONDS,
        max_requests_per_minute: int = _MAX_REQUESTS_PER_MINUTE,
        max_audio_seconds_per_minute: float = _MAX_AUDIO_SECONDS_PER_MINUTE,
    ) -> None:
        self._clock = clock
        self.max_in_flight_per_user = max_in_flight_per_user
        self.max_in_flight_total = max_in_flight_total
        self.window_seconds = window_seconds
        self.max_requests_per_minute = max_requests_per_minute
        self.max_audio_seconds_per_minute = max_audio_seconds_per_minute
        self._in_flight_total = 0
        self._in_flight_by_user: dict[str, int] = {}
        self._history: OrderedDict[str, deque[tuple[float, float]]] = OrderedDict()

    def _acquire_slot(self, user_id: str) -> None:
        user_in_flight = self._in_flight_by_user.get(user_id, 0)
        if user_in_flight >= self.max_in_flight_per_user:
            raise _DictationRefusedError(
                code="too-many-requests",
                message="Too many dictation requests in progress; try again shortly.",
                retry_after=_CONCURRENCY_RETRY_AFTER_SECONDS,
            )
        if self._in_flight_total >= self.max_in_flight_total:
            raise _DictationRefusedError(
                code="too-many-requests",
                message="Dictation is busy; try again shortly.",
                retry_after=_CONCURRENCY_RETRY_AFTER_SECONDS,
            )
        self._in_flight_by_user[user_id] = user_in_flight + 1
        self._in_flight_total += 1

    def _release_slot(self, user_id: str) -> None:
        remaining = self._in_flight_by_user.get(user_id, 0) - 1
        if remaining > 0:
            self._in_flight_by_user[user_id] = remaining
        else:
            self._in_flight_by_user.pop(user_id, None)
        self._in_flight_total = max(0, self._in_flight_total - 1)

    def _prune_history(self, user_id: str, cutoff: float) -> deque[tuple[float, float]] | None:
        while self._history:
            oldest_user, oldest_entries = next(iter(self._history.items()))
            if oldest_entries[-1][0] <= cutoff:
                self._history.pop(oldest_user)
            else:
                break
        entries = self._history.get(user_id)
        if entries is None:
            return None
        while entries and entries[0][0] <= cutoff:
            entries.popleft()
        if not entries:
            self._history.pop(user_id, None)
            return None
        return entries

    def _audio_retry_after(self, entries: deque[tuple[float, float]], needed: float, now: float) -> int:
        freed = 0.0
        unlock_at = now + self.window_seconds
        for ts, sec in entries:
            freed += sec
            if freed + 1e-9 >= needed:
                unlock_at = ts + self.window_seconds
                break
        return max(1, math.ceil(unlock_at - now))

    def _check_and_record(self, user_id: str, audio_seconds: float) -> None:
        now = self._clock()
        entries = self._prune_history(user_id, now - self.window_seconds)
        if entries is not None and len(entries) >= self.max_requests_per_minute:
            raise _DictationRefusedError(
                code="too-many-requests",
                message="Too many dictation requests; try again shortly.",
                retry_after=max(1, math.ceil(entries[0][0] + self.window_seconds - now)),
            )
        used_seconds = sum(sec for _, sec in entries) if entries is not None else 0.0
        if used_seconds + audio_seconds > self.max_audio_seconds_per_minute:
            needed = used_seconds + audio_seconds - self.max_audio_seconds_per_minute
            retry_after = self._audio_retry_after(entries or deque(), needed, now)
            raise _DictationRefusedError(
                code="too-many-requests",
                message="Dictation audio limit reached; try again shortly.",
                retry_after=retry_after,
            )
        if entries is None:
            entries = deque()
            self._history[user_id] = entries
        entries.append((now, audio_seconds))
        self._history.move_to_end(user_id)


class _DictationRoute(APIRoute):
    """Route handler translating ``_DictationRefusedError`` into the dictation error JSON envelope."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def answer(request: Request) -> Response:
            try:
                return await handler(request)
            except _DictationRefusedError as refusal:
                headers = {} if refusal.retry_after is None else {"Retry-After": str(refusal.retry_after)}
                return JSONResponse(
                    {"error": refusal.message, "code": refusal.code},
                    status_code=_STATUS[refusal.code],
                    headers=headers,
                )

        return answer


router = APIRouter(prefix="/api/dictation/v1", route_class=_DictationRoute, tags=["dictation"])


def _dictation_limiter(api_app: FastAPI) -> DictationLimiter:
    state = app_state(api_app)
    if state.dictation_limiter is None:
        state.dictation_limiter = DictationLimiter()
    return state.dictation_limiter


def _dictation_http_client(api_app: FastAPI) -> httpx.AsyncClient:
    state = app_state(api_app)
    client = state.dictation_http_client
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(_TRANSCRIBE_TIMEOUT_SECONDS, connect=5.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        state.dictation_http_client = client
    return client


async def close_dictation_http_client(api_app: FastAPI) -> None:
    """Close and clear the app's pooled dictation HTTP client on shutdown."""
    state = ensure_app_state(api_app)
    client = state.dictation_http_client
    state.dictation_http_client = None
    if client is not None and not client.is_closed:
        await client.aclose()


def _require_enabled_snapshot(request: Request) -> tuple[ApiSnapshot, Config]:
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    if (
        config is None
        or not config.voice.dictation_enabled
        or resolve_stt_connection(config, snapshot.runtime_paths) is None
    ):
        raise _DictationRefusedError(code="not-found", message=_DICTATION_OFF)
    return snapshot, config


def _allowed_any_responder(
    user_id: str,
    responder_names: Sequence[str],
    room_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
) -> tuple[bool, bool]:
    any_pending = False
    for name in responder_names:
        try:
            if is_sender_allowed_for_responder(
                user_id,
                name,
                room_id,
                config,
                runtime_paths,
                memberships,
                require_resolved_membership=True,
            ):
                return True, False
        except ReplyMembershipPendingError:
            any_pending = True
    return False, any_pending


async def _refresh_memberships(refresh: Callable[[], Awaitable[None]]) -> None:
    try:
        async with asyncio.timeout(_ACCESS_TIMEOUT_SECONDS):
            await refresh()
    except Exception:
        logger.debug("dictation_membership_refresh_failed")


async def _read_room_members(
    room_members: Callable[[str], Awaitable[Mapping[str, str | None]]] | None,
    room_id: str,
) -> Mapping[str, str | None]:
    if room_members is None:
        raise _DictationRefusedError(code="unavailable", message=_MEMBERSHIP_UNAVAILABLE)
    try:
        async with asyncio.timeout(_ACCESS_TIMEOUT_SECONDS):
            return await room_members(room_id)
    except Exception:
        logger.debug("dictation_room_membership_read_failed", room_id=room_id)
        raise _DictationRefusedError(code="unavailable", message=_MEMBERSHIP_UNAVAILABLE) from None


async def _authorized_in_room(
    request: Request,
    user_id: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    memberships: AgentReplyMembershipIndex,
) -> bool:
    state = app_state(request.app)
    members = await _read_room_members(state.room_members, room_id)
    if user_id not in members:
        return False
    entity_by_user_id = prepared_entity_names_by_user_id(config, runtime_paths)
    present_responders = tuple(name for member_id in members if (name := entity_by_user_id.get(member_id)) is not None)
    if not present_responders:
        return False
    allowed, pending = _allowed_any_responder(user_id, present_responders, room_id, config, runtime_paths, memberships)
    if allowed:
        return True
    embedded = state.embedded
    if embedded is not None:
        resolved = resolve_human_requester_alias(user_id, config, runtime_paths)
        await _refresh_memberships(
            lambda: memberships.refresh_until_member(
                resolved,
                room_id,
                config,
                runtime_paths,
                embedded.refresh_agent_reply_memberships_when_needed,
            ),
        )
        allowed, pending = _allowed_any_responder(
            user_id,
            present_responders,
            room_id,
            config,
            runtime_paths,
            memberships,
        )
        if allowed:
            return True
    if pending:
        raise _DictationRefusedError(code="unavailable", message=_MEMBERSHIP_UNAVAILABLE)
    return False


async def _authorize_user(
    request: Request,
    user_id: str,
    snapshot: ApiSnapshot,
    config: Config,
    *,
    room_id: str | None,
) -> None:
    runtime_paths = snapshot.runtime_paths
    if not is_access_checked_requester_id(user_id, config, runtime_paths):
        raise _DictationRefusedError(code="forbidden", message=_FORBIDDEN)
    if is_administrator(user_id, snapshot):
        return

    embedded = app_state(request.app).embedded
    memberships = embedded.agent_reply_memberships if embedded is not None else AgentReplyMembershipIndex()
    responders = (*config.agents, *config.teams)
    allowed, pending = _allowed_any_responder(user_id, responders, None, config, runtime_paths, memberships)
    if allowed:
        return
    if (pending or memberships.needs_refresh(config)) and embedded is not None:
        await _refresh_memberships(embedded.refresh_agent_reply_memberships_when_needed)
        if _allowed_any_responder(user_id, responders, None, config, runtime_paths, memberships)[0]:
            return
    if room_id and await _authorized_in_room(request, user_id, room_id, config, runtime_paths, memberships):
        return
    raise _DictationRefusedError(code="forbidden", message=_FORBIDDEN)


def _decode_optional_text(fields: Mapping[str, bytes], name: str) -> str | None:
    raw = fields.get(name)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise _DictationRefusedError(code="invalid", message=f"`{name}` must be valid UTF-8.") from None
    return text or None


def _extract_fields(fields: Mapping[str, bytes]) -> tuple[bytes, float, str | None, str | None]:
    audio = fields.get("audio")
    if audio is None:
        raise _DictationRefusedError(code="invalid", message="Send the audio segment as `audio`.")
    if len(audio) > _MAX_SEGMENT_BYTES:
        raise _DictationRefusedError(
            code="too-large",
            message=f"The audio segment is larger than {_MAX_SEGMENT_BYTES:,} bytes.",
        )
    try:
        seconds = wav_seconds(audio, expected_sample_rate=_EXPECTED_SAMPLE_RATE)
    except WavError as error:
        raise _DictationRefusedError(code="invalid", message=str(error)) from None
    if seconds <= 0:
        raise _DictationRefusedError(code="invalid", message="The audio segment is empty.")
    if seconds > _MAX_SEGMENT_SECONDS:
        raise _DictationRefusedError(
            code="too-large",
            message=f"The audio segment is longer than {_MAX_SEGMENT_SECONDS} seconds.",
        )

    prompt = _decode_optional_text(fields, "prompt")
    if prompt is not None and len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise _DictationRefusedError(code="invalid", message=f"`prompt` must be at most {_MAX_PROMPT_BYTES} bytes.")

    language = _decode_optional_text(fields, "language")
    if language is not None:
        if len(language) > _MAX_LANGUAGE_CHARS or _LANGUAGE_RE.fullmatch(language) is None:
            raise _DictationRefusedError(code="invalid", message="`language` must be a valid BCP 47 language tag.")
        language = language.split("-", 1)[0].lower()
    return audio, seconds, prompt, language


async def _read_dictation_form(request: Request) -> dict[str, bytes]:
    try:
        async with asyncio.timeout(_BODY_READ_TIMEOUT_SECONDS):
            return await parse_multipart_form(request, max_bytes=_MAX_FORM_BODY_BYTES)
    except TimeoutError:
        raise _DictationRefusedError(code="invalid", message="Request body timed out.") from None
    except RequestBodyTooLargeError as error:
        raise _DictationRefusedError(code="too-large", message=str(error)) from None
    except InvalidMultipartFormError as error:
        raise _DictationRefusedError(code="invalid", message=str(error)) from None


@router.get("/status")
async def dictation_status(request: Request, room_id: str | None = None) -> dict[str, object]:
    """Return dictation availability and segment limits for an authorized client session."""
    user = await require_client_user(request)
    snapshot, config = _require_enabled_snapshot(request)
    cleaned_room_id = room_id.strip() or None if room_id is not None else None
    await _authorize_user(request, user.user_id, snapshot, config, room_id=cleaned_room_id)
    return {"enabled": True, "maxSegmentSeconds": _MAX_SEGMENT_SECONDS, "maxSegmentBytes": _MAX_SEGMENT_BYTES}


@router.post("/transcribe")
async def transcribe_dictation(request: Request, room_id: str | None = None) -> dict[str, object]:
    """Transcribe one bounded 16 kHz mono 16-bit PCM WAV segment strictly in memory."""
    user = await require_client_user(request)
    snapshot, config = _require_enabled_snapshot(request)
    cleaned_room_id = room_id.strip() or None if room_id is not None else None
    await _authorize_user(request, user.user_id, snapshot, config, room_id=cleaned_room_id)

    limiter = _dictation_limiter(request.app)
    limiter._acquire_slot(user.user_id)
    try:
        fields = await _read_dictation_form(request)
        audio, seconds, prompt, language = _extract_fields(fields)
        limiter._check_and_record(user.user_id, seconds)

        started_at = time.monotonic()
        transcript = await transcribe_speech(
            audio,
            config,
            snapshot.runtime_paths,
            mime_type="audio/wav",
            language=language,
            prompt=prompt,
            timeout_seconds=_TRANSCRIBE_TIMEOUT_SECONDS,
            http_client=_dictation_http_client(request.app),
        )
        latency_ms = round((time.monotonic() - started_at) * 1000)
    finally:
        limiter._release_slot(user.user_id)

    rounded_seconds = round(seconds, 3)
    if transcript is None:
        logger.warning(
            "dictation_transcription_unavailable",
            user_id=user.user_id,
            seconds=rounded_seconds,
            latency_ms=latency_ms,
        )
        raise _DictationRefusedError(code="unavailable", message="Speech transcription is currently unavailable.")

    if transcript.usage is not None:
        await record_system_usage(transcript.usage, runtime_paths=snapshot.runtime_paths, kind="voice_dictation")
    logger.debug(
        "dictation_segment_transcribed",
        user_id=user.user_id,
        seconds=rounded_seconds,
        chars=len(transcript.text),
        latency_ms=latency_ms,
    )
    return {"text": transcript.text, "seconds": rounded_seconds}
