"""Matrix delivery helpers for sends, edits, and attachments."""

from __future__ import annotations

import asyncio
import io
import mimetypes
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import nio
from nio import crypto
from nio.api import Api
from nio.exceptions import OlmTrustError

from mindroom.config.matrix import ignore_unverified_devices_for_config
from mindroom.logging_config import get_logger
from mindroom.matrix.large_messages import prepare_large_message
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import build_matrix_edit_content
from mindroom.timing import emit_timing_event

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol

logger = get_logger(__name__)

_MATRIX_TRUST_DELIVERY_ERROR_MESSAGE = "Matrix encrypted delivery rejected by local device trust policy."
_MATRIX_GENERIC_DELIVERY_ERROR_MESSAGE = "Matrix delivery raised an unexpected local exception."
_MATRIX_POLICY_FAILURE_KIND = "matrix_delivery_policy_rejection"


@dataclass(frozen=True, slots=True)
class DeliveredMatrixEvent:
    """One successfully delivered Matrix event plus the exact sent content payload."""

    event_id: str
    content_sent: dict[str, Any]


class MatrixExpectedDeliveryPolicyError(Exception):
    """Expected deterministic Matrix delivery rejection from local trust policy."""

    def __init__(
        self,
        *,
        room_id: str,
        operation: str,
        cache_bypass: bool,
        exception_type: str,
    ) -> None:
        super().__init__(_MATRIX_TRUST_DELIVERY_ERROR_MESSAGE)
        self.room_id = room_id
        self.operation = operation
        self.cache_bypass = cache_bypass
        self.exception_type = exception_type
        self.failure_kind = _MATRIX_POLICY_FAILURE_KIND


def is_expected_matrix_delivery_policy_error(error: BaseException) -> bool:
    """Return whether an exception is a deterministic Matrix policy rejection."""
    return isinstance(error, MatrixExpectedDeliveryPolicyError | OlmTrustError)


def _sanitized_delivery_error_message(error: Exception) -> str:
    """Return a log-safe Matrix delivery failure message."""
    if is_expected_matrix_delivery_policy_error(error):
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


def _matrix_delivery_policy_error(
    error: Exception,
    *,
    room_id: str,
    operation: str,
    cache_bypass: bool,
) -> MatrixExpectedDeliveryPolicyError:
    """Build one typed expected Matrix delivery policy rejection."""
    return MatrixExpectedDeliveryPolicyError(
        room_id=room_id,
        operation=operation,
        cache_bypass=cache_bypass,
        exception_type=error.__class__.__name__,
    )


