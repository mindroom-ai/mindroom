"""Handle large Matrix messages that exceed the 64KB event limit.

When a message is too large, we upload the full original content payload as
JSON and send a compact preview event with a pointer to that sidecar.
"""

from __future__ import annotations

import json
from time import monotonic
from typing import TYPE_CHECKING, Any

import nio
from nio import crypto

from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    ATTACHMENT_IDS_KEY,
    DURABLE_FINAL_OUTCOME_KEY,
    DURABLE_FINAL_OUTCOME_VERSION,
    HOOK_MESSAGE_RECEIVED_DEPTH_KEY,
    HOOK_SOURCE_KEY,
    ORIGINAL_SENDER_KEY,
    PER_FIRE_THREAD_ROOT_EVENT_ID_KEY,
    PER_FIRE_THREAD_ROOT_KEY,
    SKIP_MENTIONS_KEY,
    SOURCE_KIND_KEY,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    STREAM_VISIBLE_BODY_KEY,
    STREAM_WARMUP_SUFFIX_KEY,
    TOOL_TRACE_CONTENT_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    VOICE_TRANSCRIPT_KEY,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.encrypted_event_metadata import encryption_visible_metadata
from mindroom.matrix.media import upload_content_uri, upload_media_bytes
from mindroom.matrix.message_builder import markdown_to_html

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

# Conservative limits accounting for Matrix overhead
_NORMAL_MESSAGE_LIMIT = 55000  # ~55KB for regular messages
_EDIT_MESSAGE_LIMIT = 27000  # ~27KB for edits (they roughly double in size)
_LARGE_MESSAGE_PREVIEW_OVERHEAD_BYTES = 5000  # Reserve room for Matrix relation and preview metadata.
_PASSTHROUGH_CONTENT_KEYS = frozenset(
    {
        "m.mentions",
        HOOK_SOURCE_KEY,
        SKIP_MENTIONS_KEY,
        SOURCE_KIND_KEY,
        ATTACHMENT_IDS_KEY,
        HOOK_MESSAGE_RECEIVED_DEPTH_KEY,
        ORIGINAL_SENDER_KEY,
        PER_FIRE_THREAD_ROOT_EVENT_ID_KEY,
        PER_FIRE_THREAD_ROOT_KEY,
        AI_RUN_METADATA_KEY,
        STREAM_STATUS_KEY,
        STREAM_WARMUP_SUFFIX_KEY,
        VOICE_RAW_AUDIO_FALLBACK_KEY,
        VOICE_TRANSCRIPT_KEY,
    },
)
_SIDECAR_ONLY_MINDROOM_KEYS = frozenset(
    {
        "io.mindroom.long_text",
        TOOL_TRACE_CONTENT_KEY,
        STREAM_VISIBLE_BODY_KEY,
    },
)
_FILE_FALLBACK_KEYS = ("url", "file", "filename", "info")
_NONTERMINAL_STREAM_STATUSES = frozenset({STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING})
_NONTERMINAL_STREAM_PREVIEW_BYTES = 12000
_MATRIX_EVENT_HARD_LIMIT = 64000
_MEGOLM_AES_BLOCK_BYTES = 16
_MEGOLM_MAX_MESSAGE_INDEX_VARINT_BYTES = 5
_MEGOLM_BASE64_KEY_LENGTH = 43
_UNREPRESENTABLE_MESSAGE_ERROR = "Large message cannot fit within the Matrix event limit after sidecar preparation"
_OVERSIZED_NONTERMINAL_STREAMING_EDIT_MIN_INTERVAL_SECONDS = 5.0
_oversized_nonterminal_streaming_edit_sent_at: dict[tuple[str, str], float] = {}


class MatrixEventTooLargeError(ValueError):
    """The complete Matrix event cannot fit even after sidecar preparation."""


