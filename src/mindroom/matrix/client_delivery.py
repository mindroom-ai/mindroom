"""Matrix delivery helpers for sends, edits, and attachments."""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import nio
from nio import crypto
from nio.api import Api
from nio.exceptions import OlmTrustError

from mindroom.logging_config import get_logger
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.media import upload_content_uri, upload_media_bytes
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_matrix_edit_content
from mindroom.timing import emit_timing_event

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment

logger = get_logger(__name__)

_MATRIX_TRUST_DELIVERY_ERROR_MESSAGE = "Matrix encrypted delivery rejected by local device trust policy."
_MATRIX_GENERIC_DELIVERY_ERROR_MESSAGE = "Matrix delivery raised an unexpected local exception."
# Allow one default nio recovery pump, while keeping terminal delivery bounded.
_SYNC_RECOVERY_RETRY_TIMEOUT_SECONDS = 30.0
_SYNC_RECOVERY_RETRY_INITIAL_DELAY_SECONDS = 0.05
_SYNC_RECOVERY_RETRY_MAX_DELAY_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class DeliveredMatrixEvent:
    """One successfully delivered Matrix event plus the exact sent content payload."""

    event_id: str
    content_sent: dict[str, Any]


MatrixDeliveryFailureKind = Literal[
    "sync_recovery",
    "rate_limited",
    "network",
    "server",
    "forbidden",
    "not_in_room",
    "target_missing",
    "too_large",
    "bad_request",
    "local_precondition",
    "unknown",
]

# Matrix error codes whose failure is a property of the request or the room, not
# of the current transport window. Retrying these forever cannot make progress.
_PERMANENT_MATRIX_ERROR_KINDS: dict[str, MatrixDeliveryFailureKind] = {
    "M_FORBIDDEN": "forbidden",
    "M_UNKNOWN_TOKEN": "forbidden",
    "M_MISSING_TOKEN": "forbidden",
    "M_USER_DEACTIVATED": "forbidden",
    "M_BAD_STATE": "not_in_room",
    "M_NOT_FOUND": "target_missing",
    "M_TOO_LARGE": "too_large",
    "M_BAD_JSON": "bad_request",
    "M_NOT_JSON": "bad_request",
    "M_INVALID_PARAM": "bad_request",
    "M_UNRECOGNIZED": "bad_request",
    "M_UNSUPPORTED_ROOM_VERSION": "bad_request",
}
_TRANSIENT_MATRIX_ERROR_KINDS: dict[str, MatrixDeliveryFailureKind] = {
    "M_LIMIT_EXCEEDED": "rate_limited",
    "M_UNKNOWN": "server",
}


@dataclass(frozen=True, slots=True)
class MatrixDeliveryFailure:
    """Classified, log-safe reason one Matrix send or edit did not land."""

    kind: MatrixDeliveryFailureKind
    detail: str
    retry_after_seconds: float | None = None
    error: Exception | None = field(default=None, compare=False, repr=False)

    @property
    def retryable(self) -> bool:
        """Return whether a later attempt with the same payload could still succeed."""
        return self.kind in {"sync_recovery", "rate_limited", "network", "server", "unknown"}


@dataclass(frozen=True, slots=True)
class MatrixSendOutcome:
    """Exactly one of a delivered Matrix event or a classified delivery failure."""

    delivered: DeliveredMatrixEvent | None = None
    failure: MatrixDeliveryFailure | None = None


def classify_matrix_send_error(response: nio.RoomSendError) -> MatrixDeliveryFailure:
    """Classify one Matrix send error response without copying server payloads."""
    error_code = response.status_code if isinstance(response.status_code, str) and response.status_code else "M_UNKNOWN"
    retry_after_ms = getattr(response, "retry_after_ms", None)
    retry_after_seconds = (
        retry_after_ms / 1000
        if isinstance(retry_after_ms, int | float) and not isinstance(retry_after_ms, bool)
        else None
    )
    permanent_kind = _PERMANENT_MATRIX_ERROR_KINDS.get(error_code)
    if permanent_kind is not None:
        return MatrixDeliveryFailure(kind=permanent_kind, detail=error_code)
    transient_kind = _TRANSIENT_MATRIX_ERROR_KINDS.get(error_code, "server")
    return MatrixDeliveryFailure(
        kind=transient_kind,
        detail=error_code,
        retry_after_seconds=retry_after_seconds,
    )