async def _send_prepared_room_message(
    client: nio.AsyncClient,
    room_id: str,
    content_sent: dict[str, Any],
    *,
    message_type: str,
    cache_bypass: bool,
    operation: str,
    ignore_unverified_devices: bool,
) -> object | None:
    """Send one prepared Matrix room message and normalize local delivery exceptions."""
    try:
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
                uuid4(),
            )
            return await client._send(
                nio.RoomSendResponse,
                method,
                path,
                data,
                response_data=(room_id,),
            )
        return await client.room_send(
            room_id=room_id,
            message_type=message_type,
            content=content_sent,
            ignore_unverified_devices=ignore_unverified_devices,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _log_matrix_delivery_exception(
            error,
            room_id=room_id,
            operation=operation,
            cache_bypass=cache_bypass,
        )
        if is_expected_matrix_delivery_policy_error(error):
            raise _matrix_delivery_policy_error(
                error,
                room_id=room_id,
                operation=operation,
                cache_bypass=cache_bypass,
            ) from error
        raise


def cached_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Return one room from nio's in-memory room cache if present."""
    return cached_rooms(client).get(room_id)


def cached_rooms(client: nio.AsyncClient) -> Mapping[str, nio.MatrixRoom]:
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


def can_send_to_encrypted_room(client: nio.AsyncClient, room_id: str, *, operation: str) -> bool:
    """Return whether one outbound Matrix operation can safely proceed."""
    return _can_send_to_encrypted_room(client, room_id, operation=operation)


async def send_message_result(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
    *,
    config: Config,
    operation: str = "send_message",
) -> DeliveredMatrixEvent | None:
    """Send a message to a Matrix room and return the exact delivered payload."""
    if not _can_send_to_encrypted_room(client, room_id, operation=operation):
        return None

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
            return None
        if not (
            isinstance(encryption_state, nio.RoomGetStateEventError) and encryption_state.status_code == "M_NOT_FOUND"
        ):
            logger.error(
                "matrix_room_send_requires_known_encryption_state",
                room_id=room_id,
                operation=operation,
                hint="Unable to determine whether the room is encrypted while nio's room cache is empty.",
            )
            return None

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
    response = await _send_prepared_room_message(
        client,
        room_id,
        content_sent,
        message_type=message_type,
        cache_bypass=cache_bypass,
        operation=operation,
        ignore_unverified_devices=ignore_unverified_devices_for_config(config),
    )
    if response is None:
        emit_timing_event(
            "Matrix send timing",
            phase="send_finish",
            room_id=room_id,
            message_type=message_type,
            cache_bypass=cache_bypass,
            outcome="error",
            error="delivery_exception",
        )
        return None
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
        return DeliveredMatrixEvent(event_id=str(response.event_id), content_sent=content_sent)
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
    return None


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

    info: dict[str, Any] = {"size": len(file_bytes), "mimetype": mimetype}
    room = cached_room(client, room_id)
    if room is None:
        logger.error("Cannot determine encryption state for unknown room", room_id=room_id)
        return None, None
    room_encrypted = bool(room.encrypted)
    upload_bytes = file_bytes
    encrypted_file_payload: dict[str, Any] | None = None
    upload_mimetype = mimetype
    upload_name = file_path.name

    if room_encrypted:
        try:
            encrypted_bytes, encryption_keys = crypto.attachments.encrypt_attachment(file_bytes)
        except Exception:
            logger.exception("Failed to encrypt file attachment", path=str(file_path))
            return None, None
        upload_bytes = encrypted_bytes
        upload_mimetype = "application/octet-stream"
        upload_name = f"{file_path.name}.enc"
        encrypted_file_payload = {
            "url": "",
            "key": encryption_keys["key"],
            "iv": encryption_keys["iv"],
            "hashes": encryption_keys["hashes"],
            "v": "v2",
            "mimetype": mimetype,
            "size": len(file_bytes),
        }

    _upload_payload = upload_bytes

    def data_provider(_monitor: object, _data: object) -> io.BytesIO:
        return io.BytesIO(_upload_payload)

    try:
        upload_response = await client.upload(
            data_provider=data_provider,
            content_type=upload_mimetype,
            filename=upload_name,
            filesize=len(upload_bytes),
        )
    except Exception:
        logger.exception("Failed uploading Matrix file", path=str(file_path))
        return None, None

    upload_result = upload_response[0] if isinstance(upload_response, tuple) else upload_response

    if not isinstance(upload_result, nio.UploadResponse) or not upload_result.content_uri:
        logger.error("Failed file upload response", path=str(file_path), response=str(upload_result))
        return None, None

    mxc_uri = str(upload_result.content_uri)
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


async def send_file_message(
    client: nio.AsyncClient,
    room_id: str,
    file_path: str | Path,
    *,
    config: Config,
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

    if thread_id:
        if latest_thread_event_id is None:
            msg = "latest_thread_event_id is required for thread fallback"
            raise ValueError(msg)
        content["m.relates_to"] = {
            "rel_type": "m.thread",
            "event_id": thread_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": latest_thread_event_id},
        }

    delivered = await send_message_result(client, room_id, content, config=config)
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
    sender_domain: str,
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
        sender_domain=sender_domain,
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
            "msgtype": "m.text",
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
    config: Config,
    extra_content: dict[str, Any] | None = None,
) -> DeliveredMatrixEvent | None:
    """Edit an existing Matrix message and return the exact delivered payload."""
    edit_content = build_edit_event_content(
        event_id=event_id,
        new_content=new_content,
        new_text=new_text,
        extra_content=extra_content,
    )

    return await send_message_result(client, room_id, edit_content, config=config, operation="edit_message")


__all__ = [
    "DeliveredMatrixEvent",
    "MatrixExpectedDeliveryPolicyError",
    "build_edit_event_content",
    "build_threaded_edit_content",
    "edit_message_result",
    "is_expected_matrix_delivery_policy_error",
    "send_file_message",
    "send_message_result",
]
