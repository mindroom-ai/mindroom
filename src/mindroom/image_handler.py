"""Image message handler for downloading and decrypting Matrix images."""

from __future__ import annotations

import nio
from agno.media import Image
from nio import crypto

from .logging_config import get_logger

logger = get_logger(__name__)

_IMAGE_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".bmp",
        ".tiff",
        ".tif",
        ".heic",
        ".heif",
        ".avif",
    },
)


def is_filename(body: str) -> bool:
    """Check if body looks like a bare filename rather than a user caption."""
    dot_idx = body.rfind(".")
    if dot_idx == -1:
        return False
    ext = body[dot_idx:].lower()
    return ext in _IMAGE_EXTENSIONS


async def download_image(
    client: nio.AsyncClient,
    event: nio.RoomMessageImage | nio.RoomEncryptedImage,
) -> Image | None:
    """Download image from Matrix, returning an agno Image or None.

    Handles both unencrypted and encrypted images. For encrypted images,
    decrypts using the key material in the event source.

    Args:
        client: Matrix client
        event: Image event (encrypted or unencrypted)

    Returns:
        agno Image object or None if download failed

    """
    mxc = event.url
    response = await client.download(mxc)
    if isinstance(response, nio.DownloadError):
        logger.error(f"Image download failed: {response}")
        return None

    if isinstance(event, nio.RoomMessageImage):
        image_bytes = response.body
    else:
        # Decrypt the image (same pattern as voice_handler._download_audio).
        # Return None on malformed payloads to match this function's contract.
        try:
            key = event.source["content"]["file"]["key"]["k"]
            sha256 = event.source["content"]["file"]["hashes"]["sha256"]
            iv = event.source["content"]["file"]["iv"]
        except (KeyError, TypeError):
            logger.exception(
                "Encrypted image payload missing decryption fields",
                event_id=getattr(event, "event_id", None),
            )
            return None

        try:
            image_bytes = crypto.attachments.decrypt_attachment(response.body, key, sha256, iv)
        except Exception:
            logger.exception("Image decryption failed")
            return None

    mime_type = event.source.get("content", {}).get("info", {}).get("mimetype", "image/png")
    return Image(content=image_bytes, mime_type=mime_type)