def classify_matrix_delivery_exception(error: Exception) -> MatrixDeliveryFailure:
    """Classify one local Matrix delivery exception by type, never by payload."""
    if isinstance(error, nio.SendRetryError):
        return MatrixDeliveryFailure(kind="sync_recovery", detail="SendRetryError", error=error)
    if isinstance(error, OlmTrustError):
        return MatrixDeliveryFailure(kind="local_precondition", detail="OlmTrustError", error=error)
    if isinstance(error, nio.LocalProtocolError):
        return MatrixDeliveryFailure(kind="local_precondition", detail="LocalProtocolError", error=error)
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return MatrixDeliveryFailure(kind="network", detail=error.__class__.__name__, error=error)
    return MatrixDeliveryFailure(kind="unknown", detail=error.__class__.__name__, error=error)


def _sanitized_delivery_error_message(error: Exception) -> str:
    """Return a log-safe Matrix delivery failure message."""
    if isinstance(error, OlmTrustError):
        return _MATRIX_TRUST_DELIVERY_ERROR_MESSAGE
    return _MATRIX_GENERIC_DELIVERY_ERROR_MESSAGE


def _log_matrix_delivery_exception(
    error: Exception,
    *,
    room_id: str,
    operation: str,
    cache_bypass: bool,
) -> None:
    """Log one local Matrix send/edit exception without exposing device details."""
    logger.error(
        "matrix_message_delivery_exception",
        room_id=room_id,
        operation=operation,
        cache_bypass=cache_bypass,
        exception_type=error.__class__.__name__,
        error_message=_sanitized_delivery_error_message(error),
    )


_PreparedSendResult = tuple[object | None, MatrixDeliveryFailure | None]


async def _retry_prepared_room_message_after_sync_recovery(
    send_once: Callable[[], Awaitable[object | None]],
    *,
    original_error: nio.SendRetryError,
    room_id: str,
    operation: str,
    cache_bypass: bool,
) -> _PreparedSendResult:
    """Retry one frozen payload within a bounded sync-recovery window."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SYNC_RECOVERY_RETRY_TIMEOUT_SECONDS
    delay = _SYNC_RECOVERY_RETRY_INITIAL_DELAY_SECONDS
    first_retry = True
    exhausted = classify_matrix_delivery_exception(original_error)
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None, exhausted
        retry_delay = min(delay, remaining)
        log = logger.warning if first_retry else logger.debug
        log(
            "Waiting to retry Matrix delivery after sync recovery",
            room_id=room_id,
            operation=operation,
            retry_in_seconds=retry_delay,
        )
        await asyncio.sleep(retry_delay)
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None, exhausted
        try:
            return await asyncio.wait_for(send_once(), remaining), None
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return None, exhausted
        except nio.SendRetryError:
            first_retry = False
            delay = min(delay * 2, _SYNC_RECOVERY_RETRY_MAX_DELAY_SECONDS)
        except Exception as error:
            _log_matrix_delivery_exception(
                error,
                room_id=room_id,
                operation=operation,
                cache_bypass=cache_bypass,
            )
            return None, classify_matrix_delivery_exception(error)


async def _send_prepared_room_message(
    client: nio.AsyncClient,
    room_id: str,
    content_sent: dict[str, Any],
    *,
    message_type: str,
    cache_bypass: bool,
    operation: str,
    retry_sync_recovery: bool,
    transaction_id: str | None = None,
) -> _PreparedSendResult:
    """Send one prepared Matrix room message and normalize local delivery exceptions."""
    missing_access_token = MatrixDeliveryFailure(kind="local_precondition", detail="missing_access_token")

    async def send_once() -> object | None:
        if cache_bypass:
            access_token = client.access_token
            if not access_token:
                _log_matrix_delivery_exception(
                    nio.LocalProtocolError("Matrix client access token is required to send a message."),
                    room_id=room_id,
                    operation=operation,
                    cache_bypass=cache_bypass,
                )
                return None
            method, path, data = Api.room_send(
                access_token,
                room_id,
                message_type,
                content_sent,
                transaction_id or uuid4(),
            )
            return await client._send(
                nio.RoomSendResponse,
                method,
                path,
                data,
                response_data=(room_id,),
            )
        # Bots have no interactive device-verification flow, so encrypted sends
        # always deliver to unverified devices.
        return await client.room_send(
            room_id=room_id,
            message_type=message_type,
            content=content_sent,
            tx_id=transaction_id,
            ignore_unverified_devices=True,
        )

    try:
        response = await send_once()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if retry_sync_recovery and isinstance(error, nio.SendRetryError):
            return await _retry_prepared_room_message_after_sync_recovery(
                send_once,
                original_error=error,
                room_id=room_id,
                operation=operation,
                cache_bypass=cache_bypass,
            )
        _log_matrix_delivery_exception(
            error,
            room_id=room_id,
            operation=operation,
            cache_bypass=cache_bypass,
        )
        # Only an exhausted recovery retry re-raises to callers, so a swallowed
        # exception must not carry the original error forward.
        return None, replace(classify_matrix_delivery_exception(error), error=None)
    if response is None:
        return None, missing_access_token
    return response, None


def cached_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Return one room from nio's in-memory room cache if present."""
    return _cached_rooms(client).get(room_id)


