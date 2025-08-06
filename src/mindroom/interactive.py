"""Interactive Q&A system using Matrix reactions as clickable buttons."""

import json
import re
from contextlib import suppress

import nio

from .logging_config import get_logger
from .matrix import create_mention_content_from_text, extract_domain_from_user_id

logger = get_logger(__name__)

# Track active interactive questions: event_id -> (room_id, thread_id, options)
_active_questions: dict[str, tuple[str, str | None, dict[str, str]]] = {}


def should_create_interactive_question(response_text: str) -> bool:
    """Check if the response contains an interactive question in JSON format.

    Args:
        response_text: The AI's response text

    Returns:
        True if an interactive code block is found
    """
    # Check for both formats: ```interactive and ```\ninteractive
    return "```interactive" in response_text or "```\ninteractive" in response_text


async def handle_interactive_response(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    response_text: str,
    response_already_sent: bool = False,
) -> None:
    """Create an interactive question from JSON in the AI response.

    Args:
        client: The Matrix client
        room_id: The room ID
        thread_id: Thread ID if in a thread
        response_text: The AI's response containing an interactive code block
        response_already_sent: Whether the response text has already been sent (e.g., in streaming mode)
    """
    # Extract JSON from interactive code block
    # Handle both ```interactive and ```\ninteractive formats
    pattern = r"```(?:interactive\s*)?\n(?:interactive\s*\n)?(.*?)\n```"
    match = re.search(pattern, response_text, re.DOTALL)

    if not match:
        logger.warning("Interactive block found but couldn't extract JSON", response_preview=response_text[:200])
        # Send the original response anyway if not already sent
        if not response_already_sent:
            await _send_response_text(client, room_id, thread_id, response_text)
        return

    try:
        interactive_data = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        logger.error("Failed to parse interactive JSON", error=str(e))
        # Send the original response anyway if not already sent
        if not response_already_sent:
            await _send_response_text(client, room_id, thread_id, response_text)
        return

    # Send the original response first (without the interactive block) if not already sent
    if not response_already_sent:
        clean_response = response_text.replace(match.group(0), "").strip()
        if clean_response:
            await _send_response_text(client, room_id, thread_id, clean_response)

    # Create the interactive question
    question_event_id = await create_interactive_question(
        client=client,
        room_id=room_id,
        thread_id=thread_id,
        question=interactive_data.get("question", "Please choose an option:"),
        options=interactive_data.get("options", []),
    )

    if question_event_id:
        logger.info("Created interactive question from response", event_id=question_event_id)


async def create_interactive_question(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    question: str,
    options: list[dict[str, str]],
) -> str | None:
    """Send an interactive question with reaction options.

    Args:
        client: The Matrix client
        room_id: The room to send the question in
        thread_id: Thread ID if in a thread
        question: The question text
        options: List of option dicts with emoji, label, and value

    Returns:
        Event ID of the question message, or None if failed
    """
    if not options:
        logger.warning("No options provided for interactive question")
        return None

    if len(options) > 5:
        logger.warning("Too many options for interactive question", count=len(options))
        options = options[:5]

    # Build the message content
    message_lines = [question, ""]
    option_map = {}

    for i, opt in enumerate(options, 1):
        emoji_char = opt.get("emoji", "❓")
        label = opt.get("label", "Option")
        value = opt.get("value", label.lower())

        message_lines.append(f"{emoji_char} {label}")
        # Support both emoji and numeric responses
        option_map[emoji_char] = value
        option_map[str(i)] = value

    message_lines.extend(["", "React with an emoji or type the number to respond."])
    message_text = "\n".join(message_lines)

    # Send the message
    sender_domain = extract_domain_from_user_id(client.user_id)
    content = create_mention_content_from_text(
        message_text,
        sender_domain=sender_domain,
        thread_event_id=thread_id,
    )

    response = await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content,
    )

    if not isinstance(response, nio.RoomSendResponse):
        logger.error("Failed to send interactive question", error=str(response))
        return None

    question_event_id: str = response.event_id

    # Store the active question
    _active_questions[question_event_id] = (room_id, thread_id, option_map)

    # Add reaction buttons (bot pre-adds them for better UX)
    for opt in options:
        emoji_char = opt.get("emoji", "❓")
        reaction_response = await client.room_send(
            room_id=room_id,
            message_type="m.reaction",
            content={
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": question_event_id,
                    "key": emoji_char,
                }
            },
        )
        if not isinstance(reaction_response, nio.RoomSendResponse):
            logger.warning("Failed to add reaction", emoji=emoji_char, error=str(reaction_response))

    logger.info(
        "Sent interactive question",
        event_id=question_event_id,
        options=len(options),
    )

    return question_event_id


