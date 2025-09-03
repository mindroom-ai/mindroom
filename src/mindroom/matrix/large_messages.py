"""Handle large Matrix messages that exceed the 64KB event limit.

This module provides minimal intervention for messages that are too large,
uploading the full text as an MXC attachment while maximizing the preview size.
"""

from __future__ import annotations

import io
import json
from typing import Any

import nio
from nio import crypto

from mindroom.logging_config import get_logger

logger = get_logger(__name__)

# Conservative limits accounting for Matrix overhead
NORMAL_MESSAGE_LIMIT = 55000  # ~55KB for regular messages
EDIT_MESSAGE_LIMIT = 27000  # ~27KB for edits (they roughly double in size)


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


def _create_preview(text: str, max_bytes: int) -> str:
    """Create a preview that fits within byte limit.

    Args:
        text: The full text to preview
        max_bytes: Maximum size in bytes for the preview

    Returns:
        Preview text that fits within the byte limit

    """
    # Reserve space for continuation indicator
    indicator = "\n\n[Message continues in attached file]"
    indicator_bytes = len(indicator.encode("utf-8"))

    # If text fits entirely, return as-is
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    # Binary search for the maximum valid UTF-8 substring
    # Account for the indicator from the start
    target_bytes = max_bytes - indicator_bytes

    # Start with a reasonable estimate
    left, right = 0, min(len(text), target_bytes)
    best_pos = 0

    while left <= right:
        mid = (left + right) // 2
        try:
            # Check if this position creates valid UTF-8
            test_bytes = text[:mid].encode("utf-8")
            if len(test_bytes) <= target_bytes:
                best_pos = mid
                left = mid + 1
            else:
                right = mid - 1
        except UnicodeEncodeError:
            # Shouldn't happen with valid input
            right = mid - 1

    return text[:best_pos] + indicator


async def _upload_text_as_mxc(
    client: nio.AsyncClient,
    text: str,
    room_id: str | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Upload text content as an MXC file.

    Args:
        client: The Matrix client
        text: The text content to upload
        room_id: Optional room ID to check for encryption

    Returns:
        Tuple of (mxc_uri, file_info_dict) or (None, None) on failure

    """
    text_bytes = text.encode("utf-8")
    file_info = {
        "size": len(text_bytes),
        "mimetype": "text/plain",
    }

    # Check if room is encrypted
    room_encrypted = False
    if room_id and room_id in client.rooms:
        room = client.rooms[room_id]
        room_encrypted = room.encrypted

    if room_encrypted:
        # Encrypt the content for E2EE room
        try:
            encrypted_data = crypto.attachments.encrypt_attachment(text_bytes)
            upload_data = encrypted_data["data"]

            # Store encryption info for the file
            file_info = {
                "url": "",  # Will be set after upload
                "key": encrypted_data["key"],
                "iv": encrypted_data["iv"],
                "hashes": encrypted_data["hashes"],
                "v": "v2",
                "mimetype": "text/plain",
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

    try:
        # nio.upload returns Tuple[Union[UploadResponse, UploadError], Optional[Dict[str, Any]]]
        upload_result, encryption_dict = await client.upload(
            data_provider=data_provider,
            content_type="application/octet-stream" if room_encrypted else "text/plain",
            filename="message.txt.enc" if room_encrypted else "message.txt",
            filesize=len(upload_data),
        )

        # Check if upload was successful
        if not isinstance(upload_result, nio.UploadResponse):
            logger.error(f"Failed to upload text: {upload_result}")
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


async def prepare_large_message(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    """Check if message is too large and prepare it if needed.

    This function:
    1. Checks the message size
    2. If too large, uploads the full text as MXC
    3. Replaces body with maximum-size preview
    4. Adds metadata for reconstruction

    Args:
        client: The Matrix client
        room_id: The room to send to
        content: The message content dictionary

    Returns:
        Original content (if small) or modified content with preview and MXC reference

    """
    # Edit messages roughly double in size due to m.new_content structure
    # which includes both the edit wrapper and the actual new content
    is_edit = _is_edit_message(content)
    size_limit = EDIT_MESSAGE_LIMIT if is_edit else NORMAL_MESSAGE_LIMIT

    # Calculate current size
    current_size = _calculate_event_size(content)

    # If it fits, return unchanged
    if current_size <= size_limit:
        return content

    # Extract the text to upload (handle both regular and edit messages)
    full_text = content["m.new_content"]["body"] if is_edit and "m.new_content" in content else content["body"]

    logger.info(f"Message too large ({current_size} bytes), uploading to MXC")

    # Upload the full text
    mxc_uri, file_info = await _upload_text_as_mxc(client, full_text, room_id)

    # Calculate how much space we have for preview
    # We'll be sending an m.file message, so account for the file attachment structure
    # The structure adds: filename, url, info object, custom metadata
    attachment_overhead = 5000  # Conservative estimate for attachment JSON structure
    available_for_preview = size_limit - attachment_overhead

    # Create maximum-size preview
    preview = _create_preview(full_text, available_for_preview)

    # Create a standard m.file message with preview body
    modified_content = {
        "msgtype": "m.file",
        "body": preview,  # Preview text for immediate readability
        "filename": "message.txt",
        "info": file_info,
    }

    # Add the file URL (either encrypted or plain)
    if room_id and room_id in client.rooms and client.rooms[room_id].encrypted:
        # For encrypted rooms, use 'file' key
        modified_content["file"] = file_info
    else:
        # For unencrypted rooms, use 'url' key
        modified_content["url"] = mxc_uri

    # Add custom metadata to signal this is a long text message
    # Future custom clients can use this to render as inline text instead of attachment
    modified_content["io.mindroom.long_text"] = {
        "version": 1,
        "original_size": len(full_text),
        "preview_size": len(preview),
        "is_complete_text": True,
    }

    # Preserve thread/reply relationships if they exist
    if "m.relates_to" in content:
        modified_content["m.relates_to"] = content["m.relates_to"]

    # Handle edit messages specially
    if is_edit and "m.new_content" in content:
        # For edits, we need to wrap everything in the edit structure
        edit_content = {
            "msgtype": "m.text",  # Edit message type
            "body": f"* {preview}",
            "m.new_content": modified_content,
            "m.relates_to": content.get("m.relates_to", {}),
        }
        modified_content = edit_content

    logger.info(f"Large message prepared: {len(full_text)} bytes -> {len(preview)} preview + MXC attachment")

    return modified_content
