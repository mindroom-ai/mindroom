"""Handle large Matrix messages that exceed the 64KB event limit.

When a message is too large, we upload the full original content payload as
JSON and send a compact preview event with a pointer to that sidecar.
"""

from __future__ import annotations

import io
import json
from time import monotonic
from typing import Any

import nio
from nio import crypto

from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    ORIGINAL_SENDER_KEY,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    STREAM_VISIBLE_BODY_KEY,
    STREAM_WARMUP_SUFFIX_KEY,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.message_builder import markdown_to_html

logger = get_logger(__name__)

# Conservative limits accounting for Matrix overhead
_NORMAL_MESSAGE_LIMIT = 55000  # ~55KB for regular messages
_EDIT_MESSAGE_LIMIT = 27000  # ~27KB for edits (they roughly double in size)
_PASSTHROUGH_CONTENT_KEYS = frozenset(
    {
        "m.mentions",
        "com.mindroom.skip_mentions",
        ORIGINAL_SENDER_KEY,
        AI_RUN_METADATA_KEY,
        STREAM_STATUS_KEY,
        STREAM_WARMUP_SUFFIX_KEY,
    },
)
_SIDECAR_ONLY_MINDROOM_KEYS = frozenset(
    {
        "io.mindroom.long_text",
        "io.mindroom.tool_trace",
        STREAM_VISIBLE_BODY_KEY,
    },
)
_NONTERMINAL_STREAM_STATUSES = frozenset({STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING})
_NONTERMINAL_STREAM_PREVIEW_BYTES = 12000
_MATRIX_EVENT_HARD_LIMIT = 64000
_OVERSIZED_NONTERMINAL_STREAMING_SIDECAR_UPLOAD_MIN_INTERVAL_SECONDS = 5.0
_oversized_nonterminal_streaming_sidecar_uploaded_at: dict[tuple[str, str], float] = {}


def _is_passthrough_preview_key(key: object) -> bool:
    """Return whether one source key should stay on the preview event."""
    if not isinstance(key, str):
        return False

    return key in _PASSTHROUGH_CONTENT_KEYS or (
        key.startswith("io.mindroom.") and key not in _SIDECAR_ONLY_MINDROOM_KEYS
    )


def _is_passthrough_edit_wrapper_key(key: object) -> bool:
    """Return whether one source key should be mirrored onto the edit wrapper."""
    return isinstance(key, str) and key.startswith("io.mindroom.") and key not in _SIDECAR_ONLY_MINDROOM_KEYS


def _copy_preview_metadata(source_content: dict[str, Any], target_content: dict[str, Any]) -> None:
    """Copy metadata keys that should survive the large-message preview event."""
    target_content.update({key: value for key, value in source_content.items() if _is_passthrough_preview_key(key)})


def _copy_edit_wrapper_metadata(source_content: dict[str, Any], target_content: dict[str, Any]) -> None:
    """Mirror edit metadata onto the outer replacement event for client access."""
    target_content.update(
        {key: value for key, value in source_content.items() if _is_passthrough_edit_wrapper_key(key)},
    )


def _copy_inline_streaming_preview_metadata(source_content: dict[str, Any], target_content: dict[str, Any]) -> None:
    """Copy metadata that should remain inline on rich streaming previews."""
    _copy_preview_metadata(source_content, target_content)


def _room_is_encrypted(client: nio.AsyncClient, room_id: str | None) -> bool:
    return bool(room_id and room_id in client.rooms and client.rooms[room_id].encrypted)


def _add_sidecar_metadata(
    target_content: dict[str, Any],
    *,
    room_encrypted: bool,
    mxc_uri: str | None,
    file_info: dict[str, Any] | None,
    original_size: int,
) -> None:
    if mxc_uri is None or file_info is None:
        return

    if room_encrypted:
        target_content["file"] = file_info
    else:
        target_content["url"] = mxc_uri

    target_content["io.mindroom.long_text"] = {
        "version": 2,
        "encoding": "matrix_event_content_json",
        "original_event_size": original_size,
        "preview_size": len(target_content["body"]),
        "is_complete_content": True,
    }


