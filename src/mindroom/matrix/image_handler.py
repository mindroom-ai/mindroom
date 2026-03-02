"""Image message handler for downloading and decrypting Matrix images."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.media import Image

from mindroom.logging_config import get_logger
from mindroom.matrix.media import download_media_bytes, media_mime_type, sniff_image_mime_type

if TYPE_CHECKING:
    import nio

logger = get_logger(__name__)


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
    image_bytes = await download_media_bytes(client, event)
    if image_bytes is None:
        return None
    declared_mime_type = media_mime_type(event)
    detected_mime_type = sniff_image_mime_type(image_bytes)
    if (
        detected_mime_type is not None
        and isinstance(declared_mime_type, str)
        and declared_mime_type.strip()
        and detected_mime_type != declared_mime_type.split(";", 1)[0].strip().lower()
    ):
        event_id = getattr(event, "event_id", None)
        logger.warning(
            "Image MIME mismatch between Matrix metadata and payload bytes",
            event_id=event_id if isinstance(event_id, str) else None,
            declared_mime_type=declared_mime_type,
            detected_mime_type=detected_mime_type,
        )
    return Image(content=image_bytes, mime_type=detected_mime_type or declared_mime_type)