def _cached_rooms(client: nio.AsyncClient) -> Mapping[str, nio.MatrixRoom]:
    """Return the client room cache when nio has initialized it."""
    rooms = client.rooms
    return rooms if isinstance(rooms, Mapping) else {}


def _can_send_to_encrypted_room(client: nio.AsyncClient, room_id: str, *, operation: str) -> bool:
    """Return whether one outbound room operation can proceed with current nio E2EE support."""
    room = cached_room(client, room_id)
    if room is None or not room.encrypted or crypto.ENCRYPTION_ENABLED:
        return True
    logger.error(
        "matrix_e2ee_support_required",
        room_id=room_id,
        operation=operation,
        hint="Reinstall MindRoom dependencies so `mindroom-nio[e2e]` is available for encrypted Matrix rooms.",
    )
    return False


async def _cached_or_remote_room_encrypted(client: nio.AsyncClient, room_id: str, *, operation: str) -> bool | None:
    """Return room encryption state, failing closed when an uncached room is encrypted."""
    room = cached_room(client, room_id)
    if room is not None:
        return bool(room.encrypted)

    encryption_state = await client.room_get_state_event(room_id, "m.room.encryption")
    if isinstance(encryption_state, nio.RoomGetStateEventResponse):
        logger.error(
            "matrix_encrypted_media_upload_requires_synced_room_cache",
            room_id=room_id,
            operation=operation,
            hint="Wait for initial sync to populate nio's room cache before uploading encrypted media.",
        )
        return None
    if isinstance(encryption_state, nio.RoomGetStateEventError) and encryption_state.status_code == "M_NOT_FOUND":
        return False
    logger.error(
        "matrix_media_upload_requires_known_encryption_state",
        room_id=room_id,
        operation=operation,
        hint="Unable to determine whether the room is encrypted while nio's room cache is empty.",
    )
    return None


def can_send_to_encrypted_room(client: nio.AsyncClient, room_id: str, *, operation: str) -> bool:
    """Return whether one outbound Matrix operation can safely proceed."""
    return _can_send_to_encrypted_room(client, room_id, operation=operation)


async def send_message_result(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
    *,
    operation: str = "send_message",
    retry_sync_recovery: bool = False,
    transaction_id: str | None = None,
) -> DeliveredMatrixEvent | None:
    """Send a message to a Matrix room and return the exact delivered payload."""
    outcome = await send_message_outcome(
        client,
        room_id,
        content,
        operation=operation,
        retry_sync_recovery=retry_sync_recovery,
        transaction_id=transaction_id,
    )
    _reraise_sync_recovery_failure(outcome.failure)
    return outcome.delivered


def _reraise_sync_recovery_failure(failure: MatrixDeliveryFailure | None) -> None:
    """Preserve the historical contract that only exhausted recovery retries raise.

    A ``SendRetryError`` seen without the bounded recovery retry enabled stays
    normalized to a ``None`` result, exactly as before classification existed.
    """
    if failure is None or failure.kind != "sync_recovery":
        return
    error = failure.error
    if isinstance(error, BaseException):
        raise error


