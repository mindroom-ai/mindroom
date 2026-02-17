"""Image message handler for downloading and decrypting Matrix images."""

from __future__ import annotations

import nio
from agno.media import Image
from nio import crypto

from .logging_config import get_logger

logger = get_logger(__name__)


def extract_caption(event: nio.RoomMessageImage | nio.RoomEncryptedImage) -> str:
    """Extract user caption from an image event using MSC2530 semantics.

    Per the Matrix spec (MSC2530): when a ``filename`` field is present in the
    event content and differs from ``body``, ``body`` is a user-provided
    caption.  Otherwise ``body`` is just the filename.

    Returns:
        The caption text, or ``"[Attached image]"`` when no caption was provided.

    """
    content = event.source.get("content", {})
    filename = content.get("filename")
    body = event.body
    if filename and filename != body and body:
        return body
    return "[Attached image]"


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
    try:
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

        # Prefer Matrix-provided mimetype metadata and keep it unset when absent.
        # For encrypted images, nio parses info.mimetype -> file.mimetype into
        # event.mimetype; for unencrypted images we read content.info.mimetype.
        if isinstance(event, nio.RoomEncryptedImage):
            mime_type = event.mimetype
        else:
            mime_type = event.source.get("content", {}).get("info", {}).get("mimetype")
        return Image(content=image_bytes, mime_type=mime_type)
    except Exception:
        logger.exception("Error downloading image")
    return None
