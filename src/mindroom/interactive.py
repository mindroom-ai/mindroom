"""Interactive Q&A system using Matrix reactions as clickable buttons."""

from __future__ import annotations

import json
import re
from contextlib import suppress
from typing import TYPE_CHECKING, NamedTuple

import nio

from .logging_config import get_logger
from .matrix.event_info import EventInfo
from .matrix.identity import is_agent_id

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)


class InteractiveQuestion(NamedTuple):
    """Represents an active interactive question."""

    room_id: str
    thread_id: str | None
    options: dict[str, str]  # emoji/number -> value mapping
    creator_agent: str


class InteractiveResponse(NamedTuple):
    """Result of parsing and formatting an interactive response."""

    formatted_text: str
    option_map: dict[str, str] | None
    options_list: list[dict[str, str]] | None


# Track active interactive questions by event_id
_active_questions: dict[str, InteractiveQuestion] = {}

# Constants
# Match interactive code blocks
INTERACTIVE_PATTERN = r"```(?:interactive\s*)?\n(?:interactive\s*\n)?(.*?)\n```"
MAX_OPTIONS = 5
DEFAULT_QUESTION = "Please choose an option:"
INSTRUCTION_TEXT = "React with an emoji or type the number to respond."


def should_create_interactive_question(response_text: str) -> bool:
    """Check if the response contains an interactive question in JSON format.

    Args:
        response_text: The AI's response text

    Returns:
        True if an interactive code block is found

    """
    return bool(re.search(INTERACTIVE_PATTERN, response_text, re.DOTALL))


async def handle_reaction(
    client: nio.AsyncClient,
    event: nio.ReactionEvent,
    agent_name: str,
    config: Config,
) -> tuple[str, str | None] | None:
    """Handle a reaction event that might be an answer to a question.

    Args:
        client: The Matrix client
        event: The reaction event
        agent_name: The name of the agent handling this
        config: Application configuration

    Returns:
        Tuple of (selected_value, thread_id) if this was a valid response, None otherwise

    """
    question = _active_questions.get(event.reacts_to)
    if not question:
        logger.debug(
            "Reaction to unknown message",
            reacts_to=event.reacts_to,
            sender=event.sender,
            reaction=event.key,
            active_questions=list(_active_questions.keys()),
        )
        return None

    # Only the agent who created the question should respond to reactions
    if agent_name != question.creator_agent:
        logger.debug(
            "Ignoring reaction to question created by another agent",
            reacting_agent=agent_name,
            question_creator=question.creator_agent,
            reaction=event.key,
        )
        return None

    reaction_key = event.key
    if reaction_key not in question.options:
        return None

    # Don't process our own reactions
    if event.sender == client.user_id:
        return None

    # Ignore reactions from other agents
    if is_agent_id(event.sender, config):
        logger.debug("Ignoring reaction from agent", sender=event.sender, reaction=reaction_key)
        return None

    selected_value = question.options[reaction_key]

    logger.info(
        "Received answer via reaction",
        user=event.sender,
        reaction=reaction_key,
        value=selected_value,
    )

    # Store the response for the agent to process
    # The agent will continue the conversation based on this selection
    # No confirmation message needed - the emoji reaction itself is the user's response

    with suppress(KeyError):
        del _active_questions[event.reacts_to]

    # Return the selected value and thread_id so the agent can respond
    return (selected_value, question.thread_id)


