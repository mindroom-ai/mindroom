"""Matrix presence and status message utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio

from mindroom.constants import ENABLE_STREAMING, ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config import Config

logger = get_logger(__name__)


async def set_presence_status(
    client: nio.AsyncClient,
    status_msg: str,
    presence: str = "online",
) -> None:
    """Set presence status for a Matrix user.

    Args:
        client: The Matrix client
        status_msg: The status message to display
        presence: The presence state (online, offline, unavailable)

    """
    response = await client.set_presence(presence, status_msg)

    if isinstance(response, nio.PresenceSetResponse):
        logger.info(f"Set presence status: {status_msg}")
    else:
        logger.warning(f"Failed to set presence: {response}")


def build_agent_status_message(
    agent_name: str,
    config: Config,
) -> str:
    """Build status message with model and role information for an agent.

    Args:
        agent_name: Name of the agent
        config: Application configuration

    Returns:
        Status message string, limited to 250 characters

    """
    status_parts = []

    # Get model name using the config method
    model_name = config.get_entity_model_name(agent_name)

    # Format model info
    if model_name in config.models:
        model_config = config.models[model_name]
        model_info = f"{model_config.provider}/{model_config.id}"
    else:
        model_info = model_name

    status_parts.append(f"🤖 Model: {model_info}")

    # Add role/purpose for teams and agents
    if agent_name == ROUTER_AGENT_NAME:
        status_parts.append("📍 Routes messages to appropriate agents")
    elif agent_name in config.teams:
        team_config = config.teams[agent_name]
        if team_config.role:
            status_parts.append(f"👥 {team_config.role[:100]}")  # Limit length
        status_parts.append(f"🤝 Team: {', '.join(team_config.agents[:5])}")  # Show first 5 agents
    elif agent_name in config.agents:
        agent_config = config.agents[agent_name]
        if agent_config.role:
            status_parts.append(f"💼 {agent_config.role[:100]}")  # Limit length
        # Add tool count
        if agent_config.tools:
            status_parts.append(f"🔧 {len(agent_config.tools)} tools available")

    # Join all parts with separators
    return " | ".join(status_parts)


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
