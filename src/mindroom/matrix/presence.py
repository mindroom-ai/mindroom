"""Matrix user presence detection utilities."""

from __future__ import annotations

import nio

from mindroom.constants import ENABLE_STREAMING
from mindroom.logging_config import get_logger

logger = get_logger(__name__)


async def is_user_online(
    client: nio.AsyncClient,
    user_id: str,
) -> bool:
    """Check if a Matrix user is currently online.

    Args:
        client: The Matrix client to use for the presence check
        user_id: The Matrix user ID string (e.g., "@user:example.com")

    Returns:
        True if the user is online or unavailable (active but busy),
        False if offline or presence check fails

    """
    try:
        response = await client.get_presence(user_id)

        # Check if we got an error response
        if isinstance(response, nio.PresenceGetError):
            logger.warning(
                "Presence API error",
                user_id=user_id,
                error=response.message,
            )
            return False

        # Presence states: "online", "unavailable" (busy/idle), "offline"
        # We consider both "online" and "unavailable" as "online" for streaming purposes
        # since "unavailable" usually means the user is idle but still has the client open
        is_online = response.presence in ("online", "unavailable")

        logger.debug(
            "User presence check",
            user_id=user_id,
            presence=response.presence,
            is_online=is_online,
            last_active_ago=response.last_active_ago,
        )

        return is_online  # noqa: TRY300

    except Exception:
        logger.exception(
            "Error checking user presence",
            user_id=user_id,
        )
        # Default to non-streaming on error (safer)
        return False


async def should_use_streaming(
    client: nio.AsyncClient,
    room_id: str,
    requester_user_id: str | None = None,
) -> bool:
    """Determine if streaming should be used based on user presence.

    This checks if the human user who sent the message is online.
    If they are online, we use streaming (message editing) for real-time updates.
    If they are offline, we send the complete message at once to save API calls.

    Args:
        client: The Matrix client
        room_id: The room where the interaction is happening
        requester_user_id: The user who sent the message (optional)

    Returns:
        True if streaming should be used, False otherwise

    """
    # Check if streaming is globally disabled
    if not ENABLE_STREAMING:
        return False

    # If no requester specified, we can't check presence, default to streaming
    if not requester_user_id:
        logger.debug("No requester specified, defaulting to streaming")
        return True

    # Check if the requester is online
    is_online = await is_user_online(client, requester_user_id)

    logger.info(
        "Streaming decision",
        room_id=room_id,
        requester=requester_user_id,
        is_online=is_online,
        use_streaming=is_online,
    )

    return is_online
