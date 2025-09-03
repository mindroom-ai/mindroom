"""Centralized message content extraction with large message support.

This module provides utilities to extract the full content from Matrix messages,
including handling large messages that are stored as MXC attachments.
"""

from __future__ import annotations

from typing import Any

import nio

from mindroom.logging_config import get_logger

logger = get_logger(__name__)


async def get_full_message_body(
    event: nio.RoomMessageText | dict[str, Any],
    client: nio.AsyncClient | None = None,
) -> str:
    """Extract the full message body, handling large message attachments.

    For regular messages, returns the body directly.
    For large messages with attachments, downloads and returns the full content.

    Args:
        event: Either a RoomMessageText event or a dict with message data
        client: Optional Matrix client for downloading attachments

    Returns:
        The full message body text

    """
    # Handle dict format (from fetch_thread_history)
    if isinstance(event, dict):
        content = event.get("content", {})
        body = str(event.get("body", ""))
    else:
        content = event.source.get("content", {})
        body = str(event.body)

    # Check if this is a large message with our custom metadata
    if "io.mindroom.long_text" in content:
        # This is a large message - need to fetch the attachment
        if not client:
            logger.warning("Cannot fetch large message attachment without client, returning preview")
            return body

        # Get the MXC URL from either 'url' (unencrypted) or 'file' (encrypted)
        mxc_url = None
        if "url" in content:
            mxc_url = content["url"]
        elif "file" in content:
            file_info = content["file"]
            mxc_url = file_info.get("url")

        if not mxc_url:
            logger.warning("Large message missing MXC URL, returning preview")
            return body

        # Download the full content
        try:
            full_text = await download_mxc_text(client, mxc_url, content.get("file"))
            if full_text:
                return full_text
            logger.warning("Failed to download large message, returning preview")
            return body  # noqa: TRY300
        except Exception:
            logger.exception("Error downloading large message, returning preview")
            return body

    # Regular message or no custom metadata
    return body


async def download_mxc_text(  # noqa: PLR0911
    client: nio.AsyncClient,
    mxc_url: str,
    file_info: dict[str, Any] | None = None,
) -> str | None:
    """Download text content from an MXC URL.

    Args:
        client: Matrix client
        mxc_url: The MXC URL to download from
        file_info: Optional encryption info for E2EE rooms

    Returns:
        The downloaded text content, or None if download failed

    """
    try:
        # Parse MXC URL
        if not mxc_url.startswith("mxc://"):
            logger.error(f"Invalid MXC URL: {mxc_url}")
            return None

        # Extract server and media ID
        parts = mxc_url[6:].split("/", 1)
        if len(parts) != 2:
            logger.error(f"Invalid MXC URL format: {mxc_url}")
            return None

        server_name, media_id = parts

        # Download the content
        response = await client.download(server_name, media_id)

        if not isinstance(response, nio.DownloadResponse):
            logger.error(f"Failed to download MXC content: {response}")
            return None

        # Handle encryption if needed
        if file_info and "key" in file_info:
            # Decrypt the content
            try:
                from nio import crypto  # noqa: PLC0415

                decrypted = crypto.attachments.decrypt_attachment(
                    response.body,
                    file_info["key"],
                    file_info["hashes"]["sha256"],
                    file_info["iv"],
                )
                text_bytes = decrypted
            except Exception:
                logger.exception("Failed to decrypt attachment")
                return None
        else:
            text_bytes = response.body

        # Decode to text
        try:
            text: str = text_bytes.decode("utf-8")
            return text  # noqa: TRY300
        except UnicodeDecodeError:
            logger.exception("Downloaded content is not valid UTF-8 text")
            return None

    except Exception:
        logger.exception("Error downloading MXC content")
        return None


def extract_message_data(
    event: nio.RoomMessageText,
    include_full_content: bool = True,
    client: nio.AsyncClient | None = None,  # noqa: ARG001
) -> dict[str, Any]:
    """Extract message data from a RoomMessageText event.

    This is a replacement for _extract_message_data that can optionally
    include the full message content for large messages.

    Args:
        event: The Matrix event to extract data from
        include_full_content: Whether to attempt to get full content for large messages
        client: Optional Matrix client for downloading attachments

    Returns:
        Dict with sender, body, timestamp, event_id, and content fields

    """
    data = {
        "sender": event.sender,
        "body": event.body,
        "timestamp": event.server_timestamp,
        "event_id": event.event_id,
        "content": event.source.get("content", {}),
    }

    # Mark if this needs full content retrieval
    if include_full_content and "io.mindroom.long_text" in data["content"]:
        data["_needs_full_content"] = True
        data["_preview_body"] = event.body

    return data


async def resolve_full_content(
    message_data: dict[str, Any],
    client: nio.AsyncClient,
) -> dict[str, Any]:
    """Resolve full content for a message that needs it.

    Args:
        message_data: Message data dict from extract_message_data
        client: Matrix client for downloading attachments

    Returns:
        Updated message data with full body content

    """
    if not message_data.get("_needs_full_content"):
        return message_data

    # Get the full body
    full_body = await get_full_message_body(message_data, client)

    # Update the message data
    message_data["body"] = full_body
    message_data.pop("_needs_full_content", None)
    message_data.pop("_preview_body", None)

    return message_data
