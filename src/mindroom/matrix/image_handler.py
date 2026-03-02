"""Image message handler for downloading and decrypting Matrix images."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.media import Image

from mindroom.logging_config import get_logger
from mindroom.matrix.media import download_media_bytes, media_mime_type, resolve_image_mime_type

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
    mime_resolution = resolve_image_mime_type(image_bytes, media_mime_type(event))
    if mime_resolution.is_mismatch:
        event_id = event.event_id if hasattr(event, "event_id") and isinstance(event.event_id, str) else None
        logger.warning(
            "Image MIME mismatch between Matrix metadata and payload bytes",
            event_id=event_id,
            declared_mime_type=mime_resolution.declared_mime_type,
            detected_mime_type=mime_resolution.detected_mime_type,
        )
    return Image(content=image_bytes, mime_type=mime_resolution.effective_mime_type)
