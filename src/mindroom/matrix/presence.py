"""Matrix presence and status message utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config import Config

logger = get_logger(__name__)


async def set_presence_status(
    client: nio.AsyncClient,
    status_msg: str,
    presence: str = "online",
) -> bool:
    """Set presence status for a Matrix user.

    Args:
        client: The Matrix client
        status_msg: The status message to display
        presence: The presence state (online, offline, unavailable)

    Returns:
        True if successful, False otherwise

    """
    response = await client.set_presence(presence, status_msg)

    if isinstance(response, nio.PresenceSetResponse):
        logger.info(f"Set presence status: {status_msg}")
        return True

    logger.warning(f"Failed to set presence: {response}")
    return False


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
    status_msg = " | ".join(status_parts)

    # Limit total length to avoid API limits (usually 256 chars)
    if len(status_msg) > 250:
        status_msg = status_msg[:247] + "..."

    return status_msg