async def send_message_outcome(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
    *,
    operation: str = "send_message",
    retry_sync_recovery: bool = False,
    transaction_id: str | None = None,
) -> MatrixSendOutcome:
    """Send a message to a Matrix room and return its delivered payload or classified failure."""
    if not _can_send_to_encrypted_room(client, room_id, operation=operation):
        return MatrixSendOutcome(
            failure=MatrixDeliveryFailure(kind="local_precondition", detail="e2ee_support_missing"),
        )

    rooms = client.rooms
    room = rooms.get(room_id) if isinstance(rooms, Mapping) else None
    cache_bypass = isinstance(rooms, Mapping) and room is None
    if cache_bypass:
        encryption_state = await client.room_get_state_event(room_id, "m.room.encryption")
        if isinstance(encryption_state, nio.RoomGetStateEventResponse):
            logger.error(
                "matrix_encrypted_room_send_requires_synced_room_cache",
                room_id=room_id,
                operation=operation,
                hint="Wait for initial sync to populate nio's room cache before sending to encrypted rooms.",
            )
            return MatrixSendOutcome(
                failure=MatrixDeliveryFailure(kind="local_precondition", detail="room_cache_not_synced"),
            )
        if not (
            isinstance(encryption_state, nio.RoomGetStateEventError) and encryption_state.status_code == "M_NOT_FOUND"
        ):
            logger.error(
                "matrix_room_send_requires_known_encryption_state",
                room_id=room_id,
                operation=operation,
                hint="Unable to determine whether the room is encrypted while nio's room cache is empty.",
            )
            return MatrixSendOutcome(
                failure=MatrixDeliveryFailure(kind="local_precondition", detail="room_encryption_state_unknown"),
            )

    message_type = "m.room.message"
    emit_timing_event(
        "Matrix send timing",
        phase="prepare_start",
        room_id=room_id,
        message_type=message_type,
    )
    content_sent = await prepare_large_message(client, room_id, content)
    emit_timing_event(
        "Matrix send timing",
        phase="prepare_finish",
        room_id=room_id,
        message_type=message_type,
    )
    emit_timing_event(
        "Matrix send timing",
        phase="send_start",
        room_id=room_id,
        message_type=message_type,
        cache_bypass=cache_bypass,
    )
    response, prepared_failure = await _send_prepared_room_message(
        client,
        room_id,
        content_sent,
        message_type=message_type,
        cache_bypass=cache_bypass,
        operation=operation,
        retry_sync_recovery=retry_sync_recovery,
        transaction_id=transaction_id,
    )
    if response is None:
        failure = prepared_failure or MatrixDeliveryFailure(kind="unknown", detail="delivery_exception")
        emit_timing_event(
            "Matrix send timing",
            phase="send_finish",
            room_id=room_id,
            message_type=message_type,
            cache_bypass=cache_bypass,
            outcome="error",
            error="delivery_exception",
        )
        return MatrixSendOutcome(failure=failure)
    if isinstance(response, nio.RoomSendResponse):
        emit_timing_event(
            "Matrix send timing",
            phase="send_finish",
            room_id=room_id,
            message_type=message_type,
            cache_bypass=cache_bypass,
            outcome="sent",
            event_id=str(response.event_id),
        )
        logger.debug(
            "matrix_message_sent",
            room_id=room_id,
            event_id=str(response.event_id),
            cache_bypass=cache_bypass,
        )
        return MatrixSendOutcome(
            delivered=DeliveredMatrixEvent(event_id=str(response.event_id), content_sent=content_sent),
        )
    emit_timing_event(
        "Matrix send timing",
        phase="send_finish",
        room_id=room_id,
        message_type=message_type,
        cache_bypass=cache_bypass,
        outcome="error",
        error=str(response),
    )
    logger.error(
        "matrix_message_send_failed",
        room_id=room_id,
        error=str(response),
        cache_bypass=cache_bypass,
    )
    failure = (
        classify_matrix_send_error(response)
        if isinstance(response, nio.RoomSendError)
        else MatrixDeliveryFailure(kind="unknown", detail=type(response).__name__)
    )
    return MatrixSendOutcome(failure=failure)


