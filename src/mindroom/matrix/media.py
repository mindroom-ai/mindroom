"""Matrix media transport helpers shared across handlers."""

from __future__ import annotations

import nio
from nio import crypto

from mindroom.logging_config import get_logger

logger = get_logger(__name__)


def _event_id_for_log(event: nio.RoomMessageMedia | nio.RoomEncryptedMedia) -> str | None:
    event_id = getattr(event, "event_id", None)
    return event_id if isinstance(event_id, str) else None


def media_mime_type(event: nio.RoomMessageMedia | nio.RoomEncryptedMedia) -> str | None:
    """Extract MIME type from Matrix media events."""
    if isinstance(event, nio.RoomEncryptedMedia):
        mimetype = getattr(event, "mimetype", None)
        if isinstance(mimetype, str) and mimetype:
            return mimetype

    source = getattr(event, "source", {})
    content = source.get("content", {}) if isinstance(source, dict) else {}
    info = content.get("info", {}) if isinstance(content, dict) else {}
    mimetype = info.get("mimetype") if isinstance(info, dict) else None
    return mimetype if isinstance(mimetype, str) and mimetype else None


def sniff_image_mime_type(media_bytes: bytes | None) -> str | None:
    """Best-effort image MIME detection from file signatures."""
    if not media_bytes:
        return None
    mime_type: str | None = None
    if media_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        mime_type = "image/png"
    elif media_bytes.startswith(b"\xff\xd8\xff"):
        mime_type = "image/jpeg"
    elif media_bytes.startswith((b"GIF87a", b"GIF89a")):
        mime_type = "image/gif"
    elif len(media_bytes) >= 12 and media_bytes.startswith(b"RIFF") and media_bytes[8:12] == b"WEBP":
        mime_type = "image/webp"
    elif media_bytes.startswith(b"BM"):
        mime_type = "image/bmp"
    elif media_bytes.startswith((b"II*\x00", b"MM\x00*")):
        mime_type = "image/tiff"
    return mime_type


def extract_media_caption(
    event: nio.RoomMessageMedia | nio.RoomEncryptedMedia,
    *,
    default: str,
) -> str:
    """Extract user caption from Matrix media event content using MSC2530 semantics."""
    source = getattr(event, "source", {})
    content = source.get("content", {}) if isinstance(source, dict) else {}
    filename = content.get("filename")
    body = getattr(event, "body", None)
    if isinstance(filename, str) and filename and isinstance(body, str) and body and filename != body:
        return body
    return default


def _decrypt_encrypted_media_bytes(
    event: nio.RoomEncryptedMedia,
    encrypted_bytes: bytes,
) -> bytes | None:
    """Decrypt encrypted Matrix media payload bytes."""
    try:
        key = event.source["content"]["file"]["key"]["k"]
        sha256 = event.source["content"]["file"]["hashes"]["sha256"]
        iv = event.source["content"]["file"]["iv"]
    except (KeyError, TypeError):
        logger.exception("Encrypted media payload missing decryption fields", event_id=_event_id_for_log(event))
        return None

    try:
        return crypto.attachments.decrypt_attachment(encrypted_bytes, key, sha256, iv)
    except Exception:
        logger.exception("Media decryption failed", event_id=_event_id_for_log(event))
        return None


async def download_media_bytes(
    client: nio.AsyncClient,
    event: nio.RoomMessageMedia | nio.RoomEncryptedMedia,
) -> bytes | None:
    """Download and decrypt Matrix media payload bytes."""
    try:
        response = await client.download(event.url)
    except Exception:
        logger.exception("Error downloading media")
        return None

    if isinstance(response, nio.DownloadError):
        logger.error("Media download failed", event_id=_event_id_for_log(event), error=str(response))
        return None
    if not isinstance(response.body, bytes):
        logger.error("Media download returned non-bytes payload", event_id=_event_id_for_log(event))
        return None

    if isinstance(event, nio.RoomEncryptedMedia):
        return _decrypt_encrypted_media_bytes(event, response.body)
    return response.body
