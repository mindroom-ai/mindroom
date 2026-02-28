"""Matrix avatar management helpers."""

import io
from pathlib import Path

import nio

from mindroom.logging_config import get_logger

logger = get_logger(__name__)


async def _upload_avatar_file(
    client: nio.AsyncClient,
    avatar_path: Path,
) -> str | None:
    """Upload an avatar file to the Matrix server.

    Args:
        client: Authenticated Matrix client
        avatar_path: Path to the avatar image file

    Returns:
        The content URI if successful, None otherwise

    """
    if not avatar_path.exists():
        logger.warning(f"Avatar file not found: {avatar_path}")
        return None

    extension = avatar_path.suffix.lower()
    content_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(extension, "image/png")

    with avatar_path.open("rb") as f:
        avatar_data = f.read()

    file_size = len(avatar_data)

    def data_provider(_upload_monitor: object, _unused_data: object) -> io.BytesIO:
        return io.BytesIO(avatar_data)

    upload_result = await client.upload(
        data_provider=data_provider,
        content_type=content_type,
        filename=avatar_path.name,
        filesize=file_size,
    )

    # nio returns tuple (response, error)
    if isinstance(upload_result, tuple):
        upload_response, error = upload_result
        if error:
            logger.error(f"Upload error: {error}")
            return None
    else:
        upload_response = upload_result

    if not isinstance(upload_response, nio.UploadResponse):
        logger.error(f"Failed to upload avatar: {upload_response}")
        return None

    if not upload_response.content_uri:
        logger.error("Upload response missing content_uri")
        return None

    return str(upload_response.content_uri)


async def set_avatar_from_file(
    client: nio.AsyncClient,
    avatar_path: Path,
) -> bool:
    """Set a user's avatar from a local file.

    Args:
        client: Authenticated Matrix client
        avatar_path: Path to the avatar image file

    Returns:
        True if successful, False otherwise

    """
    avatar_url = await _upload_avatar_file(client, avatar_path)
    if not avatar_url:
        return False

    response = await client.set_avatar(avatar_url)

    if isinstance(response, nio.ProfileSetAvatarResponse):
        logger.info(f"✅ Successfully set avatar for {client.user_id}")
        return True

    logger.error(f"Failed to set avatar for {client.user_id}: {response}")
    return False


async def check_and_set_avatar(
    client: nio.AsyncClient,
    avatar_path: Path,
    room_id: str | None = None,
) -> bool:
    """Check if user or room has an avatar and set it if they don't.

    Args:
        client: Authenticated Matrix client
        avatar_path: Path to the avatar image file
        room_id: Optional room ID for setting room avatar (if None, sets user avatar)

    Returns:
        True if avatar was already set or successfully set, False otherwise

    """
    if room_id:
        # Check room avatar
        response = await client.room_get_state_event(room_id, "m.room.avatar")
        if isinstance(response, nio.RoomGetStateEventResponse) and response.content and response.content.get("url"):
            logger.debug(f"Avatar already set for room {room_id}")
            return True
        # Set room avatar
        return await set_room_avatar_from_file(client, room_id, avatar_path)
    # Check user avatar
    response = await client.get_profile(client.user_id)
    if isinstance(response, nio.ProfileGetResponse) and response.avatar_url:
        logger.debug(f"Avatar already set for {client.user_id}")
        return True
    # Set user avatar
    return await set_avatar_from_file(client, avatar_path)


async def set_room_avatar_from_file(
    client: nio.AsyncClient,
    room_id: str,
    avatar_path: Path,
) -> bool:
    """Set the avatar for a Matrix room from a file.

    Args:
        client: Authenticated Matrix client
        room_id: The room ID to set the avatar for
        avatar_path: Path to the avatar image file

    Returns:
        True if avatar was successfully set, False otherwise

    """
    avatar_url = await _upload_avatar_file(client, avatar_path)
    if not avatar_url:
        return False

    # Set room avatar using room state
    response = await client.room_put_state(
        room_id=room_id,
        event_type="m.room.avatar",
        content={"url": avatar_url},
    )

    if isinstance(response, nio.RoomPutStateResponse):
        logger.info(f"✅ Successfully set avatar for room {room_id}")
        return True

    logger.error(f"Failed to set avatar for room {room_id}: {response}")
    return False