def _guess_mimetype(file_path: Path) -> str:
    guessed_mimetype, _ = mimetypes.guess_type(file_path.name)
    return guessed_mimetype or "application/octet-stream"


async def _upload_file_as_mxc(
    client: nio.AsyncClient,
    room_id: str,
    file_path: Path,
    *,
    mimetype: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload a local file as MXC, encrypting payloads in encrypted rooms."""
    try:
        file_bytes = await asyncio.to_thread(file_path.read_bytes)
    except OSError:
        logger.exception("Failed to read file before upload", path=str(file_path))
        return None, None

    return await _upload_media_bytes_as_mxc(
        client,
        room_id,
        file_bytes,
        filename=file_path.name,
        mimetype=mimetype,
    )


async def _upload_media_bytes_as_mxc(
    client: nio.AsyncClient,
    room_id: str,
    media_bytes: bytes,
    *,
    filename: str,
    mimetype: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload an in-memory Matrix media payload as MXC, encrypting for encrypted rooms."""
    info: dict[str, Any] = {"size": len(media_bytes), "mimetype": mimetype}
    room_encrypted = await _cached_or_remote_room_encrypted(client, room_id, operation="upload_media_bytes")
    if room_encrypted is None:
        return None, None
    upload_bytes = media_bytes
    encrypted_file_payload: dict[str, Any] | None = None
    upload_mimetype = mimetype
    upload_name = filename

    if room_encrypted:
        try:
            encrypted_bytes, encryption_keys = crypto.attachments.encrypt_attachment(media_bytes)
        except Exception:
            logger.exception("Failed to encrypt Matrix media upload", filename=filename)
            return None, None
        upload_bytes = encrypted_bytes
        upload_mimetype = "application/octet-stream"
        upload_name = f"{filename}.enc"
        encrypted_file_payload = {
            "url": "",
            "key": encryption_keys["key"],
            "iv": encryption_keys["iv"],
            "hashes": encryption_keys["hashes"],
            "v": "v2",
            "mimetype": mimetype,
            "size": len(media_bytes),
        }

    try:
        upload_response = await upload_media_bytes(
            client,
            upload_bytes,
            content_type=upload_mimetype,
            filename=upload_name,
        )
    except Exception:
        logger.exception("Failed uploading Matrix media", filename=filename)
        return None, None

    mxc_uri = upload_content_uri(upload_response)
    if mxc_uri is None:
        logger.error("Failed Matrix media upload response", filename=filename, response=str(upload_response))
        return None, None

    upload_payload: dict[str, Any] = {"info": info}
    if encrypted_file_payload is not None:
        encrypted_file_payload["url"] = mxc_uri
        upload_payload["file"] = encrypted_file_payload
    return mxc_uri, upload_payload


def _msgtype_for_mimetype(mimetype: str) -> str:
    """Return the Matrix msgtype appropriate for the given MIME type."""
    major = mimetype.split("/", 1)[0]
    if major == "image":
        return "m.image"
    if major == "video":
        return "m.video"
    if major == "audio":
        return "m.audio"
    return "m.file"


def _normalized_voice_waveform(waveform: Sequence[int] | None) -> list[int]:
    """Return a Matrix-compatible waveform payload."""
    if waveform is None:
        return [0] * 30
    return [min(1024, max(0, int(level))) for level in waveform]


def _voice_audio_details(duration_ms: int | None, waveform: Sequence[int] | None) -> dict[str, Any] | None:
    """Build Matrix voice-message audio details when duration is available."""
    if duration_ms is None:
        return None
    return {
        "duration": duration_ms,
        "waveform": _normalized_voice_waveform(waveform),
    }


def _thread_relation_content(thread_id: str | None, latest_thread_event_id: str | None) -> dict[str, Any] | None:
    """Build Matrix thread relation content for media sends."""
    if thread_id is None:
        return None
    if latest_thread_event_id is None:
        msg = "latest_thread_event_id is required for thread fallback"
        raise ValueError(msg)
    return {
        "rel_type": "m.thread",
        "event_id": thread_id,
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": latest_thread_event_id},
    }


async def send_file_message(
    client: nio.AsyncClient,
    room_id: str,
    file_path: str | Path,
    *,
    thread_id: str | None = None,
    caption: str | None = None,
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Upload a file and send it with the appropriate Matrix message type."""
    resolved_path = Path(file_path).expanduser().resolve()
    if not resolved_path.is_file():
        logger.error("Cannot send non-file attachment", path=str(resolved_path))
        return None
    if not _can_send_to_encrypted_room(client, room_id, operation="send_file_message"):
        return None

    mimetype = _guess_mimetype(resolved_path)
    mxc_uri, upload_payload = await _upload_file_as_mxc(client, room_id, resolved_path, mimetype=mimetype)
    if mxc_uri is None or upload_payload is None:
        return None

    info = upload_payload.get("info")
    if not isinstance(info, dict):
        info = {"size": resolved_path.stat().st_size, "mimetype": mimetype}

    msgtype = _msgtype_for_mimetype(mimetype)
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": caption or resolved_path.name,
        "info": info,
    }
    if msgtype == "m.file":
        content["filename"] = resolved_path.name
    encrypted_file_payload = upload_payload.get("file")
    if isinstance(encrypted_file_payload, dict):
        content["file"] = encrypted_file_payload
    else:
        content["url"] = mxc_uri

    thread_relation = _thread_relation_content(thread_id, latest_thread_event_id)
    if thread_relation is not None:
        content["m.relates_to"] = thread_relation

    delivered = await send_message_result(client, room_id, content)
    if delivered is not None and conversation_cache is not None:
        conversation_cache.notify_outbound_message(
            room_id,
            delivered.event_id,
            delivered.content_sent,
        )
    return delivered.event_id if delivered is not None else None


async def send_runtime_encrypted_media_message(
    client: nio.AsyncClient,
    room_id: str,
    attachment: RuntimeEncryptedMediaAttachment,
    *,
    thread_id: str | None = None,
    caption: str | None = None,
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Send an existing encrypted MXC object without writing or uploading plaintext bytes."""
    msgtype = _msgtype_for_mimetype(attachment.mime_type)
    content: dict[str, Any] = {
        "msgtype": msgtype,
        "body": caption or attachment.filename,
        "info": {"size": attachment.size, "mimetype": attachment.mime_type},
        "file": attachment.encrypted_file_content(),
    }
    if msgtype == "m.file":
        content["filename"] = attachment.filename
    thread_relation = _thread_relation_content(thread_id, latest_thread_event_id)
    if thread_relation is not None:
        content["m.relates_to"] = thread_relation

    delivered = await send_message_result(
        client,
        room_id,
        content,
        operation="send_runtime_encrypted_media_message",
    )
    if delivered is not None and conversation_cache is not None:
        conversation_cache.notify_outbound_message(
            room_id,
            delivered.event_id,
            delivered.content_sent,
        )
    return delivered.event_id if delivered is not None else None


async def send_audio_message(
    client: nio.AsyncClient,
    room_id: str,
    audio_bytes: bytes,
    *,
    mimetype: str,
    filename: str = "voice-message.opus",
    caption: str | None = None,
    duration_ms: int | None = None,
    waveform: Sequence[int] | None = None,
    thread_id: str | None = None,
    latest_thread_event_id: str | None = None,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Upload an in-memory audio payload and send it as a Matrix voice message."""
    if not _can_send_to_encrypted_room(client, room_id, operation="send_audio_message"):
        return None

    mxc_uri, upload_payload = await _upload_media_bytes_as_mxc(
        client,
        room_id,
        audio_bytes,
        filename=filename,
        mimetype=mimetype,
    )
    if mxc_uri is None or upload_payload is None:
        return None

    info = upload_payload.get("info")
    if not isinstance(info, dict):
        info = {"size": len(audio_bytes), "mimetype": mimetype}
    audio_details = _voice_audio_details(duration_ms, waveform)
    if audio_details is not None:
        info["duration"] = audio_details["duration"]

    content: dict[str, Any] = {
        "msgtype": "m.audio",
        "body": caption or filename,
        "info": info,
    }
    if audio_details is not None:
        content["org.matrix.msc3245.voice"] = {}
        content["org.matrix.msc1767.audio"] = audio_details
    if caption:
        content["filename"] = filename
    encrypted_file_payload = upload_payload.get("file")
    if isinstance(encrypted_file_payload, dict):
        content["file"] = encrypted_file_payload
    else:
        content["url"] = mxc_uri

    thread_relation = _thread_relation_content(thread_id, latest_thread_event_id)
    if thread_relation is not None:
        content["m.relates_to"] = thread_relation

    delivered = await send_message_result(client, room_id, content)
    if delivered is not None and conversation_cache is not None:
        conversation_cache.notify_outbound_message(
            room_id,
            delivered.event_id,
            delivered.content_sent,
        )
    return delivered.event_id if delivered is not None else None


def build_threaded_edit_content(
    *,
    new_text: str,
    thread_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    tool_trace: list[Any] | None = None,
    extra_content: dict[str, Any] | None = None,
    latest_thread_event_id: str | None = None,
) -> dict[str, Any]:
    """Build edit content that preserves thread fallback semantics when needed."""
    if thread_id is not None and latest_thread_event_id is None:
        msg = "latest_thread_event_id is required for thread fallback"
        raise ValueError(msg)

    return format_message_with_mentions(
        config,
        runtime_paths,
        new_text,
        thread_event_id=thread_id,
        latest_thread_event_id=latest_thread_event_id,
        tool_trace=tool_trace,
        extra_content=extra_content,
    )


def build_edit_event_content(
    *,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
    extra_content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Wrap replacement content in one Matrix m.replace edit envelope."""
    replacement_content = dict(new_content)
    replacement_content.pop("m.relates_to", None)
    if extra_content:
        replacement_content.update(extra_content)
    edit_content = build_matrix_edit_content(event_id, replacement_content)
    edit_content.update(
        {
            # Keep the fallback event's notification semantics aligned with
            # the replacement. In-progress streams use m.notice; terminal
            # updates return to m.text.
            "msgtype": replacement_content.get("msgtype", "m.text"),
            "body": f"* {new_text}",
            "format": "org.matrix.custom.html",
            "formatted_body": new_content.get("formatted_body", new_text),
        },
    )
    if extra_content:
        edit_content.update(extra_content)
    return edit_content


async def edit_message_result(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
    *,
    extra_content: dict[str, Any] | None = None,
    retry_sync_recovery: bool = False,
    transaction_id: str | None = None,
) -> DeliveredMatrixEvent | None:
    """Edit an existing Matrix message and return the exact delivered payload."""
    outcome = await edit_message_outcome(
        client,
        room_id,
        event_id,
        new_content,
        new_text,
        extra_content=extra_content,
        retry_sync_recovery=retry_sync_recovery,
        transaction_id=transaction_id,
    )
    _reraise_sync_recovery_failure(outcome.failure)
    return outcome.delivered


async def edit_message_outcome(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    new_content: dict[str, Any],
    new_text: str,
    *,
    extra_content: dict[str, Any] | None = None,
    retry_sync_recovery: bool = False,
    transaction_id: str | None = None,
) -> MatrixSendOutcome:
    """Edit an existing Matrix message and return its delivered payload or classified failure."""
    edit_content = build_edit_event_content(
        event_id=event_id,
        new_content=new_content,
        new_text=new_text,
        extra_content=extra_content,
    )

    return await send_message_outcome(
        client,
        room_id,
        edit_content,
        operation="edit_message",
        retry_sync_recovery=retry_sync_recovery,
        transaction_id=transaction_id,
    )


__all__ = [
    "DeliveredMatrixEvent",
    "MatrixDeliveryFailure",
    "MatrixDeliveryFailureKind",
    "MatrixSendOutcome",
    "build_edit_event_content",
    "build_threaded_edit_content",
    "cached_room",
    "can_send_to_encrypted_room",
    "classify_matrix_delivery_exception",
    "classify_matrix_send_error",
    "edit_message_outcome",
    "edit_message_result",
    "send_audio_message",
    "send_file_message",
    "send_message_outcome",
    "send_message_result",
    "send_runtime_encrypted_media_message",
]