async def handle_reaction(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    event: nio.ReactionEvent,
) -> None:
    """Handle a reaction event that might be an answer to a question.

    Args:
        client: The Matrix client
        room: The room the reaction occurred in
        event: The reaction event
    """
    # Check if this reaction relates to an active question
    question_data = _active_questions.get(event.reacts_to)
    if not question_data:
        logger.debug(
            "Reaction to unknown message",
            reacts_to=event.reacts_to,
            sender=event.sender,
            reaction=event.key,
            active_questions=list(_active_questions.keys()),
        )
        return

    room_id, thread_id, option_map = question_data

    # Check if the reaction is one of our options
    reaction_key = event.key
    if reaction_key not in option_map:
        return

    # Don't process our own reactions
    if event.sender == client.user_id:
        return

    # Ignore reactions from other agents
    from .matrix.identity import is_agent_id

    if is_agent_id(event.sender):
        logger.debug("Ignoring reaction from agent", sender=event.sender, reaction=reaction_key)
        return

    # Get the value for this reaction
    selected_value = option_map[reaction_key]

    logger.info(
        "Received answer via reaction",
        user=event.sender,
        reaction=reaction_key,
        value=selected_value,
    )

    # Send confirmation
    await _send_confirmation(client, room_id, thread_id, event.reacts_to, reaction_key, selected_value)

    # Remove the question after successful response
    with suppress(KeyError):
        del _active_questions[event.reacts_to]


async def handle_text_response(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    event: nio.RoomMessageText,
) -> None:
    """Handle text responses to interactive questions (e.g., "1", "2", "3").

    Args:
        client: The Matrix client
        room: The room the message occurred in
        event: The message event
    """
    # Check if this might be a response to a question
    message_text = event.body.strip()

    # Look for numeric responses
    if not message_text.isdigit() or len(message_text) > 1:
        return

    # Extract thread info from the event
    is_thread = "m.relates_to" in event.source.get("content", {})
    thread_id = None
    if is_thread:
        relates_to = event.source["content"]["m.relates_to"]
        if relates_to.get("rel_type") == "m.thread":
            thread_id = relates_to.get("event_id")

    # Find matching active questions in this room/thread
    for question_event_id, (q_room_id, q_thread_id, option_map) in _active_questions.items():
        if q_room_id != room.room_id:
            continue
        if q_thread_id != thread_id:
            continue
        if message_text not in option_map:
            continue
        if event.sender == client.user_id:
            continue

        # Found a matching question
        selected_value = option_map[message_text]

        logger.info(
            "Received answer via text",
            user=event.sender,
            text=message_text,
            value=selected_value,
        )

        # Send confirmation
        await _send_confirmation(client, q_room_id, q_thread_id, question_event_id, message_text, selected_value)

        # Remove the question after successful response
        del _active_questions[question_event_id]
        break


async def _send_response_text(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    text: str,
) -> None:
    """Send a text response to the room."""
    sender_domain = extract_domain_from_user_id(client.user_id)
    content = create_mention_content_from_text(
        text,
        sender_domain=sender_domain,
        thread_event_id=thread_id,
    )

    await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content,
    )


async def _send_confirmation(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str | None,
    question_event_id: str,
    response: str,
    value: str,
) -> None:
    """Send a confirmation message after a user responds."""
    confirmation = f"✅ You selected: {value}"

    sender_domain = extract_domain_from_user_id(client.user_id)
    content = create_mention_content_from_text(
        confirmation,
        sender_domain=sender_domain,
        thread_event_id=thread_id,
        reply_to_event_id=question_event_id,
    )

    await client.room_send(
        room_id=room_id,
        message_type="m.room.message",
        content=content,
    )


def cleanup() -> None:
    """Clean up when shutting down."""
    _active_questions.clear()