def _calculate_event_size(content: dict[str, Any]) -> int:
    """Calculate the approximate size of a Matrix event.

    Args:
        content: The message content dictionary

    Returns:
        Approximate size in bytes including JSON overhead

    """
    # Convert to canonical JSON (sorted keys, no spaces)
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    # Add ~2KB overhead for event metadata, signatures, etc.
    return len(canonical.encode("utf-8")) + 2000


def _is_edit_message(content: dict[str, Any]) -> bool:
    """Check if this is an edit message."""
    return "m.new_content" in content or (
        "m.relates_to" in content and content.get("m.relates_to", {}).get("rel_type") == "m.replace"
    )


def _is_nonterminal_stream_content(content: dict[str, Any]) -> bool:
    """Return whether content is an in-progress streaming payload."""
    return content.get(STREAM_STATUS_KEY) in _NONTERMINAL_STREAM_STATUSES


def _clear_oversized_nonterminal_streaming_sidecar_upload_rate_limits() -> None:
    """Reset oversized streaming-edit sidecar upload rate state for tests."""
    _oversized_nonterminal_streaming_sidecar_uploaded_at.clear()


def _prune_expired_oversized_nonterminal_streaming_sidecar_upload_rate_limits(now: float) -> None:
    expired_keys = [
        key
        for key, sent_at in _oversized_nonterminal_streaming_sidecar_uploaded_at.items()
        if now - sent_at >= _OVERSIZED_NONTERMINAL_STREAMING_SIDECAR_UPLOAD_MIN_INTERVAL_SECONDS
    ]
    for key in expired_keys:
        _oversized_nonterminal_streaming_sidecar_uploaded_at.pop(key, None)


def should_upload_oversized_nonterminal_stream_sidecar(
    *,
    room_id: str,
    original_event_id: str,
    edit_content: dict[str, Any],
) -> bool:
    """Return whether one oversized non-terminal streaming edit should upload a fresh sidecar now."""
    if not original_event_id or not _is_edit_message(edit_content):
        return True

    source_content = edit_content.get("m.new_content")
    if not isinstance(source_content, dict) or not _is_nonterminal_stream_content(source_content):
        return True
    if _calculate_event_size(edit_content) <= _EDIT_MESSAGE_LIMIT:
        return True

    key = (room_id, original_event_id)
    now = monotonic()
    _prune_expired_oversized_nonterminal_streaming_sidecar_upload_rate_limits(now)
    last_sent_at = _oversized_nonterminal_streaming_sidecar_uploaded_at.get(key)
    if (
        last_sent_at is not None
        and now - last_sent_at < _OVERSIZED_NONTERMINAL_STREAMING_SIDECAR_UPLOAD_MIN_INTERVAL_SECONDS
    ):
        return False
    _oversized_nonterminal_streaming_sidecar_uploaded_at[key] = now
    return True


