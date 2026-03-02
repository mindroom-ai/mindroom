"""Centralized message content extraction with large message support.

This module provides utilities to extract the full content from Matrix messages,
including handling large messages that are stored as MXC attachments.
"""

from __future__ import annotations

import json
import re
import time
from html import unescape
from typing import Any

import nio
from nio import crypto

from mindroom.logging_config import get_logger

logger = get_logger(__name__)

# MXC download cache - stores (content, timestamp) tuples
# Key: mxc_url, Value: (content, timestamp)
_mxc_cache: dict[str, tuple[str, float]] = {}
_cache_ttl = 3600.0  # 1 hour TTL


def _attachment_mimetype(content: dict[str, Any]) -> str | None:
    """Return attachment mimetype when available."""
    info = content.get("info")
    if isinstance(info, dict):
        mimetype = info.get("mimetype")
        if isinstance(mimetype, str):
            return mimetype

    file_info = content.get("file")
    if isinstance(file_info, dict):
        mimetype = file_info.get("mimetype")
        if isinstance(mimetype, str):
            return mimetype

    filename = content.get("filename")
    if isinstance(filename, str):
        normalized_filename = filename.lower()
        if normalized_filename.endswith((".html", ".htm")):
            return "text/html"
        if normalized_filename.endswith(".txt"):
            return "text/plain"
        if normalized_filename.endswith(".json"):
            return "application/json"

    return None


def _html_to_text(html_text: str) -> str:
    """Convert HTML attachment content back to plain text for prompt history."""

    def _anchor_to_text(match: re.Match[str]) -> str:
        href = match.group(1) or match.group(2) or match.group(3) or ""
        label = match.group(4).strip()
        if not label:
            return href
        if label == href:
            return href
        return f"{label} ({href})"

    text = re.sub(
        r"""(?is)<a\b[^>]*\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>`]+))[^>]*>(.*?)</a>""",
        _anchor_to_text,
        html_text,
    )
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|tr|h1|h2|h3|h4|h5|h6|pre|blockquote)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_large_message_v2_body(payload_json: str) -> str | None:
    """Extract prompt body text from a v2 large-message sidecar JSON payload."""
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    nested_new_content = payload.get("m.new_content")
    if isinstance(nested_new_content, dict):
        nested_body = nested_new_content.get("body")
        if isinstance(nested_body, str):
            return nested_body

    body = payload.get("body")
    if isinstance(body, str):
        return body
    return None


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
        long_text_meta = content.get("io.mindroom.long_text")
        long_text_version = long_text_meta.get("version") if isinstance(long_text_meta, dict) else None
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
        full_text = await _download_mxc_text(
            client,
            mxc_url,
            content.get("file"),
            mimetype=_attachment_mimetype(content),
        )
        if full_text:
            if long_text_version == 2:
                extracted_body = _extract_large_message_v2_body(full_text)
                if extracted_body is not None:
                    return extracted_body
                logger.warning("Invalid large-message v2 payload JSON, returning preview")
            else:
                return full_text
        else:
            logger.warning("Failed to download large message, returning preview")
        return body

    # Regular message or no custom metadata
    return body


async def _download_mxc_text(  # noqa: PLR0911, PLR0912, C901
    client: nio.AsyncClient,
    mxc_url: str,
    file_info: dict[str, Any] | None = None,
    mimetype: str | None = None,
) -> str | None:
    """Download text content from an MXC URL with caching.

    Args:
        client: Matrix client
        mxc_url: The MXC URL to download from
        file_info: Optional encryption info for E2EE rooms
        mimetype: Optional attachment MIME type

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
        if mimetype == "text/html":
            decoded_text = _html_to_text(decoded_text)

        # Cache the result
        _mxc_cache[mxc_url] = (decoded_text, time.time())
        logger.debug(f"Cached MXC content for: {mxc_url}")

        # Clean old entries if cache is getting large
        if len(_mxc_cache) > 100:
            _clean_expired_cache()

    except Exception:
        logger.exception("Error downloading MXC content")
        return None
    else:
        return decoded_text


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


async def extract_edit_body(
    event_source: dict[str, Any],
    client: nio.AsyncClient | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Extract body/content from an edit event's ``m.new_content`` payload."""
    content = event_source.get("content", {})
    new_content = content.get("m.new_content", {})

    body = new_content.get("body")
    if not isinstance(body, str):
        return None, None

    resolved_body = body
    if client and "io.mindroom.long_text" in new_content:
        resolved_body = await _get_full_message_body(
            {"body": body, "content": new_content},
            client,
        )

    return resolved_body, dict(new_content)


def _clean_expired_cache() -> None:
    """Remove expired entries from the MXC cache."""
    current_time = time.time()
    expired_keys = [key for key, (_, timestamp) in _mxc_cache.items() if current_time - timestamp >= _cache_ttl]
    for key in expired_keys:
        del _mxc_cache[key]
    if expired_keys:
        logger.debug(f"Cleaned {len(expired_keys)} expired cache entries")


def _clear_mxc_cache() -> None:
    """Clear the entire MXC cache. Useful for testing."""
    _mxc_cache.clear()