async def handle_text_response(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    event: nio.RoomMessageText,
    agent_name: str,
) -> tuple[str, str | None] | None:
    """Handle text responses to interactive questions (e.g., "1", "2", "3").

    Args:
        client: The Matrix client
        room: The room the message occurred in
        event: The message event
        agent_name: The name of the agent handling this

    Returns:
        Tuple of (selected_value, thread_id) if this was a valid response, None otherwise

    """
    message_text = event.body.strip()

    # Look for numeric responses
    if not message_text.isdigit() or len(message_text) > 1:
        return None

    thread_info = EventInfo.from_event(event.source)
    thread_id = thread_info.thread_id

    # Find matching active questions in this room/thread
    for question_event_id, question in _active_questions.items():
        if question.room_id != room.room_id:
            continue
        if question.thread_id != thread_id:
            continue
        if message_text not in question.options:
            continue
        if event.sender == client.user_id:
            continue
        # Only respond if this agent created the question
        if agent_name != question.creator_agent:
            continue

        # Found a matching question
        selected_value = question.options[message_text]

        logger.info(
            "Received answer via text",
            user=event.sender,
            text=message_text,
            value=selected_value,
        )

        del _active_questions[question_event_id]

        return (selected_value, question.thread_id)

    return None


def parse_and_format_interactive(response_text: str, extract_mapping: bool = False) -> InteractiveResponse:
    """Parse and format interactive content from response text.

    Args:
        response_text: The response text containing interactive JSON
        extract_mapping: Whether to extract option mapping and return options list

    Returns:
        InteractiveResponse with formatted_text, option_map, and options_list

    """
    # Find the first interactive block for processing
    first_match = re.search(INTERACTIVE_PATTERN, response_text, re.DOTALL)

    if not first_match:
        return InteractiveResponse(response_text, None, None)

    try:
        interactive_data = json.loads(first_match.group(1))
    except json.JSONDecodeError:
        return InteractiveResponse(response_text, None, None)

    question = interactive_data.get("question", DEFAULT_QUESTION)
    options = interactive_data.get("options", [])

    if not options:
        return InteractiveResponse(response_text, None, None)

    options = options[:MAX_OPTIONS]
    clean_response = response_text.replace(first_match.group(0), "").strip()

    option_lines = []
    option_map: dict[str, str] | None = {} if extract_mapping else None

    for i, opt in enumerate(options, 1):
        emoji_char = opt.get("emoji", "❓")
        label = opt.get("label", "Option")
        option_lines.append(f"{i}. {emoji_char} {label}")

        if extract_mapping and option_map is not None:
            value = opt.get("value", label.lower())
            option_map[emoji_char] = value
            option_map[str(i)] = value

    # Combine everything into the final message
    message_parts = []
    if clean_response:
        message_parts.append(clean_response)
    message_parts.append("")  # Empty line
    message_parts.append(question)
    message_parts.append("")  # Empty line
    message_parts.extend(option_lines)
    message_parts.append("")  # Empty line
    message_parts.append(INSTRUCTION_TEXT)

    final_text = "\n".join(message_parts)

    return InteractiveResponse(final_text, option_map, options if extract_mapping else None)


def register_interactive_question(
    event_id: str,
    room_id: str,
    thread_id: str | None,
    option_map: dict[str, str],
    agent_name: str,
) -> None:
    """Register an interactive question for tracking.

    Args:
        event_id: The event ID of the message with the question
        room_id: The room ID
        thread_id: Thread ID if in a thread
        option_map: Mapping of emoji/number to values
        agent_name: The agent that created the question

    """
    _active_questions[event_id] = InteractiveQuestion(
        room_id=room_id,
        thread_id=thread_id,
        options=option_map,
        creator_agent=agent_name,
    )
    logger.info("Registered interactive question", event_id=event_id, options=len(option_map))


async def add_reaction_buttons(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    options: list[dict[str, str]],
) -> None:
    """Add reaction buttons to a message.

    Args:
        client: The Matrix client
        room_id: The room ID
        event_id: The event ID of the message to add reactions to
        options: List of option dictionaries with 'emoji' keys

    """
    for opt in options:
        emoji_char = opt.get("emoji", "❓")
        reaction_response = await client.room_send(
            room_id=room_id,
            message_type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event_id,
                    "key": emoji_char,
                },
            },
        )
        if not isinstance(reaction_response, nio.RoomSendResponse):
            logger.warning("Failed to add reaction", emoji=emoji_char, error=str(reaction_response))


def cleanup() -> None:
    """Clean up when shutting down."""
    _active_questions.clear()