def _build_nonterminal_streaming_edit_preview(
    content: dict[str, Any],
    source_content: dict[str, Any],
    preview_text: str,
    *,
    room_encrypted: bool,
    mxc_uri: str | None,
    file_info: dict[str, Any] | None,
    original_size: int,
) -> dict[str, Any] | None:
    """Build an in-progress rich edit preview with optional full-content sidecar metadata."""
    preview_limit = _NONTERMINAL_STREAM_PREVIEW_BYTES
    while True:
        preview = _create_preview(
            preview_text,
            preview_limit,
            continuation_indicator=_STREAMING_PREVIEW_TRUNCATION_INDICATOR,
        )
        formatted_preview = markdown_to_html(preview)
        preview_content: dict[str, Any] = {
            "msgtype": source_content.get("msgtype", "m.text"),
            "body": preview,
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_preview,
        }
        _copy_inline_streaming_preview_metadata(source_content, preview_content)
        _add_sidecar_metadata(
            preview_content,
            room_encrypted=room_encrypted,
            mxc_uri=mxc_uri,
            file_info=file_info,
            original_size=original_size,
        )
        modified_content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"* {preview}",
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_preview,
            "m.new_content": preview_content,
            "m.relates_to": content.get("m.relates_to", {}),
        }
        _copy_edit_wrapper_metadata(source_content, modified_content)
        if _calculate_event_size(modified_content) <= _MATRIX_EVENT_HARD_LIMIT:
            return modified_content
        if preview_limit == 0:
            break
        preview_limit = max(0, preview_limit // 2)
    return None


def _prefix_by_bytes(text: str, max_bytes: int) -> str:
    """Return the longest prefix of *text* that fits within *max_bytes* UTF-8."""
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    lo, hi, best = 0, min(len(text), max_bytes), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(text[:mid].encode("utf-8")) <= max_bytes:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return text[:best]


_CONTINUATION_INDICATOR = "\n\n[Message continues in attached file]"
_STREAMING_PREVIEW_TRUNCATION_INDICATOR = "\n\n[Streaming preview truncated]"


def _create_preview(
    text: str,
    max_bytes: int,
    *,
    continuation_indicator: str = _CONTINUATION_INDICATOR,
) -> str:
    """Create a preview that fits within byte limit.

    Args:
        text: The full text to preview
        max_bytes: Maximum size in bytes for the preview
        continuation_indicator: Marker appended when the preview truncates text

    Returns:
        Preview text that fits within the byte limit

    """
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    indicator_bytes = len(continuation_indicator.encode("utf-8"))
    target_bytes = max_bytes - indicator_bytes
    if target_bytes <= 0:
        return continuation_indicator.lstrip()

    return _prefix_by_bytes(text, target_bytes) + continuation_indicator


async def _upload_text_as_mxc(  # noqa: C901
    client: nio.AsyncClient,
    text: str,
    room_id: str | None = None,
    *,
    mimetype: str = "text/plain",
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload text content as an MXC file.

    Args:
        client: The Matrix client
        text: The text content to upload
        room_id: Optional room ID to check for encryption
        mimetype: MIME type for the uploaded content (default: "text/plain")

    Returns:
        Tuple of (mxc_uri, file_info_dict) or (None, None) on failure

    """
    text_bytes = text.encode("utf-8")
    file_info = {
        "size": len(text_bytes),
        "mimetype": mimetype,
    }

    if mimetype == "text/html":
        filename = "message.html"
    elif mimetype == "application/json":
        filename = "message-content.json"
    else:
        filename = "message.txt"

    # Check if room is encrypted
    room_encrypted = False
    if room_id and room_id in client.rooms:
        room = client.rooms[room_id]
        room_encrypted = room.encrypted

    if room_encrypted:
        # Encrypt the content for E2EE room
        try:
            upload_data, encryption_keys = crypto.attachments.encrypt_attachment(text_bytes)

            # Store encryption info for the file
            file_info = {
                "url": "",  # Will be set after upload
                "key": encryption_keys["key"],
                "iv": encryption_keys["iv"],
                "hashes": encryption_keys["hashes"],
                "v": "v2",
                "mimetype": mimetype,
                "size": len(text_bytes),
            }
        except Exception:
            logger.exception("Failed to encrypt attachment")
            return None, None
    else:
        upload_data = text_bytes

    # Upload the file
    def data_provider(_monitor: object, _data: object) -> io.BytesIO:
        return io.BytesIO(upload_data)

    enc_filename = f"{filename}.enc" if room_encrypted else filename

    try:
        # nio.upload returns Tuple[Union[UploadResponse, UploadError], Optional[Dict[str, Any]]]
        upload_result, _encryption_dict = await client.upload(
            data_provider=data_provider,
            content_type="application/octet-stream" if room_encrypted else mimetype,
            filename=enc_filename,
            filesize=len(upload_data),
        )

        # Check if upload was successful
        if not isinstance(upload_result, nio.UploadResponse):
            logger.error(
                "large_message_sidecar_upload_failed",
                room_id=room_id,
                error=str(upload_result),
            )
            return None, None

        if not upload_result.content_uri:
            logger.error("Upload response missing content_uri")
            return None, None

        mxc_uri = str(upload_result.content_uri)
        file_info["url"] = mxc_uri

    except Exception:
        logger.exception("Failed to upload text")
        return None, None
    else:
        return mxc_uri, file_info


async def _build_file_content(
    client: nio.AsyncClient,
    room_id: str,
    full_content: dict[str, Any],
    preview_text: str,
    size_limit: int,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any]]:
    """Upload full original content JSON and build preview ``m.file`` event."""
    mxc_uri, file_info = await _upload_content_json_sidecar(client, room_id, full_content)

    attachment_overhead = 5000  # Conservative estimate for attachment JSON structure
    available = size_limit - attachment_overhead
    preview = _create_preview(preview_text, available)

    modified_content: dict[str, Any] = {
        "msgtype": "m.file",
        "body": preview,
        "filename": "message-content.json",
        "info": file_info,
    }

    return mxc_uri, file_info, modified_content


async def _upload_content_json_sidecar(
    client: nio.AsyncClient,
    room_id: str,
    full_content: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload full original content JSON for supported-client hydration."""
    upload_text = json.dumps(full_content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return await _upload_text_as_mxc(client, upload_text, room_id, mimetype="application/json")


async def prepare_large_message(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
    *,
    upload_nonterminal_stream_sidecar: bool = True,
) -> dict[str, Any]:
    """Check if message is too large and prepare it if needed.

    This function:
    1. Checks the message size
    2. If too large, uploads full original event content JSON as MXC
    3. Replaces body with maximum-size preview
    4. Adds metadata for reconstruction/hydration

    Args:
        client: The Matrix client
        room_id: The room to send to
        content: The message content dictionary
        upload_nonterminal_stream_sidecar: Whether oversized in-progress
            streaming edits should upload a fresh full-content JSON sidecar.

    Returns:
        Original content (if small) or modified content with preview and MXC reference

    """
    is_edit = _is_edit_message(content)
    size_limit = _EDIT_MESSAGE_LIMIT if is_edit else _NORMAL_MESSAGE_LIMIT

    current_size = _calculate_event_size(content)
    if current_size <= size_limit:
        return content

    source_content = content["m.new_content"] if is_edit and "m.new_content" in content else content
    preview_text = source_content["body"]
    if is_edit and _is_nonterminal_stream_content(source_content):
        mxc_uri = None
        file_info = None
        if upload_nonterminal_stream_sidecar:
            logger.info(
                "large_streaming_edit_sidecar_upload_started",
                room_id=room_id,
                original_size_bytes=current_size,
            )
            mxc_uri, file_info = await _upload_content_json_sidecar(client, room_id, content)
        else:
            logger.info(
                "large_streaming_edit_sidecar_upload_skipped",
                room_id=room_id,
                original_size_bytes=current_size,
            )
        modified_content = _build_nonterminal_streaming_edit_preview(
            content,
            source_content,
            preview_text,
            room_encrypted=_room_is_encrypted(client, room_id),
            mxc_uri=mxc_uri,
            file_info=file_info,
            original_size=current_size,
        )
        if modified_content is not None:
            inner: dict[str, Any] = modified_content["m.new_content"]
            logger.info(
                "large_streaming_edit_preview_prepared",
                room_id=room_id,
                original_size_bytes=current_size,
                preview_length=len(inner["body"]),
                final_size_bytes=_calculate_event_size(modified_content),
                has_sidecar="io.mindroom.long_text" in inner,
            )
            return modified_content

    logger.info(
        "large_message_sidecar_upload_started",
        room_id=room_id,
        original_size_bytes=current_size,
        is_edit=is_edit,
    )

    mxc_uri, file_info, modified_content = await _build_file_content(
        client,
        room_id,
        content,
        preview_text,
        size_limit,
    )

    _copy_preview_metadata(source_content, modified_content)
    _add_sidecar_metadata(
        modified_content,
        room_encrypted=_room_is_encrypted(client, room_id),
        mxc_uri=mxc_uri,
        file_info=file_info,
        original_size=current_size,
    )

    if "m.relates_to" in content:
        modified_content["m.relates_to"] = content["m.relates_to"]

    if is_edit and "m.new_content" in content:
        modified_content = {
            "msgtype": "m.text",
            "body": f"* {modified_content['body']}",
            "m.new_content": modified_content,
            "m.relates_to": content.get("m.relates_to", {}),
        }
        _copy_edit_wrapper_metadata(source_content, modified_content)

    final_size = _calculate_event_size(modified_content)
    if final_size > 64000:
        logger.warning(
            "large_message_still_exceeds_limit",
            room_id=room_id,
            final_size_bytes=final_size,
            size_limit_bytes=64000,
        )

    new_content = modified_content.get("m.new_content")
    inner = new_content if isinstance(new_content, dict) else modified_content
    body = inner.get("body")
    logger.info(
        "large_message_prepared",
        room_id=room_id,
        original_size_bytes=current_size,
        preview_length=len(body) if isinstance(body, str) else 0,
        is_edit=is_edit,
    )

    return modified_content
