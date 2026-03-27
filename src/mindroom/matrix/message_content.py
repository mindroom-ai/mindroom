"""Centralized message content extraction for Matrix sidecar-backed messages."""

from __future__ import annotations

import json
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

if hasattr(nio, "RoomMessageFormatted"):
    type ReadableMessageEvent = nio.RoomMessageFormatted
else:
    type ReadableMessageEvent = nio.RoomMessageText | nio.RoomMessageNotice


def _extract_large_message_v2_content(payload_json: str) -> dict[str, Any] | None:
    """Extract canonical content dict from a v2 large-message sidecar JSON payload."""
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return {key: value for key, value in payload.items() if isinstance(key, str)}


def non_text_msgtype(content: dict[str, Any] | None) -> str | None:
    """Return the Matrix msgtype only when it differs from plain text."""
    if not isinstance(content, dict):
        return None

    msgtype = content.get("msgtype")
    if not isinstance(msgtype, str) or msgtype == "m.text":
        return None
    return msgtype


def _normalized_content_dict(content: object) -> dict[str, Any]:
    """Return a string-keyed content dict."""
    if not isinstance(content, dict):
        return {}
    return {key: value for key, value in content.items() if isinstance(key, str)}


def _content_body(content: dict[str, Any], fallback_body: str) -> str:
    """Return the body from content when present, otherwise the provided fallback."""
    body = content.get("body")
    return body if isinstance(body, str) else fallback_body


def visible_body_from_event_source(event_source: dict[str, Any], fallback_body: str) -> str:
    """Return the visible message body from an event source dict."""
    content = _normalized_content_dict(event_source.get("content", {}))
    visible_content = _normalized_content_dict(content.get("m.new_content")) or content
    return _content_body(visible_content, fallback_body)


def is_v2_sidecar_text_preview(event_source: dict[str, Any]) -> bool:
    """Return whether one event source is a large-text preview transported as ``m.file``."""
    content = _normalized_content_dict(event_source.get("content", {}))
    if content.get("msgtype") != "m.file":
        return False

    long_text_meta = content.get("io.mindroom.long_text")
    if not isinstance(long_text_meta, dict):
        return False
    if long_text_meta.get("version") != 2 or long_text_meta.get("encoding") != "matrix_event_content_json":
        return False
    return _sidecar_mxc_url(content) is not None


def _sidecar_content_for_resolution(content: dict[str, Any]) -> dict[str, Any] | None:
    """Return the content dict that owns the long-text sidecar metadata."""
    if "io.mindroom.long_text" in content:
        return content

    new_content = content.get("m.new_content")
    if isinstance(new_content, dict) and "io.mindroom.long_text" in new_content:
        return new_content

    return None


def _sidecar_mxc_url(content: dict[str, Any]) -> str | None:
    """Return the MXC URL referenced by one sidecar-backed content dict."""
    url = content.get("url")
    if isinstance(url, str):
        return url

    file_info = content.get("file")
    if not isinstance(file_info, dict):
        return None

    file_url = file_info.get("url")
    return file_url if isinstance(file_url, str) else None


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

        # Validate the MXC URL structure before issuing the download.
        parts = mxc_url[6:].split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            logger.error(f"Invalid MXC URL format: {mxc_url}")
            return None

        response = await client.download(mxc=mxc_url)

        if not isinstance(response, nio.DownloadResponse):
            logger.error(f"Failed to download MXC content: {response}", mxc_url=mxc_url)
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
    event: ReadableMessageEvent,
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
    preview_content = _normalized_content_dict(event.source.get("content", {}))
    resolved_content = await _resolve_canonical_content(preview_content, client)
    data = {
        "sender": event.sender,
        "body": _content_body(resolved_content, event.body),
        "timestamp": event.server_timestamp,
        "event_id": event.event_id,
        "content": resolved_content,
    }
    if msgtype := non_text_msgtype(data["content"]):
        data["msgtype"] = msgtype
    return data


async def extract_edit_body(
    event_source: dict[str, Any],
    client: nio.AsyncClient | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Extract body/content from an edit event's ``m.new_content`` payload."""
    content = _normalized_content_dict(event_source.get("content", {}))
    resolved_content = await _resolve_canonical_content(content, client)
    new_content = _normalized_content_dict(resolved_content.get("m.new_content"))

    body = new_content.get("body")
    if not isinstance(body, str):
        return None, None
    return body, dict(new_content)


async def resolve_event_source_content(
    event_source: dict[str, Any],
    client: nio.AsyncClient | None = None,
) -> dict[str, Any]:
    """Return an event source with canonical v2 sidecar content hydrated when available."""
    preview_content = _normalized_content_dict(event_source.get("content", {}))
    resolved_content = await _resolve_canonical_content(preview_content, client)
    if resolved_content is preview_content:
        return event_source

    resolved_event_source = {key: value for key, value in event_source.items() if isinstance(key, str)}
    resolved_event_source["content"] = resolved_content
    return resolved_event_source


async def _resolve_canonical_content(
    content: dict[str, Any],
    client: nio.AsyncClient | None,
) -> dict[str, Any]:
    """Hydrate canonical event content from a v2 JSON sidecar when available."""
    sidecar_content = _sidecar_content_for_resolution(content)
    if client is None or sidecar_content is None:
        return content

    long_text_meta = sidecar_content.get("io.mindroom.long_text")
    long_text_version = long_text_meta.get("version") if isinstance(long_text_meta, dict) else None
    mxc_url = _sidecar_mxc_url(sidecar_content) if long_text_version == 2 else None
    if mxc_url is None:
        return content

    full_text = await _download_mxc_text(
        client,
        mxc_url,
        sidecar_content.get("file") if isinstance(sidecar_content.get("file"), dict) else None,
    )
    if full_text is None:
        return content

    resolved_content = _extract_large_message_v2_content(full_text)
    if resolved_content is None:
        logger.warning("Invalid large-message v2 payload JSON, returning preview content")
        return content

    return resolved_content


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
