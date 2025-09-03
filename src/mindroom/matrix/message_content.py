"""Centralized message content extraction with large message support.

This module provides utilities to extract the full content from Matrix messages,
including handling large messages that are stored as MXC attachments.
"""

from __future__ import annotations

import time
from typing import Any

import nio
from nio import crypto

from mindroom.logging_config import get_logger

logger = get_logger(__name__)

# MXC download cache - stores (content, timestamp) tuples
# Key: mxc_url, Value: (content, timestamp)
_mxc_cache: dict[str, tuple[str, float]] = {}
_cache_ttl = 3600.0  # 1 hour TTL


async def _get_full_message_body(
    message_data: dict[str, Any],
    client: nio.AsyncClient | None = None,
) -> str:
    """Extract the full message body, handling large message attachments.

    For regular messages, returns the body directly.
    For large messages with attachments, downloads and returns the full content.

    Args:
        message_data: Dict with message data including 'body' and 'content' keys
        client: Optional Matrix client for downloading attachments

    Returns:
        The full message body text

    """
    content = message_data.get("content", {})
    body = str(message_data.get("body", ""))

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
        full_text = await _download_mxc_text(client, mxc_url, content.get("file"))
        if full_text:
            return full_text
        logger.warning("Failed to download large message, returning preview")
        return body

    # Regular message or no custom metadata
    return body


async def _download_mxc_text(  # noqa: PLR0911, C901
    client: nio.AsyncClient,
    mxc_url: str,
    file_info: dict[str, Any] | None = None,
) -> str | None:
    """Download text content from an MXC URL with caching.

    Args:
        client: Matrix client
        mxc_url: The MXC URL to download from
        file_info: Optional encryption info for E2EE rooms

    Returns:
        The downloaded text content, or None if download failed

    """
    # Check cache first
    current_time = time.time()
    if mxc_url in _mxc_cache:
        content, timestamp = _mxc_cache[mxc_url]
        if current_time - timestamp < _cache_ttl:
            logger.debug(f"Cache hit for MXC URL: {mxc_url}")
            return content
        # Expired, remove from cache
        del _mxc_cache[mxc_url]

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
            decoded_text: str = text_bytes.decode("utf-8")
        except UnicodeDecodeError:
            logger.exception("Downloaded content is not valid UTF-8 text")
            return None

        else:
            # Cache the result
            _mxc_cache[mxc_url] = (decoded_text, time.time())
            logger.debug(f"Cached MXC content for: {mxc_url}")

            # Clean old entries if cache is getting large
            if len(_mxc_cache) > 100:
                _clean_expired_cache()

            return decoded_text

    except Exception:
        logger.exception("Error downloading MXC content")
        return None


async def extract_and_resolve_message(
    event: nio.RoomMessageText,
    client: nio.AsyncClient | None = None,
) -> dict[str, Any]:
    """Extract message data and resolve large message content if needed.

    This is a convenience function that combines extraction and resolution
    of large message content in a single call.

    Args:
        event: The Matrix event to extract data from
        client: Optional Matrix client for downloading attachments

    Returns:
        Dict with sender, body, timestamp, event_id, and content fields.
        If the message is large and client is provided, body will contain
        the full text from the attachment.

    """
    # Extract basic message data
    data = {
        "sender": event.sender,
        "body": event.body,
        "timestamp": event.server_timestamp,
        "event_id": event.event_id,
        "content": event.source.get("content", {}),
    }

    # Check if this is a large message and resolve if we have a client
    if client and "io.mindroom.long_text" in data["content"]:
        data["body"] = await _get_full_message_body(data, client)

    return data


def _clean_expired_cache() -> None:
    """Remove expired entries from the MXC cache."""
    current_time = time.time()
    expired_keys = [key for key, (_, timestamp) in _mxc_cache.items() if current_time - timestamp >= _cache_ttl]
    for key in expired_keys:
        del _mxc_cache[key]
    if expired_keys:
        logger.debug(f"Cleaned {len(expired_keys)} expired cache entries")


def clear_mxc_cache() -> None:
    """Clear the entire MXC cache. Useful for testing."""
    _mxc_cache.clear()
