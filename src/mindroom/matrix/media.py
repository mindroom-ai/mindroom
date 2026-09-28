"""Matrix media transport helpers shared across handlers."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeGuard
from urllib.parse import urlsplit

import nio
from aiohttp import ClientConnectionError, ClientResponse
from nio import crypto
from nio.api import MATRIX_MEDIA_API_PATH
from nio.http import TransportResponse

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes
from mindroom.logging_config import get_logger
from mindroom.matrix.encrypted_file import encrypted_file_content

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
_MATRIX_MEDIA_MSGTYPES = frozenset({"m.image", "m.audio", "m.video", "m.file"})
_matrix_media_max_bytes = 64 * 1024 * 1024
_AVATAR_MAX_BYTES = 1024 * 1024
_AVATAR_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_MXC_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_MXC_DOWNLOAD_ATTEMPTS = 3
# nio waits this long after a rate limit that names no delay.
_MXC_RATE_LIMIT_DEFAULT_WAIT_SECONDS = 5
_MXC_RATE_LIMIT_MAX_WAIT_SECONDS = 30
_MXC_CONNECTION_RETRY_WAIT_SECONDS = 1
_MXC_DOWNLOAD_BASE_SECONDS = 30
_MXC_DOWNLOAD_MIN_BYTES_PER_SECOND = 256 * 1024
_MEDIA_INTRINSIC_FAILURE_STATUSES = frozenset({404, 413})
_EXIF_ORIENTATION_TAG = 274
_EXIF_ROTATED_ORIENTATIONS = frozenset({5, 6, 7, 8})
_HEADER_DIMENSION_IMAGE_FORMATS = ("PNG", "JPEG", "GIF", "WEBP")


class MatrixMediaUpstreamError(RuntimeError):
    """A Matrix profile or thumbnail request failed upstream."""


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


def is_encrypted_media_event_source(event_source: Mapping[str, Any]) -> bool:
    """Return whether one event source contains standard encrypted media."""
    content = event_source.get("content")
    return (
        event_source.get("type") == "m.room.message"
        and isinstance(content, Mapping)
        and content.get("msgtype") in _MATRIX_MEDIA_MSGTYPES
        and "file" in content
    )


def parse_matrix_media_event_source(
    event_source: Mapping[str, Any],
) -> MatrixMediaEvent | nio.BadEvent | None:
    """Parse one Matrix event source through nio's correct media validation path."""
    normalized_source = {key: value for key, value in event_source.items() if isinstance(key, str)}
    try:
        parsed_event = (
            nio.RoomMessage.parse_decrypted_event(normalized_source)
            if is_encrypted_media_event_source(normalized_source)
            else nio.RoomMessage.parse_event(normalized_source)
        )
    except Exception:
        return None
    return parsed_event if isinstance(parsed_event, (*MATRIX_MEDIA_EVENT_TYPES, nio.BadEvent)) else None


def parse_matrix_media_dispatch_event_source(
    event_source: Mapping[str, Any],
) -> MatrixMediaDispatchEvent | None:
    """Parse one Matrix event source into image/file/video media when possible."""
    parsed_event = parse_matrix_media_event_source(event_source)
    return parsed_event if is_matrix_media_dispatch_event(parsed_event) else None


def upload_content_uri(upload_result: object) -> str | None:
    """Return the MXC URI from a direct or tuple-shaped nio upload response."""
    upload_response = upload_result[0] if isinstance(upload_result, tuple) else upload_result
    if isinstance(upload_response, nio.UploadResponse) and upload_response.content_uri:
        return str(upload_response.content_uri)
    return None


def _is_upstream_matrix_error(response: nio.ErrorResponse) -> bool:
    if response.status_code == "M_NOT_FOUND":
        return False
    if response.status_code is not None:
        return True
    if isinstance(response.transport_response, TransportResponse):
        http_status = response.transport_response.status_code
    elif isinstance(response.transport_response, ClientResponse):
        http_status = response.transport_response.status
    else:
        http_status = None
    return http_status == 429 or (http_status is not None and http_status >= 500)


