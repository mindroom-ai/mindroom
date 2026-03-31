"""Interactive Q&A system using Matrix reactions as clickable buttons."""

from __future__ import annotations

import fcntl
import json
import re
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, cast

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import is_agent_id

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


class TextResponseEvent(Protocol):
    """Minimal normalized text-event shape used for interactive replies."""

    sender: str
    body: str
    source: dict[str, Any]


@dataclass(slots=True)
class _InteractiveQuestion:
    """Represents an active interactive question."""

    room_id: str
    thread_id: str | None
    options: dict[str, str]  # emoji/number -> value mapping
    creator_agent: str
    created_at: float = field(default_factory=time.time)


class _InteractiveResponse(NamedTuple):
    """Result of parsing and formatting an interactive response."""

    formatted_text: str
    option_map: dict[str, str] | None
    options_list: list[dict[str, str]] | None


# Track active interactive questions by event_id
_active_questions: dict[str, _InteractiveQuestion] = {}
_persistence_file: Path | None = None
_thread_lock = threading.RLock()

# Constants
# Match interactive code blocks
_INTERACTIVE_PATTERN = r"```(?:interactive\s*)?\n(?:interactive\s*\n)?(.*?)\n```"
_MAX_OPTIONS = 5
_DEFAULT_QUESTION = "Please choose an option:"
_INSTRUCTION_TEXT = "React with an emoji or type the number to respond."
_INTERACTIVE_TTL_SECONDS = 24 * 60 * 60


def _serialize_active_questions() -> dict[str, dict[str, object]]:
    """Return the JSON-serializable persistence payload."""
    return {event_id: asdict(question) for event_id, question in _active_questions.items()}


def _load_active_questions(payload: object) -> dict[str, _InteractiveQuestion]:
    """Deserialize persisted questions."""
    if not isinstance(payload, dict):
        msg = "Interactive question persistence payload must be an object"
        raise TypeError(msg)

    payload_dict = cast("dict[str, object]", payload)
    questions: dict[str, _InteractiveQuestion] = {}
    for event_id, raw_question in payload_dict.items():
        if not isinstance(event_id, str) or not isinstance(raw_question, dict):
            msg = "Interactive question record is invalid"
            raise TypeError(msg)
        question_data = cast("dict[str, object]", raw_question)
        raw_options = question_data["options"]
        if not isinstance(raw_options, dict):
            msg = "Interactive question options must be an object"
            raise TypeError(msg)
        raw_thread_id = question_data.get("thread_id")
        raw_created_at = question_data["created_at"]
        if not isinstance(raw_created_at, int | float | str):
            msg = "Interactive question timestamp is invalid"
            raise TypeError(msg)
        questions[event_id] = _InteractiveQuestion(
            room_id=str(question_data["room_id"]),
            thread_id=None if raw_thread_id is None else str(raw_thread_id),
            options={str(key): str(value) for key, value in cast("dict[object, object]", raw_options).items()},
            creator_agent=str(question_data["creator_agent"]),
            created_at=float(raw_created_at),
        )
    return questions


def _prune_expired_questions(questions: dict[str, _InteractiveQuestion]) -> dict[str, _InteractiveQuestion]:
    """Drop questions older than the persistence TTL."""
    current_time = time.time()
    return {
        event_id: question
        for event_id, question in questions.items()
        if current_time - question.created_at < _INTERACTIVE_TTL_SECONDS
    }


def _question_has_expired(question: _InteractiveQuestion) -> bool:
    """Return whether a question is older than the interactive TTL."""
    return time.time() - question.created_at >= _INTERACTIVE_TTL_SECONDS


