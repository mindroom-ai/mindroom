"""Handle large Matrix messages that exceed the 64KB event limit.

This module provides minimal intervention for messages that are too large,
uploading the full text as an MXC attachment while maximizing the preview size.
"""

from __future__ import annotations

import hashlib
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


def calculate_event_size(content: dict[str, Any]) -> int:
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


def is_edit_message(content: dict[str, Any]) -> bool:
    """Check if this is an edit message."""
    return "m.new_content" in content or (
        "m.relates_to" in content and content.get("m.relates_to", {}).get("rel_type") == "m.replace"
    )


def create_preview(text: str, max_bytes: int) -> str:
    """Create a maximum-size preview that breaks at natural boundaries.

    Args:
        text: The full text to preview
        max_bytes: Maximum size in bytes for the preview

    Returns:
        Preview text that fits within the byte limit

    """
    # Start with a safe estimate (UTF-8 can be up to 4 bytes per char)
    preview = text[: max_bytes // 2]

    # Extend until we approach the limit
    while len(preview) < len(text):
        # Try adding more characters
        test_preview = text[: len(preview) + 100]
        if len(test_preview.encode("utf-8")) > max_bytes:
            break
        preview = test_preview

    # Ensure we don't exceed the byte limit
    while len(preview.encode("utf-8")) > max_bytes:
        preview = preview[:-10]  # Trim 10 chars at a time

    # If we truncated, find a natural break point
    if len(preview) < len(text):
        # Try to break at paragraph, sentence, or word boundary
        for separator in ["\n\n", ". ", "\n", " "]:
            pos = preview.rfind(separator)
            if pos > max_bytes * 0.7:  # At least 70% of max size
                preview = preview[: pos + len(separator)].rstrip()
                break

        # Add continuation indicator
        indicator = "\n\n[Message continues...]"
        # Ensure indicator fits
        while len((preview + indicator).encode("utf-8")) > max_bytes:
            preview = preview[:-20]
        preview += indicator

    return preview


async def upload_text_as_mxc(  # noqa: C901, PLR0912
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
        Tuple of (mxc_uri, metadata_dict) or (None, None) on failure

    """
    text_bytes = text.encode("utf-8")
    metadata = {
        "size": len(text_bytes),
        "sha256": hashlib.sha256(text_bytes).hexdigest(),
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

            # Store encryption info in metadata
            metadata["file"] = {
                "url": "",  # Will be set after upload
                "key": encrypted_data["key"],
                "iv": encrypted_data["iv"],
                "hashes": encrypted_data["hashes"],
                "v": "v2",
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
        upload_result = await client.upload(
            data_provider=data_provider,
            content_type="application/octet-stream" if room_encrypted else "text/plain",
            filename="message.txt.enc" if room_encrypted else "message.txt",
            filesize=len(upload_data),
        )

        # Handle response
        if isinstance(upload_result, tuple):
            upload_response, error = upload_result
            if error:
                logger.error(f"Upload error: {error}")
                return None, None
        else:
            upload_response = upload_result

        if not isinstance(upload_response, nio.UploadResponse):
            # Check if it's a test/mock response with content_uri
            if hasattr(upload_response, "content_uri") and upload_response.content_uri:
                mxc_uri = str(upload_response.content_uri)
            else:
                logger.error(f"Failed to upload text: {upload_response}")
                return None, None
        else:
            if not upload_response.content_uri:
                logger.error("Upload response missing content_uri")
                return None, None
            mxc_uri = str(upload_response.content_uri)

        # Set the URL in the encrypted file metadata
        if room_encrypted and "file" in metadata:
            file_dict = metadata["file"]
            assert isinstance(file_dict, dict)
            file_dict["url"] = mxc_uri
        else:
            metadata["mxc"] = mxc_uri

        return mxc_uri, metadata  # noqa: TRY300

    except Exception:
        logger.exception("Failed to upload text")
        return None, None


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
    # Determine the appropriate size limit
    is_edit = is_edit_message(content)
    size_limit = EDIT_MESSAGE_LIMIT if is_edit else NORMAL_MESSAGE_LIMIT

    # Calculate current size
    current_size = calculate_event_size(content)

    # If it fits, return unchanged
    if current_size <= size_limit:
        return content

    # Extract the text to upload (handle both regular and edit messages)
    if is_edit and "m.new_content" in content:
        full_text = content["m.new_content"].get("body", "")
        full_html = content["m.new_content"].get("formatted_body", "")
    else:
        full_text = content.get("body", "")
        full_html = content.get("formatted_body", "")

    if not full_text:
        return content  # Nothing to do

    logger.info(f"Message too large ({current_size} bytes), uploading to MXC")

    # Upload the full text
    mxc_uri, metadata = await upload_text_as_mxc(client, full_text, room_id)
    if not mxc_uri or not metadata:
        logger.error("Failed to upload large message, sending truncated version")
        # Fall back to truncated message
        preview = create_preview(full_text, size_limit - 5000)
        content["body"] = preview
        if full_html:
            # Simple truncation for HTML (proper HTML truncation would need parsing)
            content["formatted_body"] = preview  # Use plain preview for safety
        return content

    # Calculate how much space we have for preview
    # Account for the metadata we'll add
    metadata_size = len(json.dumps({"io.mindroom.long_text": metadata}).encode("utf-8"))
    available_for_preview = size_limit - metadata_size - 3000  # Extra safety margin

    # Create maximum-size preview
    preview = create_preview(full_text, available_for_preview)

    # Modify the content with preview and metadata
    modified_content = content.copy()

    # Add our metadata
    modified_content["io.mindroom.long_text"] = metadata

    # Replace text with preview
    if is_edit and "m.new_content" in modified_content:
        # For edits, update both places
        modified_content["body"] = "* " + preview
        modified_content["m.new_content"]["body"] = preview
        if full_html:
            # For safety, use plain preview for HTML too
            modified_content["formatted_body"] = modified_content["body"]
            modified_content["m.new_content"]["formatted_body"] = preview
    else:
        modified_content["body"] = preview
        if full_html:
            modified_content["formatted_body"] = preview

    logger.info(f"Large message prepared: {len(full_text)} bytes -> {len(preview)} preview + MXC")

    return modified_content