def matrix_profile_avatar_uri(response: object) -> str | None:
    """Return a profile avatar URI while preserving typed Matrix failures."""
    if isinstance(response, nio.ProfileGetResponse):
        return response.avatar_url
    if isinstance(response, nio.ProfileGetError) and _is_upstream_matrix_error(response):
        raise MatrixMediaUpstreamError
    return None


async def fetch_matrix_thumbnail(
    client: nio.AsyncClient,
    mxc_uri: object,
) -> tuple[bytes, str] | None:
    """Fetch one bounded raster thumbnail from a validated Matrix content URI."""
    if not isinstance(mxc_uri, str):
        return None
    try:
        uri = urlsplit(mxc_uri)
    except ValueError:
        return None
    if (
        uri.scheme != "mxc"
        or not uri.netloc
        or not uri.path.strip("/")
        or uri.path.count("/") != 1
        or uri.query
        or uri.fragment
    ):
        return None
    thumbnail = await client.thumbnail(uri.netloc, uri.path[1:], width=96, height=96)
    if isinstance(thumbnail, nio.ThumbnailError):
        if _is_upstream_matrix_error(thumbnail):
            raise MatrixMediaUpstreamError
        return None
    if (
        not isinstance(thumbnail, nio.ThumbnailResponse)
        or not isinstance(thumbnail.body, bytes)
        or not 0 < len(thumbnail.body) <= _AVATAR_MAX_BYTES
        or thumbnail.content_type not in _AVATAR_MIME_TYPES
    ):
        return None
    return thumbnail.body, thumbnail.content_type


def media_size_exceeds_limit(size_bytes: int) -> bool:
    """Return whether a media size exceeds the runtime ingestion cap."""
    return size_bytes > _matrix_media_max_bytes


def media_payload_exceeds_limit(media_bytes: bytes | None) -> bool:
    """Return whether a Matrix media payload exceeds the runtime ingestion cap."""
    return media_bytes is not None and media_size_exceeds_limit(len(media_bytes))


@dataclass(frozen=True, slots=True)
class _PreparedMediaUpload:
    """Upload bytes and metadata after the caller has resolved room encryption."""

    data: bytes
    content_type: str
    filename: str
    info: dict[str, Any]
    encryption_keys: dict[str, Any] | None

    def encrypted_file_content(self) -> dict[str, Any] | None:
        """Build encrypted metadata separately so callers retain their error boundaries."""
        if self.encryption_keys is None:
            return None
        return encrypted_file_content(
            url="",
            key=self.encryption_keys["key"],
            iv=self.encryption_keys["iv"],
            hashes=self.encryption_keys["hashes"],
            mime_type=self.info["mimetype"],
            size=self.info["size"],
        )


def _image_dimensions(media_bytes: bytes, mimetype: str) -> dict[str, int]:
    """Return displayed image dimensions so clients size previews instead of cropping them."""
    if not mimetype.startswith("image/"):
        return {}
    # Keep the image decoder lazy during slim worker startup.
    from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

    try:
        # Some formats, such as ICO, decode the whole raster while opening, so only header-parsing formats are opened.
        with Image.open(io.BytesIO(media_bytes), formats=_HEADER_DIMENSION_IMAGE_FORMATS) as image:
            width, height = image.size
            # The base getexif() reads EXIF parsed with the header; the PNG override decodes the whole raster
            # to look for an eXIf chunk after the image data.
            orientation = Image.Image.getexif(image).get(_EXIF_ORIENTATION_TAG)
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError):
        return {}
    if orientation in _EXIF_ROTATED_ORIENTATIONS:
        width, height = height, width
    return {"w": width, "h": height}