def _is_passthrough_preview_key(key: object) -> bool:
    """Return whether one source key should stay on the preview event."""
    if not isinstance(key, str):
        return False

    return key in _PASSTHROUGH_CONTENT_KEYS or (
        key.startswith("io.mindroom.") and key not in _SIDECAR_ONLY_MINDROOM_KEYS
    )


def _is_passthrough_edit_wrapper_key(key: object) -> bool:
    """Return whether one source key should be mirrored onto the edit wrapper."""
    return (
        isinstance(key, str)
        and key.startswith("io.mindroom.")
        and key not in _SIDECAR_ONLY_MINDROOM_KEYS
        and key != DURABLE_FINAL_OUTCOME_KEY
    )


def _copy_preview_metadata(source_content: dict[str, Any], target_content: dict[str, Any]) -> None:
    """Copy metadata keys that should survive the large-message preview event."""
    target_content.update({key: value for key, value in source_content.items() if _is_passthrough_preview_key(key)})


def _copy_edit_wrapper_metadata(source_content: dict[str, Any], target_content: dict[str, Any]) -> None:
    """Mirror edit metadata onto the outer replacement event for client access."""
    target_content.update(
        {key: value for key, value in source_content.items() if _is_passthrough_edit_wrapper_key(key)},
    )


def _copy_file_edit_fallback_fields(source_content: dict[str, Any], target_content: dict[str, Any]) -> None:
    """Keep the outer fallback independently valid when an edit replaces a file."""
    target_content.update({key: source_content[key] for key in _FILE_FALLBACK_KEYS if key in source_content})


def _wrap_large_edit(
    content: dict[str, Any],
    source_content: dict[str, Any],
    replacement_content: dict[str, Any],
) -> dict[str, Any]:
    """Build a compact edit wrapper whose fallback is valid on its own."""
    source_msgtype = source_content.get("msgtype", "m.text")
    wrapper_msgtype = (
        replacement_content.get("msgtype", source_msgtype) if source_msgtype == "m.file" else source_msgtype
    )
    wrapper = {
        "msgtype": wrapper_msgtype,
        "body": f"* {replacement_content['body']}",
        "m.new_content": replacement_content,
        "m.relates_to": content.get("m.relates_to", {}),
    }
    if source_msgtype == "m.file":
        _copy_file_edit_fallback_fields(replacement_content, wrapper)
    _copy_edit_wrapper_metadata(source_content, wrapper)
    return wrapper


def _copy_inline_streaming_preview_metadata(source_content: dict[str, Any], target_content: dict[str, Any]) -> None:
    """Copy metadata that should remain inline on rich streaming previews."""
    _copy_preview_metadata(source_content, target_content)


def _without_local_recovery_data(content: dict[str, Any]) -> dict[str, Any]:
    """Return event content without semantic results that belong only in the outbox."""
    replacement = content.get("m.new_content")
    compatibility_marker = {"version": DURABLE_FINAL_OUTCOME_VERSION}
    outer_has_result = (
        DURABLE_FINAL_OUTCOME_KEY in content and content[DURABLE_FINAL_OUTCOME_KEY] != compatibility_marker
    )
    nested_has_result = (
        isinstance(replacement, dict)
        and DURABLE_FINAL_OUTCOME_KEY in replacement
        and replacement[DURABLE_FINAL_OUTCOME_KEY] != compatibility_marker
    )
    if not outer_has_result and not nested_has_result:
        return content

    sanitized = dict(content)
    if outer_has_result:
        sanitized.pop(DURABLE_FINAL_OUTCOME_KEY, None)
    if isinstance(replacement, dict):
        sanitized_replacement = dict(replacement)
        if nested_has_result:
            sanitized_replacement.pop(DURABLE_FINAL_OUTCOME_KEY, None)
        sanitized["m.new_content"] = sanitized_replacement
    return sanitized


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


