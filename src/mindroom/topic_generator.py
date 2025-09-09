"""Generate contextual topics for Matrix rooms using AI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio
from agno.agent import Agent
from pydantic import BaseModel, Field

from mindroom.ai import _cached_agent_run, get_model_instance
from mindroom.constants import STORAGE_PATH_OBJ
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config import Config

logger = get_logger(__name__)


class RoomTopic(BaseModel):
    """Structured room topic response."""

    topic: str = Field(description="The room topic - concise, informative, with emoji")


async def generate_room_topic_ai(room_key: str, room_name: str, config: Config) -> str | None:
    """Generate a contextual topic for a room using AI based on its purpose and configured agents.

    Args:
        room_key: The room key/alias (e.g., 'dev', 'analysis', 'lobby')
        room_name: Display name for the room
        config: Configuration with agent settings

    Returns:
        A contextual topic string for the room

    """
    # Get agents configured for this room
    agents_in_room = []
    for agent_name, agent_config in config.agents.items():
        if room_key in agent_config.rooms:
            display_name = agent_config.display_name or agent_name
            agents_in_room.append(display_name)

    # Build agent list for the prompt
    agent_list = ", ".join(agents_in_room)

    prompt = f"""Generate a concise, informative room topic for a MindRoom Matrix room.

Context about MindRoom:
MindRoom is a platform that frees AI agents from being trapped in single apps. Key features:
- AI agents with persistent memory that work across all platforms (Slack, Discord, Telegram, WhatsApp)
- Agents collaborate naturally in threads and remember everything across sessions
- Built on Matrix protocol for secure, federated communication
- 80+ integrations with tools like Gmail, GitHub, Spotify, Home Assistant
- Self-hosted or cloud options with military-grade encryption

Room details:
- Room key/alias: {room_key}
- Room name: {room_name}
- Configured agents: {agent_list if agent_list else "No specific agents configured yet"}

Create a topic that:
1. Describes the room's purpose based on its name
2. Mentions the AI agents or capabilities available
3. Highlights MindRoom's persistent memory or cross-platform nature when relevant
4. Is welcoming and informative
5. Uses 1-2 relevant emojis
6. Is under 100 characters
7. Follows this format: [emoji] [Description] • [Capabilities/Purpose]

Examples:
- 💻 Development Hub • AI agents that remember your code patterns across sessions
- 📊 Analysis Center • Persistent insights with cross-platform data access
- 🏠 Main Lobby • Your AI team headquarters with continuous memory
- 💰 Finance Room • AI agents tracking markets 24/7 with full context
- 🔬 Research Lab • Collaborative AI exploration with shared knowledge

Generate the topic:"""

    model = get_model_instance(config, "default")

    agent = Agent(
        name="TopicGenerator",
        role="Generate contextual room topics",
        model=model,
        response_model=RoomTopic,
    )

    session_id = f"topic_{room_key}"
    try:
        response = await _cached_agent_run(
            agent=agent,
            full_prompt=prompt,
            session_id=session_id,
            agent_name="TopicGenerator",
            storage_path=STORAGE_PATH_OBJ,
        )
    except Exception:
        logger.exception(f"Error generating topic for room {room_key}")
        return None
    content = response.content
    assert isinstance(content, RoomTopic)  # Type narrowing for mypy
    return content.topic


async def ensure_room_has_topic(
    client: nio.AsyncClient,
    room_id: str,
    room_key: str,
    room_name: str,
    config: Config,
) -> bool:
    """Ensure a room has a topic set, generating one if needed.

    Args:
        client: Matrix client
        room_id: The room ID
        room_key: The room key/alias
        room_name: Display name for the room
        config: Configuration with agent settings

    Returns:
        True if topic was set or already exists, False on error

    """
    # Check if room already has a topic
    response = await client.room_get_state_event(room_id, "m.room.topic")
    if isinstance(response, nio.RoomGetStateEventResponse) and response.content.get("topic"):
        logger.debug(f"Room {room_key} already has topic: {response.content['topic']}")
        return True

    # Generate and set topic
    logger.info(f"Generating AI topic for existing room {room_key}")
    topic = await generate_room_topic_ai(room_key, room_name, config)
    if topic is None:
        logger.warning(f"Failed to generate topic for room {room_key}")
        return False

    # Set the topic
    response = await client.room_put_state(
        room_id=room_id,
        event_type="m.room.topic",
        content={"topic": topic},
    )

    if isinstance(response, nio.RoomPutStateResponse):
        logger.info(f"Set topic for room {room_key}: {topic}")
        return True

    logger.warning(f"Failed to set topic for room {room_key}: {response}")
    return False