def prepare_media_upload(
    media_bytes: bytes,
    *,
    filename: str,
    mimetype: str,
    encrypt: bool,
) -> _PreparedMediaUpload:
    """Prepare media without discovering room state, uploading, or handling failures."""
    upload_bytes, encryption_keys = (
        crypto.attachments.encrypt_attachment(media_bytes) if encrypt else (media_bytes, None)
    )
    return _PreparedMediaUpload(
        data=upload_bytes,
        content_type="application/octet-stream" if encrypt else mimetype,
        filename=f"{filename}.enc" if encrypt else filename,
        info={"size": len(media_bytes), "mimetype": mimetype, **_image_dimensions(media_bytes, mimetype)},
        encryption_keys=encryption_keys,
    )


async def upload_media_bytes(
    client: nio.AsyncClient,
    upload_bytes: bytes,
    *,
    content_type: str,
    filename: str,
) -> tuple[nio.UploadResponse | nio.UploadError, dict[str, object] | None]:
    """Upload an in-memory byte payload through nio's callback-based upload API."""

    def data_provider(_monitor: object, _data: object) -> io.BytesIO:
        return io.BytesIO(upload_bytes)

    return await client.upload(
        data_provider=data_provider,
        content_type=content_type,
        filename=filename,
        filesize=len(upload_bytes),
    )


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


def _media_payload_exceeds_limit_for_event(
    event: nio.RoomMessageMedia | nio.RoomEncryptedMedia,
    media_bytes: bytes,
    *,
    stage: str,
) -> bool:
    if not media_payload_exceeds_limit(media_bytes):
        return False
    logger.warning(
        "Matrix media payload exceeds byte limit",
        event_id=_event_id_for_log(event),
        stage=stage,
        size_bytes=len(media_bytes),
        limit_bytes=_matrix_media_max_bytes,
    )
    return True


def _decrypt_validated_media_bytes(
    event: nio.RoomEncryptedMedia,
    encrypted_bytes: bytes,
) -> bytes | None:
    decrypted_bytes = _decrypt_encrypted_media_bytes(event, encrypted_bytes)
    if decrypted_bytes is None:
        return None
    if _media_payload_exceeds_limit_for_event(event, decrypted_bytes, stage="decrypt"):
        return None
    return decrypted_bytes


def _mxc_media_path(mxc_url: str) -> str | None:
    """Return the authenticated media download path for one ``mxc://server/media_id`` URI."""
    server_name, separator, media_id = mxc_url.removeprefix("mxc://").partition("/")
    if not mxc_url.startswith("mxc://") or not separator or not server_name or not media_id or "/" in media_id:
        return None
    return nio.Api._build_path(["download", server_name, media_id], {"allow_remote": "true"}, MATRIX_MEDIA_API_PATH)


def _rate_limit_wait_seconds(response: ClientResponse) -> int:
    retry_after = response.headers.get("Retry-After", "")
    wait_seconds = int(retry_after) if retry_after.isdecimal() else _MXC_RATE_LIMIT_DEFAULT_WAIT_SECONDS
    return min(wait_seconds, _MXC_RATE_LIMIT_MAX_WAIT_SECONDS)


def _log_media_over_limit(mxc_url: str, *, stage: str, size_bytes: int | None, max_bytes: int) -> None:
    logger.warning(
        "matrix_media_exceeds_byte_limit",
        mxc_url=mxc_url,
        stage=stage,
        size_bytes=size_bytes,
        limit_bytes=max_bytes,
    )


@dataclass(frozen=True, slots=True)
class _MxcFetched:
    """A whole MXC payload within the caller's byte limit."""

    data: bytes


@dataclass(frozen=True, slots=True)
class MxcUnavailable:
    """An MXC payload that could not be read, and whether that is a fact about the media itself.

    Permanent means the media is missing, too large for the caller, or its URI is invalid, which no retry changes.
    """

    permanent: bool


type _MxcDownload = _MxcFetched | MxcUnavailable