def _calculate_delivery_event_size(
    content: dict[str, Any],
    *,
    room_id: str,
    room_encrypted: bool,
    device_id: str | None,
) -> int:
    """Estimate the complete event size after nio's optional Megolm wrapping.

    The custom client copies a small recovery-metadata allowlist onto the
    encrypted envelope after Megolm encryption. Those outer fields are wire
    bytes too. Real encryption and this estimate therefore share
    ``encryption_visible_metadata``; letting the contracts drift would make a
    boundary payload fail identically on every replay.
    """
    if not room_encrypted:
        return _calculate_event_size(content)

    plaintext = nio.Api.to_json(
        {
            "content": content,
            "type": "m.room.message",
            "room_id": room_id,
        },
    )
    plaintext_bytes = len(plaintext.encode("utf-8"))
    padded_bytes = ((plaintext_bytes // _MEGOLM_AES_BLOCK_BYTES) + 1) * _MEGOLM_AES_BLOCK_BYTES
    ciphertext_length_varint_bytes = max(1, (padded_bytes.bit_length() + 6) // 7)
    signed_message_bytes = (
        padded_bytes
        + 1  # Megolm version byte.
        + 1  # Message-index protobuf tag.
        + _MEGOLM_MAX_MESSAGE_INDEX_VARINT_BYTES
        + 1  # Ciphertext protobuf tag.
        + ciphertext_length_varint_bytes
        + 8  # HMAC-SHA-256 truncated MAC.
        + 64  # Ed25519 signature.
    )
    ciphertext_bytes = ((signed_message_bytes + 2) // 3) * 4
    estimated_content: dict[str, Any] = {
        "algorithm": "m.megolm.v1.aes-sha2",
        "sender_key": "s" * _MEGOLM_BASE64_KEY_LENGTH,
        "ciphertext": "c" * ciphertext_bytes,
        "session_id": "i" * _MEGOLM_BASE64_KEY_LENGTH,
        "device_id": device_id,
    }
    relation = content.get("m.relates_to")
    if isinstance(relation, dict):
        estimated_content["m.relates_to"] = relation
    estimated_content.update(encryption_visible_metadata(content))
    return _calculate_event_size(estimated_content)


def _delivery_event_size_calculator(
    client: nio.AsyncClient,
    *,
    room_id: str,
    room_encrypted: bool,
) -> Callable[[dict[str, Any]], int]:
    """Bind the currently observed delivery fields for size estimation."""
    device_id: str | None = None
    if room_encrypted:
        olm = client.olm
        raw_device_id = olm.device_id if olm is not None else client.device_id
        device_id = raw_device_id if isinstance(raw_device_id, str) else None

    def calculate(candidate: dict[str, Any]) -> int:
        return _calculate_delivery_event_size(
            candidate,
            room_id=room_id,
            room_encrypted=room_encrypted,
            device_id=device_id,
        )

    return calculate


def _is_edit_message(content: dict[str, Any]) -> bool:
    """Check if this is an edit message."""
    return "m.new_content" in content or (
        "m.relates_to" in content and content.get("m.relates_to", {}).get("rel_type") == "m.replace"
    )


def _is_nonterminal_stream_content(content: dict[str, Any]) -> bool:
    """Return whether content is an in-progress streaming payload."""
    return content.get(STREAM_STATUS_KEY) in _NONTERMINAL_STREAM_STATUSES


def _prune_expired_oversized_nonterminal_streaming_edit_rate_limits(now: float) -> None:
    expired_keys = [
        key
        for key, sent_at in _oversized_nonterminal_streaming_edit_sent_at.items()
        if now - sent_at >= _OVERSIZED_NONTERMINAL_STREAMING_EDIT_MIN_INTERVAL_SECONDS
    ]
    for key in expired_keys:
        _oversized_nonterminal_streaming_edit_sent_at.pop(key, None)


def should_send_oversized_nonterminal_streaming_edit(
    *,
    room_id: str,
    original_event_id: str,
    edit_content: dict[str, Any],
) -> bool:
    """Return whether one oversized non-terminal streaming edit may be sent now."""
    if not original_event_id or not _is_edit_message(edit_content):
        return True

    source_content = edit_content.get("m.new_content")
    if not isinstance(source_content, dict) or not _is_nonterminal_stream_content(source_content):
        return True
    if _calculate_event_size(edit_content) <= _EDIT_MESSAGE_LIMIT:
        return True

    key = (room_id, original_event_id)
    now = monotonic()
    _prune_expired_oversized_nonterminal_streaming_edit_rate_limits(now)
    last_sent_at = _oversized_nonterminal_streaming_edit_sent_at.get(key)
    if last_sent_at is not None and now - last_sent_at < _OVERSIZED_NONTERMINAL_STREAMING_EDIT_MIN_INTERVAL_SECONDS:
        return False
    _oversized_nonterminal_streaming_edit_sent_at[key] = now
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
    calculate_event_size: Callable[[dict[str, Any]], int],
) -> dict[str, Any] | None:
    """Build an in-progress rich edit preview with a fresh full-content sidecar."""
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
            "msgtype": source_content.get("msgtype", "m.text"),
            "body": f"* {preview}",
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_preview,
            "m.new_content": preview_content,
            "m.relates_to": content.get("m.relates_to", {}),
        }
        _copy_edit_wrapper_metadata(source_content, modified_content)
        if calculate_event_size(modified_content) <= _MATRIX_EVENT_HARD_LIMIT:
            return modified_content
        if preview_limit == 0:
            break
        preview_limit = max(0, preview_limit // 2)
    return None


def _build_terminal_edit_preview(
    content: dict[str, Any],
    source_content: dict[str, Any],
    preview_content: dict[str, Any],
    preview_text: str,
    *,
    continuation_indicator: str,
    calculate_event_size: Callable[[dict[str, Any]], int],
) -> dict[str, Any]:
    """Fit a terminal edit after all replacement and wrapper metadata is present."""
    preview_body = preview_content.get("body")
    if not isinstance(preview_body, str):
        msg = "Large-message preview body must be text"
        raise TypeError(msg)

    def build_inner(inner_limit: int) -> dict[str, Any]:
        inner_content = dict(preview_content)
        inner_content["body"] = (
            _create_preview(
                preview_text,
                inner_limit,
                continuation_indicator=continuation_indicator,
            )
            if inner_limit > 0
            else ""
        )
        sidecar_metadata = inner_content.get("io.mindroom.long_text")
        if isinstance(sidecar_metadata, dict):
            sidecar_metadata = dict(sidecar_metadata)
            sidecar_metadata["preview_size"] = len(inner_content["body"])
            inner_content["io.mindroom.long_text"] = sidecar_metadata
        return inner_content

    def build_event(inner_content: dict[str, Any], outer_limit: int) -> dict[str, Any]:
        outer_body = (
            f"* {_create_preview(preview_text, outer_limit, continuation_indicator=continuation_indicator)}"
            if outer_limit > 0
            else ""
        )
        modified_content = _wrap_large_edit(content, source_content, inner_content)
        modified_content["body"] = outer_body
        return modified_content

    def fits(inner_content: dict[str, Any], outer_limit: int) -> bool:
        return calculate_event_size(build_event(inner_content, outer_limit)) <= _MATRIX_EVENT_HARD_LIMIT

    empty_inner = build_inner(0)
    if not fits(empty_inner, 0):
        raise MatrixEventTooLargeError(_UNREPRESENTABLE_MESSAGE_ERROR)

    maximum_inner_limit = len(preview_body.encode("utf-8"))
    full_inner = build_inner(maximum_inner_limit)
    if fits(full_inner, 0):
        fitted_inner = full_inner
    else:
        inner_limit = _largest_fitting_limit(
            maximum_inner_limit,
            lambda candidate_limit: fits(build_inner(candidate_limit), 0),
        )
        fitted_inner = build_inner(inner_limit)

    outer_limit = _largest_fitting_limit(
        len(fitted_inner["body"].encode("utf-8")),
        lambda candidate_limit: fits(fitted_inner, candidate_limit),
    )
    return build_event(fitted_inner, outer_limit)


def _fit_regular_preview(
    preview_content: dict[str, Any],
    preview_text: str,
    *,
    continuation_indicator: str,
    calculate_event_size: Callable[[dict[str, Any]], int],
) -> dict[str, Any]:
    """Fit a non-edit preview after all metadata and relations are present."""
    preview_body = preview_content.get("body")
    if not isinstance(preview_body, str):
        msg = "Large-message preview body must be text"
        raise TypeError(msg)

    def build_event(preview_limit: int) -> dict[str, Any]:
        event = dict(preview_content)
        event["body"] = (
            _create_preview(
                preview_text,
                preview_limit,
                continuation_indicator=continuation_indicator,
            )
            if preview_limit > 0
            else ""
        )
        sidecar_metadata = event.get("io.mindroom.long_text")
        if isinstance(sidecar_metadata, dict):
            sidecar_metadata = dict(sidecar_metadata)
            sidecar_metadata["preview_size"] = len(event["body"])
            event["io.mindroom.long_text"] = sidecar_metadata
        return event

    empty_preview = build_event(0)
    if calculate_event_size(empty_preview) > _MATRIX_EVENT_HARD_LIMIT:
        raise MatrixEventTooLargeError(_UNREPRESENTABLE_MESSAGE_ERROR)

    maximum_preview_limit = len(preview_body.encode("utf-8"))
    full_preview = build_event(maximum_preview_limit)
    if calculate_event_size(full_preview) <= _MATRIX_EVENT_HARD_LIMIT:
        return full_preview
    preview_limit = _largest_fitting_limit(
        maximum_preview_limit,
        lambda candidate_limit: calculate_event_size(build_event(candidate_limit)) <= _MATRIX_EVENT_HARD_LIMIT,
    )
    return build_event(preview_limit)


def _largest_fitting_limit(maximum: int, fits: Callable[[int], bool]) -> int:
    """Return the greatest monotonic byte limit accepted by ``fits``."""
    lower = 0
    upper = maximum
    while lower < upper:
        candidate = (lower + upper + 1) // 2
        if fits(candidate):
            lower = candidate
        else:
            upper = candidate - 1
    return lower


def _ensure_event_fits(
    content: dict[str, Any],
    *,
    room_id: str,
    calculate_event_size: Callable[[dict[str, Any]], int],
) -> None:
    """Reject any prepared payload that still exceeds the Matrix event limit."""
    final_size = calculate_event_size(content)
    if final_size <= _MATRIX_EVENT_HARD_LIMIT:
        return
    logger.error(
        "large_message_cannot_fit_event_limit",
        room_id=room_id,
        final_size_bytes=final_size,
        size_limit_bytes=_MATRIX_EVENT_HARD_LIMIT,
    )
    raise MatrixEventTooLargeError(_UNREPRESENTABLE_MESSAGE_ERROR)


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
_SIDECAR_UPLOAD_FALLBACK_INDICATOR = "\n\n[Message truncated because the attachment upload failed.]"


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


async def _upload_text_as_mxc(
    client: nio.AsyncClient,
    text: str,
    room_id: str | None = None,
    *,
    mimetype: str = "text/plain",
    room_encrypted: bool | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload text content as an MXC file.

    Args:
        client: The Matrix client
        text: The text content to upload
        room_id: Optional room ID to check for encryption
        mimetype: MIME type for the uploaded content (default: "text/plain")
        room_encrypted: Authoritative encryption state when the room cache is unavailable

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

    if room_encrypted is None:
        room_encrypted = _room_is_encrypted(client, room_id)

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

    enc_filename = f"{filename}.enc" if room_encrypted else filename

    try:
        # nio.upload returns Tuple[Union[UploadResponse, UploadError], Optional[Dict[str, Any]]]
        upload_result, _encryption_dict = await upload_media_bytes(
            client,
            upload_data,
            content_type="application/octet-stream" if room_encrypted else mimetype,
            filename=enc_filename,
        )

        # Check if upload was successful
        if not isinstance(upload_result, nio.UploadResponse):
            logger.error(
                "large_message_sidecar_upload_failed",
                room_id=room_id,
                error=str(upload_result),
            )
            return None, None

        mxc_uri = upload_content_uri(upload_result)
        if mxc_uri is None:
            logger.error("Upload response missing content_uri")
            return None, None

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
    *,
    room_encrypted: bool,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any]]:
    """Upload full original content JSON and build preview ``m.file`` event."""
    mxc_uri, file_info = await upload_json_sidecar(
        client,
        room_id,
        full_content,
        room_encrypted=room_encrypted,
    )

    available = size_limit - _LARGE_MESSAGE_PREVIEW_OVERHEAD_BYTES
    preview = _create_preview(preview_text, available)

    modified_content: dict[str, Any] = {
        "msgtype": "m.file",
        "body": preview,
        "filename": "message-content.json",
    }
    if file_info is not None:
        modified_content["info"] = file_info

    return mxc_uri, file_info, modified_content


def _sidecar_upload_has_common_metadata(file_info: dict[str, Any]) -> bool:
    size = file_info.get("size")
    mimetype = file_info.get("mimetype")
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
        and isinstance(mimetype, str)
        and mimetype != ""
    )


def _sidecar_upload_has_encrypted_metadata(mxc_uri: str, file_info: dict[str, Any]) -> bool:
    file_url = file_info.get("url")
    key = file_info.get("key")
    iv = file_info.get("iv")
    hashes = file_info.get("hashes")
    sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
    version = file_info.get("v")
    return (
        file_url == mxc_uri
        and isinstance(key, dict)
        and bool(key)
        and isinstance(iv, str)
        and iv != ""
        and isinstance(sha256, str)
        and sha256 != ""
        and version == "v2"
    )


def sidecar_upload_is_usable(
    mxc_uri: str | None,
    file_info: dict[str, Any] | None,
    *,
    room_encrypted: bool,
) -> bool:
    """Return whether one uploaded sidecar carries the metadata clients need to fetch it."""
    if not isinstance(mxc_uri, str) or not mxc_uri or not isinstance(file_info, dict):
        return False

    if not _sidecar_upload_has_common_metadata(file_info):
        return False

    return not room_encrypted or _sidecar_upload_has_encrypted_metadata(mxc_uri, file_info)


def content_fits_normal_event(content: dict[str, Any]) -> bool:
    """Return whether one content payload fits a normal Matrix event send."""
    return _calculate_event_size(content) <= _NORMAL_MESSAGE_LIMIT


async def upload_json_sidecar(
    client: nio.AsyncClient,
    room_id: str,
    payload: dict[str, Any],
    *,
    room_encrypted: bool | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload one JSON payload as an MXC sidecar and return ``(mxc_uri, file_info)``."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return await _upload_text_as_mxc(
        client,
        text,
        room_id,
        mimetype="application/json",
        room_encrypted=room_encrypted,
    )


def _build_text_fallback_content(
    source_content: dict[str, Any],
    preview_text: str,
    size_limit: int,
) -> dict[str, Any]:
    """Build a text preview when the full-content sidecar is unavailable."""
    preview_limit = max(0, size_limit - _LARGE_MESSAGE_PREVIEW_OVERHEAD_BYTES)
    preview_msgtype = "m.notice" if source_content.get("msgtype") == "m.notice" else "m.text"
    while True:
        preview_content: dict[str, Any] = {
            "msgtype": preview_msgtype,
            "body": _create_preview(
                preview_text,
                preview_limit,
                continuation_indicator=_SIDECAR_UPLOAD_FALLBACK_INDICATOR,
            ),
        }
        _copy_preview_metadata(source_content, preview_content)
        if _calculate_event_size(preview_content) <= size_limit or preview_limit == 0:
            return preview_content
        preview_limit = max(0, preview_limit // 2)


def _ensure_minimum_preview_can_fit(
    content: dict[str, Any],
    source_content: dict[str, Any],
    *,
    is_edit: bool,
    room_id: str,
    calculate_event_size: Callable[[dict[str, Any]], int],
) -> None:
    """Reject fixed metadata that cannot fit even with an empty text preview.

    This is deliberately a lower-bound check before any sidecar upload. If the
    smallest possible fallback cannot fit, uploading cannot make the event
    deliverable and would only leave an unreferenced attachment behind.
    """
    preview_msgtype = "m.notice" if source_content.get("msgtype") == "m.notice" else "m.text"
    minimum_preview: dict[str, Any] = {"msgtype": preview_msgtype, "body": ""}
    _copy_preview_metadata(source_content, minimum_preview)

    if is_edit:
        minimum_event = _wrap_large_edit(content, source_content, minimum_preview)
        minimum_event["body"] = ""
    else:
        minimum_event = minimum_preview
        if "m.relates_to" in content:
            minimum_event["m.relates_to"] = content["m.relates_to"]

    _ensure_event_fits(
        minimum_event,
        room_id=room_id,
        calculate_event_size=calculate_event_size,
    )


def _fit_large_message_preview(
    content: dict[str, Any],
    source_content: dict[str, Any],
    preview_content: dict[str, Any],
    preview_text: str,
    *,
    is_edit: bool,
    continuation_indicator: str,
    calculate_event_size: Callable[[dict[str, Any]], int],
) -> dict[str, Any]:
    """Attach relations and fit one regular or replacement preview."""
    if "m.relates_to" in content:
        preview_content["m.relates_to"] = content["m.relates_to"]

    if is_edit and "m.new_content" in content:
        return _build_terminal_edit_preview(
            content,
            source_content,
            preview_content,
            preview_text,
            continuation_indicator=continuation_indicator,
            calculate_event_size=calculate_event_size,
        )
    return _fit_regular_preview(
        preview_content,
        preview_text,
        continuation_indicator=continuation_indicator,
        calculate_event_size=calculate_event_size,
    )


async def prepare_large_message(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
    *,
    room_encrypted: bool | None = None,
    prepare_for_encrypted_delivery: bool = False,
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
        room_encrypted: Authoritative encryption state when the room cache is unavailable
        prepare_for_encrypted_delivery: Encrypt sidecars and fit a durable payload for later encrypted delivery

    Returns:
        The original content object when no preparation is needed, otherwise
        modified content with a preview and MXC reference. The identity
        guarantee lets the delivery boundary avoid a redundant state lookup
        when this coroutine completed without yielding.

    """
    content = _without_local_recovery_data(content)
    is_edit = _is_edit_message(content)
    size_limit = _EDIT_MESSAGE_LIMIT if is_edit else _NORMAL_MESSAGE_LIMIT
    if room_encrypted is None:
        room_encrypted = _room_is_encrypted(client, room_id)
    encrypted_delivery_safe = room_encrypted or prepare_for_encrypted_delivery
    calculate_delivery_event_size = _delivery_event_size_calculator(
        client,
        room_id=room_id,
        room_encrypted=encrypted_delivery_safe,
    )
    current_size = _calculate_event_size(content)
    if current_size <= size_limit and calculate_delivery_event_size(content) <= _MATRIX_EVENT_HARD_LIMIT:
        return content

    source_content = content["m.new_content"] if is_edit and "m.new_content" in content else content
    preview_text = source_content["body"]
    _ensure_minimum_preview_can_fit(
        content,
        source_content,
        is_edit=is_edit,
        room_id=room_id,
        calculate_event_size=calculate_delivery_event_size,
    )
    if is_edit and _is_nonterminal_stream_content(source_content):
        logger.info(
            "large_streaming_edit_sidecar_upload_started",
            room_id=room_id,
            original_size_bytes=current_size,
        )
        mxc_uri, file_info = await upload_json_sidecar(
            client,
            room_id,
            content,
            room_encrypted=encrypted_delivery_safe,
        )
        if not sidecar_upload_is_usable(
            mxc_uri,
            file_info,
            room_encrypted=encrypted_delivery_safe,
        ):
            logger.warning(
                "large_message_sidecar_unavailable_using_inline_preview",
                room_id=room_id,
                original_size_bytes=current_size,
                is_edit=True,
                has_mxc_uri=bool(mxc_uri),
                has_file_info=bool(file_info),
            )
            mxc_uri = None
            file_info = None
        modified_content = _build_nonterminal_streaming_edit_preview(
            content,
            source_content,
            preview_text,
            room_encrypted=encrypted_delivery_safe,
            mxc_uri=mxc_uri,
            file_info=file_info,
            original_size=current_size,
            calculate_event_size=calculate_delivery_event_size,
        )
        if modified_content is not None:
            inner: dict[str, Any] = modified_content["m.new_content"]
            logger.info(
                "large_streaming_edit_preview_prepared",
                room_id=room_id,
                original_size_bytes=current_size,
                preview_length=len(inner["body"]),
                final_size_bytes=calculate_delivery_event_size(modified_content),
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
        room_encrypted=encrypted_delivery_safe,
    )

    sidecar_usable = sidecar_upload_is_usable(
        mxc_uri,
        file_info,
        room_encrypted=encrypted_delivery_safe,
    )
    if sidecar_usable:
        _copy_preview_metadata(source_content, modified_content)
        _add_sidecar_metadata(
            modified_content,
            room_encrypted=encrypted_delivery_safe,
            mxc_uri=mxc_uri,
            file_info=file_info,
            original_size=current_size,
        )
    else:
        logger.warning(
            "large_message_sidecar_unavailable_using_text_fallback",
            room_id=room_id,
            original_size_bytes=current_size,
            is_edit=is_edit,
            has_mxc_uri=bool(mxc_uri),
            has_file_info=bool(file_info),
        )
        modified_content = _build_text_fallback_content(source_content, preview_text, size_limit)

    try:
        modified_content = _fit_large_message_preview(
            content,
            source_content,
            modified_content,
            preview_text,
            is_edit=is_edit,
            continuation_indicator=(_CONTINUATION_INDICATOR if sidecar_usable else _SIDECAR_UPLOAD_FALLBACK_INDICATOR),
            calculate_event_size=calculate_delivery_event_size,
        )
    except MatrixEventTooLargeError:
        if not sidecar_usable:
            raise
        # The server chooses the final MXC URI, so its exact sidecar envelope
        # cannot be proven before upload. The empty text envelope was proven
        # above; fall back to it instead of failing and uploading again on the
        # next delivery attempt.
        logger.warning(
            "large_message_sidecar_envelope_too_large_using_text_fallback",
            room_id=room_id,
            original_size_bytes=current_size,
            is_edit=is_edit,
        )
        modified_content = _fit_large_message_preview(
            content,
            source_content,
            _build_text_fallback_content(source_content, preview_text, size_limit),
            preview_text,
            is_edit=is_edit,
            continuation_indicator=_SIDECAR_UPLOAD_FALLBACK_INDICATOR,
            calculate_event_size=calculate_delivery_event_size,
        )

    _ensure_event_fits(
        modified_content,
        room_id=room_id,
        calculate_event_size=calculate_delivery_event_size,
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