def _save_active_questions_locked() -> None:
    """Persist active questions when persistence is enabled.

    This method must be called while holding ``_thread_lock``.
    """
    global _active_questions
    if _persistence_file is None:
        return

    try:
        _persistence_file.parent.mkdir(parents=True, exist_ok=True)
        _active_questions = _prune_expired_questions(_active_questions)
        with _persistence_file.open("a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                f.seek(0)
                f.truncate()
                json.dump(_serialize_active_questions(), f, indent=2)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning(
            "Failed to persist interactive questions; continuing in-memory",
            path=str(_persistence_file),
            error=str(exc),
        )


def init_persistence(storage_root: Path) -> None:
    """Initialize interactive question persistence from disk."""
    global _active_questions, _persistence_file
    persistence_file = storage_root / "tracking" / "interactive_questions.json"

    with _thread_lock:
        _persistence_file = persistence_file
        try:
            persistence_file.parent.mkdir(parents=True, exist_ok=True)
            with persistence_file.open("a+") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.seek(0)
                    raw_payload = f.read().strip()
                    loaded_questions = _load_active_questions(json.loads(raw_payload) if raw_payload else {})
                    _active_questions = _prune_expired_questions(loaded_questions)
                    f.seek(0)
                    f.truncate()
                    json.dump(_serialize_active_questions(), f, indent=2)
                    f.flush()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            _active_questions = {}
            logger.warning(
                "Failed to initialize interactive question persistence; continuing in-memory",
                path=str(persistence_file),
                error=str(exc),
            )


def should_create_interactive_question(response_text: str) -> bool:
    """Check if the response contains an interactive question in JSON format.

    Args:
        response_text: The AI's response text

    Returns:
        True if an interactive code block is found

    """
    return bool(re.search(_INTERACTIVE_PATTERN, response_text, re.DOTALL))


async def handle_reaction(
    client: nio.AsyncClient,
    event: nio.ReactionEvent,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[str, str | None] | None:
    """Handle a reaction event that might be an answer to a question.

    Args:
        client: The Matrix client
        event: The reaction event
        agent_name: The name of the agent handling this
        config: Application configuration
        runtime_paths: Explicit runtime context for agent detection

    Returns:
        Tuple of (selected_value, thread_id) if this was a valid response, None otherwise

    """
    with _thread_lock:
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

        if _question_has_expired(question):
            del _active_questions[event.reacts_to]
            _save_active_questions_locked()
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
        if reaction_key not in question.options or event.sender == client.user_id:
            return None

        # Ignore reactions from other agents
        if is_agent_id(event.sender, config, runtime_paths):
            logger.debug("Ignoring reaction from agent", sender=event.sender, reaction=reaction_key)
            return None

        selected_value = question.options[reaction_key]

        logger.info(
            "Received answer via reaction",
            user=event.sender,
            reaction=reaction_key,
            value=selected_value,
        )

        # The emoji reaction itself is the user's response, so just consume the question.
        with suppress(KeyError):
            del _active_questions[event.reacts_to]
        _save_active_questions_locked()

        return (selected_value, question.thread_id)


async def handle_text_response(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    event: TextResponseEvent,
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
    with _thread_lock:
        for question_event_id, question in list(_active_questions.items()):
            if question.room_id != room.room_id:
                continue
            if question.thread_id != thread_id:
                continue
            if _question_has_expired(question):
                del _active_questions[question_event_id]
                _save_active_questions_locked()
                continue
            if message_text not in question.options:
                continue
            if event.sender == client.user_id:
                continue
            # Only respond if this agent created the question
            if agent_name != question.creator_agent:
                continue

            selected_value = question.options[message_text]

            logger.info(
                "Received answer via text",
                user=event.sender,
                text=message_text,
                value=selected_value,
            )

            del _active_questions[question_event_id]
            _save_active_questions_locked()

            return (selected_value, question.thread_id)

    return None


def parse_and_format_interactive(response_text: str, extract_mapping: bool = False) -> _InteractiveResponse:
    """Parse and format interactive content from response text.

    Args:
        response_text: The response text containing interactive JSON
        extract_mapping: Whether to extract option mapping and return options list

    Returns:
        _InteractiveResponse with formatted_text, option_map, and options_list

    """
    # Find the first interactive block for processing
    first_match = re.search(_INTERACTIVE_PATTERN, response_text, re.DOTALL)

    if not first_match:
        return _InteractiveResponse(response_text, None, None)

    try:
        interactive_data = json.loads(first_match.group(1))
    except json.JSONDecodeError:
        return _InteractiveResponse(response_text, None, None)

    question = interactive_data.get("question", _DEFAULT_QUESTION)
    options = interactive_data.get("options", [])

    if not options:
        return _InteractiveResponse(response_text, None, None)

    options = options[:_MAX_OPTIONS]
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
    message_parts.append(_INSTRUCTION_TEXT)

    final_text = "\n".join(message_parts)

    return _InteractiveResponse(final_text, option_map, options if extract_mapping else None)


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
    with _thread_lock:
        _active_questions[event_id] = _InteractiveQuestion(
            room_id=room_id,
            thread_id=thread_id,
            options=option_map,
            creator_agent=agent_name,
        )
        _save_active_questions_locked()
    logger.info("Registered interactive question", event_id=event_id, options=len(option_map))


def clear_interactive_question(event_id: str) -> None:
    """Remove one tracked interactive question when its message is edited away."""
    with suppress(KeyError):
        del _active_questions[event_id]


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


def _cleanup() -> None:
    """Clean up when shutting down."""
    global _persistence_file
    with _thread_lock:
        _active_questions.clear()
        _persistence_file = None
