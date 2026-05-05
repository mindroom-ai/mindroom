"""Matrix media transport helpers shared across handlers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard

import nio
from nio import crypto

from mindroom.logging_config import get_logger

logger = get_logger(__name__)


type ImageMessageEvent = nio.RoomMessageImage | nio.RoomEncryptedImage
type FileMessageEvent = nio.RoomMessageFile | nio.RoomEncryptedFile
type _VideoMessageEvent = nio.RoomMessageVideo | nio.RoomEncryptedVideo
type FileOrVideoMessageEvent = FileMessageEvent | _VideoMessageEvent
type AudioMessageEvent = nio.RoomMessageAudio | nio.RoomEncryptedAudio
type MatrixMediaDispatchEvent = ImageMessageEvent | FileOrVideoMessageEvent
type MatrixMediaEvent = MatrixMediaDispatchEvent | AudioMessageEvent

_IMAGE_MESSAGE_EVENT_TYPES = (nio.RoomMessageImage, nio.RoomEncryptedImage)
_FILE_MESSAGE_EVENT_TYPES = (nio.RoomMessageFile, nio.RoomEncryptedFile)
_VIDEO_MESSAGE_EVENT_TYPES = (nio.RoomMessageVideo, nio.RoomEncryptedVideo)
_FILE_OR_VIDEO_MESSAGE_EVENT_TYPES = (*_FILE_MESSAGE_EVENT_TYPES, *_VIDEO_MESSAGE_EVENT_TYPES)
_AUDIO_MESSAGE_EVENT_TYPES = (nio.RoomMessageAudio, nio.RoomEncryptedAudio)
_MATRIX_MEDIA_DISPATCH_EVENT_TYPES = (*_IMAGE_MESSAGE_EVENT_TYPES, *_FILE_OR_VIDEO_MESSAGE_EVENT_TYPES)
MATRIX_MEDIA_EVENT_TYPES = (*_MATRIX_MEDIA_DISPATCH_EVENT_TYPES, *_AUDIO_MESSAGE_EVENT_TYPES)


@dataclass(frozen=True)
class _ImageMimeResolution:
    """Resolved MIME metadata for image payload bytes."""

    effective_mime_type: str | None
    declared_mime_type: str | None
    detected_mime_type: str | None
    is_mismatch: bool


def is_image_message_event(event: object) -> TypeGuard[ImageMessageEvent]:
    """Return whether *event* is a Matrix image message."""
    return isinstance(event, _IMAGE_MESSAGE_EVENT_TYPES)


def is_file_message_event(event: object) -> TypeGuard[FileMessageEvent]:
    """Return whether *event* is a Matrix file message."""
    return isinstance(event, _FILE_MESSAGE_EVENT_TYPES)


def is_video_message_event(event: object) -> TypeGuard[_VideoMessageEvent]:
    """Return whether *event* is a Matrix video message."""
    return isinstance(event, _VIDEO_MESSAGE_EVENT_TYPES)


def is_file_or_video_message_event(event: object) -> TypeGuard[FileOrVideoMessageEvent]:
    """Return whether *event* is a Matrix file or video message."""
    return is_file_message_event(event) or is_video_message_event(event)


def is_audio_message_event(event: object) -> TypeGuard[AudioMessageEvent]:
    """Return whether *event* is a Matrix audio message."""
    return isinstance(event, _AUDIO_MESSAGE_EVENT_TYPES)


def is_matrix_media_dispatch_event(event: object) -> TypeGuard[MatrixMediaDispatchEvent]:
    """Return whether *event* is image, file, or video media."""
    return is_image_message_event(event) or is_file_or_video_message_event(event)


def parse_matrix_media_dispatch_event_source(
    event_source: Mapping[str, Any],
) -> MatrixMediaDispatchEvent | None:
    """Parse one Matrix event source into image/file/video media when possible."""
    normalized_source = {key: value for key, value in event_source.items() if isinstance(key, str)}
    content = normalized_source.get("content")
    try:
        parsed_event = (
            nio.RoomMessage.parse_decrypted_event(normalized_source)
            if isinstance(content, Mapping) and "file" in content
            else nio.RoomMessage.parse_event(normalized_source)
        )
    except Exception:
        return None
    return parsed_event if is_matrix_media_dispatch_event(parsed_event) else None


def _event_id_for_log(event: nio.RoomMessageMedia | nio.RoomEncryptedMedia) -> str | None:
    event_id = event.event_id
    return event_id if isinstance(event_id, str) else None


def media_mime_type(event: nio.RoomMessageMedia | nio.RoomEncryptedMedia) -> str | None:
    """Extract MIME type from Matrix media events."""
    if isinstance(event, nio.RoomEncryptedMedia):
        mimetype = event.mimetype
        if isinstance(mimetype, str) and mimetype:
            return mimetype

    source = event.source
    content = source.get("content", {}) if isinstance(source, dict) else {}
    info = content.get("info", {}) if isinstance(content, dict) else {}
    mimetype = info.get("mimetype") if isinstance(info, dict) else None
    return mimetype if isinstance(mimetype, str) and mimetype else None


def _sniff_image_mime_type(media_bytes: bytes | None) -> str | None:
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


def _normalize_mime_type(mime_type: str | None) -> str | None:
    if not isinstance(mime_type, str):
        return None
    normalized = mime_type.split(";", 1)[0].strip().lower()
    return normalized or None


def resolve_image_mime_type(media_bytes: bytes | None, declared_mime_type: str | None) -> _ImageMimeResolution:
    """Resolve effective image MIME type with byte-signature fallback."""
    normalized_declared = _normalize_mime_type(declared_mime_type)
    detected_mime_type = _sniff_image_mime_type(media_bytes)
    is_mismatch = (
        detected_mime_type is not None and normalized_declared is not None and detected_mime_type != normalized_declared
    )
    return _ImageMimeResolution(
        effective_mime_type=detected_mime_type or normalized_declared,
        declared_mime_type=normalized_declared,
        detected_mime_type=detected_mime_type,
        is_mismatch=is_mismatch,
    )


def extract_media_caption(
    event: nio.RoomMessageMedia | nio.RoomEncryptedMedia,
    *,
    default: str,
) -> str:
    """Extract user caption from Matrix media event content using MSC2530 semantics."""
    source = event.source
    content = source.get("content", {}) if isinstance(source, dict) else {}
    filename = content.get("filename")
    body = event.body
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