def _unavailable_for_status(mxc_url: str, status: int) -> MxcUnavailable:
    # Only 404 M_NOT_FOUND and 413 M_TOO_LARGE answer for the media itself; any other status, such as a rate limit,
    # an expired token, a refusal, or a server error, can clear on a later attempt.
    permanent = status in _MEDIA_INTRINSIC_FAILURE_STATUSES
    logger.warning("matrix_media_download_failed", mxc_url=mxc_url, http_status=status, permanent=permanent)
    return MxcUnavailable(permanent=permanent)


async def _read_bounded_media_body(mxc_url: str, response: ClientResponse, max_bytes: int) -> _MxcDownload:
    if response.status != 200:
        return _unavailable_for_status(mxc_url, response.status)
    if response.content_length is not None and response.content_length > max_bytes:
        _log_media_over_limit(mxc_url, stage="declared", size_bytes=response.content_length, max_bytes=max_bytes)
        return MxcUnavailable(permanent=True)
    try:
        data = await collect_bounded_bytes(
            response.content.iter_chunked(_MXC_DOWNLOAD_CHUNK_BYTES),
            max_bytes=max_bytes,
        )
    except ByteLimitExceededError:
        _log_media_over_limit(mxc_url, stage="download", size_bytes=None, max_bytes=max_bytes)
        return MxcUnavailable(permanent=True)
    return _MxcFetched(data)


def _download_timeout_seconds(max_bytes: int) -> float:
    """Return a whole-transfer deadline that still lets a slow but steady link deliver ``max_bytes``."""
    return _MXC_DOWNLOAD_BASE_SECONDS + max_bytes / _MXC_DOWNLOAD_MIN_BYTES_PER_SECOND


async def download_bounded_mxc_bytes(client: nio.AsyncClient, mxc_url: str, *, max_bytes: int) -> _MxcDownload:
    """Download one MXC payload without buffering more than ``max_bytes``.

    nio's download reads the whole body before a caller can check its size, and any room member
    can point an event at media as large as the homeserver allows.
    Like nio's request loop, rate limits, lost connections, and timeouts are retried, here a bounded number of times,
    and each attempt must finish within a deadline scaled to ``max_bytes``.
    """
    path = _mxc_media_path(mxc_url)
    if path is None:
        logger.warning("invalid_mxc_url", mxc_url=mxc_url)
        return MxcUnavailable(permanent=True)
    headers = {"Accept-Encoding": "identity"}
    if client.access_token:
        headers["Authorization"] = f"Bearer {client.access_token}"
    timeout = _download_timeout_seconds(max_bytes)
    attempt = 1
    while True:
        try:
            response = await client.send("GET", path, headers=headers, timeout=timeout)
            try:
                if response.status != 429 or attempt == _MXC_DOWNLOAD_ATTEMPTS:
                    return await _read_bounded_media_body(mxc_url, response, max_bytes)
                wait_seconds = _rate_limit_wait_seconds(response)
            finally:
                response.release()
        except (ClientConnectionError, TimeoutError):
            if attempt == _MXC_DOWNLOAD_ATTEMPTS:
                logger.warning("matrix_media_download_interrupted", mxc_url=mxc_url, attempts=attempt)
                return MxcUnavailable(permanent=False)
            wait_seconds = _MXC_CONNECTION_RETRY_WAIT_SECONDS
        attempt += 1
        await asyncio.sleep(wait_seconds)


async def download_media_bytes(
    client: nio.AsyncClient,
    event: nio.RoomMessageMedia | nio.RoomEncryptedMedia,
) -> bytes | None:
    """Download and decrypt Matrix media payload bytes."""
    try:
        downloaded = await download_bounded_mxc_bytes(client, event.url, max_bytes=_matrix_media_max_bytes)
    except Exception:
        logger.exception("Error downloading media", event_id=_event_id_for_log(event))
        return None
    if not isinstance(downloaded, _MxcFetched):
        return None
    downloaded_bytes = downloaded.data

    if isinstance(event, nio.RoomEncryptedMedia):
        return _decrypt_validated_media_bytes(event, downloaded_bytes)
    return downloaded_bytes
