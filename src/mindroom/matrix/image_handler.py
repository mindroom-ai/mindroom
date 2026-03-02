"""Image message handler for downloading and decrypting Matrix images."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.media import Image

from mindroom.logging_config import get_logger
from mindroom.matrix.media import download_media_bytes, media_mime_type

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
    return Image(content=image_bytes, mime_type=media_mime_type(event))
