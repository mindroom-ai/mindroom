"""Command parsing and handling for user commands."""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

import nio

from .logging_config import get_logger

logger = get_logger(__name__)


class CommandType(Enum):
    """Types of commands supported."""

    INVITE = "invite"
    UNINVITE = "uninvite"
    LIST_INVITES = "list_invites"
    HELP = "help"


@dataclass
class Command:
    """Parsed command with arguments."""

    type: CommandType
    args: dict[str, Any]
    raw_text: str


class CommandParser:
    """Parser for user commands in messages."""

    # Command patterns
    # Match: /invite agent
    INVITE_PATTERN = re.compile(
        r"^/invite\s+@?(\w+)$",  # agent name
        re.IGNORECASE,
    )
    UNINVITE_PATTERN = re.compile(r"^/uninvite\s+@?(\w+)$", re.IGNORECASE)
    LIST_INVITES_PATTERN = re.compile(r"^/list[_-]?invites?$", re.IGNORECASE)
    HELP_PATTERN = re.compile(r"^/help(?:\s+(.+))?$", re.IGNORECASE)

    def parse(self, message: str) -> Command | None:
        """Parse a message for commands.

        Args:
            message: The message text to parse

        Returns:
            Parsed command or None if no command found
        """
        message = message.strip()
        if not message.startswith("/"):
            return None

        # Try to match each command pattern

        # /invite command
        match = self.INVITE_PATTERN.match(message)
        if match:
            agent_name = match.group(1)
            args = {
                "agent_name": agent_name,
            }
            return Command(
                type=CommandType.INVITE,
                args=args,
                raw_text=message,
            )

        # /uninvite command
        match = self.UNINVITE_PATTERN.match(message)
        if match:
            agent_name = match.group(1)
            return Command(
                type=CommandType.UNINVITE,
                args={"agent_name": agent_name},
                raw_text=message,
            )

        # /list_invites command
        if self.LIST_INVITES_PATTERN.match(message):
            return Command(
                type=CommandType.LIST_INVITES,
                args={},
                raw_text=message,
            )

        # /help command
        match = self.HELP_PATTERN.match(message)
        if match:
            topic = match.group(1)
            return Command(
                type=CommandType.HELP,
                args={"topic": topic},
                raw_text=message,
            )

        # Unknown command
        logger.debug(f"Unknown command: {message}")
        return None


def get_command_help(topic: str | None = None) -> str:
    """Get help text for commands.

    Args:
        topic: Specific topic to get help for (optional)

    Returns:
        Help text
    """
    if topic == "invite":
        return """**Invite Command**

Usage: `/invite <agent>` - Invite an agent to this thread

Example:
- `/invite calculator` - Invite calculator agent to this thread

Note: Invites only work in threads. The agent will be able to participate in this thread only.
Agents are automatically removed from the room 24 hours after being invited."""

    elif topic == "uninvite":
        return """**Uninvite Command**

Usage: `/uninvite <agent>`

Example:
- `/uninvite calculator` - Remove calculator agent from this thread

The agent will no longer receive messages from this thread."""

    elif topic == "list" or topic == "list_invites":
        return """**List Invites Command**

Usage: `/list_invites` or `/listinvites`

Shows all agents currently invited to this thread."""

    else:
        # General help
        return """**Available Commands**

- `/invite <agent>` - Invite an agent to this thread
- `/uninvite <agent>` - Remove an agent from this thread
- `/list_invites` - List all invited agents
- `/help [topic]` - Show this help or help for a specific command

Note: All commands only work within threads, not in main room messages.

For detailed help on a command, use: `/help <command>`"""


async def handle_invite_command(
    room_id: str,
    thread_id: str,
    agent_name: str,
    sender: str,
    agent_domain: str,
    client: nio.AsyncClient,
    thread_invite_manager: Any,
) -> str:
    """Handle the invite command to invite an agent to a thread."""
    from .agent_config import load_config
    from .matrix import construct_agent_user_id, get_room_members

    config = load_config()
    if agent_name not in config.agents:
        return f"❌ Unknown agent: @{agent_name}. Available agents: {', '.join(f'@{name}' for name in sorted(config.agents.keys()))}"

    # Add the thread invitation
    await thread_invite_manager.add_invite(thread_id, room_id, agent_name, sender)

    # Check if agent user exists in room
    agent_user_id = construct_agent_user_id(agent_name, agent_domain)
    room_members = await get_room_members(client, room_id)

    if isinstance(room_members, set):
        if agent_user_id not in room_members:
            # Invite the agent to the room (regular room invitation)
            invite_response = await client.room_invite(room_id, agent_user_id)
            if isinstance(invite_response, nio.RoomInviteResponse):
                logger.info("Invited agent to room", agent=agent_name, room_id=room_id)
            else:
                logger.error(
                    "Failed to invite agent to room", agent=agent_name, room_id=room_id, error=str(invite_response)
                )
    else:
        logger.error("Failed to get room members", room_id=room_id, error=str(room_members))

    response_text = f"✅ Invited @{agent_name} to this thread."
    response_text += f"\n\n@{agent_name}, you've been invited to help in this thread!"
    return response_text


async def handle_list_invites_command(
    room_id: str,
    thread_id: str,
    thread_invite_manager: Any,
) -> str:
    """Handle the list invites command."""
    thread_invites = await thread_invite_manager.get_thread_agents(thread_id, room_id)
    if thread_invites:
        thread_list = "\n".join([f"- @{agent}" for agent in thread_invites])
        return f"**Invited agents in this thread:**\n{thread_list}"

    return "No agents are currently invited to this thread."


# Global parser instance
command_parser = CommandParser()
