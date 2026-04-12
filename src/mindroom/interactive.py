"""Interactive Q&A system using Matrix reactions as clickable buttons."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import threading
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, cast

import nio

from mindroom.logging_config import bound_log_context, get_logger
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import is_agent_id

if TYPE_CHECKING:
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


@dataclass(frozen=True, slots=True)
class InteractiveSelection:
    """One validated interactive question selection ready for execution."""

    question_event_id: str
    selection_key: str
    selected_value: str
    thread_id: str | None


# Track active interactive questions by event_id
_active_questions: dict[str, _InteractiveQuestion] = {}
_persistence_file: Path | None = None
_persistence_lock_file: Path | None = None
# _thread_lock protects the in-process dictionaries.
# The flock on _persistence_lock_file serializes JSON persistence across processes.
_thread_lock = threading.RLock()
_dirty_question_ids: set[str] = set()
_deleted_question_ids: set[str] = set()

# Constants
# Match interactive code blocks
_INTERACTIVE_MARKERS = frozenset({"interactive", "interactive json"})
_INTERACTIVE_PATTERN = (
    r"```[ \t]*(?:"
    r"interactive(?:[ \t]+json)?[ \t]*\r?\n"
    r"|"
    r"\r?\n[ \t]*interactive(?:[ \t]+json)?[ \t]*\r?\n"
    r")(.*?)\r?\n[ \t]*```[ \t]*(?=\r?\n|$)"
)
_INTERACTIVE_PATTERN_FLAGS = re.DOTALL | re.IGNORECASE
_MAX_OPTIONS = 5
_DEFAULT_QUESTION = "Please choose an option:"
_INSTRUCTION_TEXT = "React with an emoji or type the number to respond."


def _serialize_active_questions(questions: dict[str, _InteractiveQuestion]) -> dict[str, dict[str, object]]:
    """Return the JSON-serializable persistence payload."""
    return {event_id: asdict(question) for event_id, question in questions.items()}


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


def _load_persisted_questions() -> dict[str, _InteractiveQuestion]:
    """Read the persisted questions file."""
    if _persistence_file is None or not _persistence_file.exists():
        return {}
    raw_payload = _persistence_file.read_text().strip()
    return _load_active_questions(json.loads(raw_payload) if raw_payload else {})


def _write_active_questions_atomically_locked(questions: dict[str, _InteractiveQuestion]) -> None:
    """Atomically replace the persisted questions file.

    This method must be called while holding the cross-process persistence lock.
    """
    if _persistence_file is None:
        return

    _persistence_file.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_path = tempfile.mkstemp(
        dir=str(_persistence_file.parent),
        prefix=f".{_persistence_file.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
            json.dump(_serialize_active_questions(questions), temp_file, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        Path(temp_path).replace(_persistence_file)
        directory_fd = os.open(_persistence_file.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        with suppress(FileNotFoundError):
            Path(temp_path).unlink()
        raise


def _replace_active_questions_locked(questions: dict[str, _InteractiveQuestion]) -> None:
    """Replace in-memory state after a successful load or save."""
    global _active_questions
    _active_questions = questions
    _dirty_question_ids.clear()
    _deleted_question_ids.clear()


def _set_active_questions_locked(questions: dict[str, _InteractiveQuestion]) -> None:
    """Replace the in-memory snapshot without clearing pending local changes."""
    global _active_questions
    _active_questions = questions


def _store_active_question_locked(event_id: str, question: _InteractiveQuestion) -> None:
    """Record a new or updated active question."""
    _active_questions[event_id] = question
    _dirty_question_ids.add(event_id)
    _deleted_question_ids.discard(event_id)


def _remove_active_question_locked(event_id: str) -> bool:
    """Remove a tracked question and record the deletion for persistence."""
    if event_id not in _active_questions:
        return False
    del _active_questions[event_id]
    _dirty_question_ids.discard(event_id)
    _deleted_question_ids.add(event_id)
    return True


def _apply_local_changes_locked(
    questions: dict[str, _InteractiveQuestion],
) -> dict[str, _InteractiveQuestion]:
    """Overlay unsaved local additions and deletions onto a persisted snapshot."""
    merged_questions = dict(questions)
    for event_id in _deleted_question_ids:
        merged_questions.pop(event_id, None)
    for event_id in _dirty_question_ids:
        question = _active_questions.get(event_id)
        if question is None:
            merged_questions.pop(event_id, None)
            continue
        merged_questions[event_id] = question
    return merged_questions


def _refresh_active_questions_locked() -> None:
    """Refresh the in-memory snapshot from disk before answering interactive lookups."""
    if _persistence_file is None or _persistence_lock_file is None:
        return

    try:
        with _persistence_lock_file.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
            try:
                persisted_questions = _load_persisted_questions()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning(
            "Failed to refresh persisted interactive questions; continuing with in-memory snapshot",
            path=str(_persistence_file),
            error=str(exc),
        )
        return

    _set_active_questions_locked(_apply_local_changes_locked(persisted_questions))


def _save_active_questions_locked() -> None:
    """Persist active questions when persistence is enabled.

    This method must be called while holding ``_thread_lock``.
    """
    if _persistence_file is None or _persistence_lock_file is None:
        return

    try:
        _persistence_file.parent.mkdir(parents=True, exist_ok=True)
        with _persistence_lock_file.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    merged_questions = _apply_local_changes_locked(_load_persisted_questions())
                except Exception as exc:
                    merged_questions = dict(_active_questions)
                    logger.warning(
                        "Failed to read persisted interactive questions before save; rebuilding file from in-memory questions",
                        path=str(_persistence_file),
                        error=str(exc),
                    )
                _write_active_questions_atomically_locked(merged_questions)
                _replace_active_questions_locked(merged_questions)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        logger.warning(
            "Failed to persist interactive questions; continuing in-memory",
            path=str(_persistence_file),
            error=str(exc),
        )


def init_persistence(storage_root: Path) -> None:
    """Initialize interactive question persistence from disk."""
    global _active_questions, _persistence_file, _persistence_lock_file
    persistence_file = storage_root / "tracking" / "interactive_questions.json"
    persistence_lock_file = storage_root / "tracking" / "interactive_questions.lock"

    with _thread_lock:
        _persistence_file = persistence_file
        _persistence_lock_file = persistence_lock_file
        try:
            persistence_file.parent.mkdir(parents=True, exist_ok=True)
            with persistence_lock_file.open("a+") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    loaded_questions = _load_persisted_questions()
                    _write_active_questions_atomically_locked(loaded_questions)
                    _replace_active_questions_locked(loaded_questions)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            _active_questions = {}
            _dirty_question_ids.clear()
            _deleted_question_ids.clear()
            if isinstance(exc, (json.JSONDecodeError, KeyError, TypeError, ValueError)):
                try:
                    persistence_file.unlink(missing_ok=True)
                except OSError:
                    _persistence_file = None
                    _persistence_lock_file = None
            else:
                _persistence_file = None
                _persistence_lock_file = None
            logger.warning(
                "Failed to initialize interactive question persistence; continuing in-memory",
                path=str(persistence_file),
                error=str(exc),
            )


def _preview_text(text: str, max_length: int = 160) -> str:
    """Return a compact preview for warning logs."""
    compact = " ".join(text.split())
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 3].rstrip()}..."


def _find_interactive_match(response_text: str) -> re.Match[str] | None:
    """Return the first interactive block match if present."""
    return re.search(_INTERACTIVE_PATTERN, response_text, _INTERACTIVE_PATTERN_FLAGS)


def _normalize_interactive_marker(text: str) -> str:
    """Normalize an interactive fence marker for exact comparisons."""
    return " ".join(text.strip().lower().split())


def _is_interactive_marker(text: str) -> bool:
    """Return whether the text is an allowed interactive marker."""
    return _normalize_interactive_marker(text) in _INTERACTIVE_MARKERS


def _is_inline_interactive_json(text: str) -> bool:
    """Return whether the text looks like an interactive marker with inline JSON."""
    normalized = _normalize_interactive_marker(text)
    for marker in ("interactive json", "interactive"):
        if not normalized.startswith(f"{marker} "):
            continue
        remainder = normalized[len(marker) :].lstrip()
        if remainder.startswith(("{", "[")):
            return True
    return False


def _should_warn_unparsed_interactive(response_text: str) -> bool:
    """Return whether the text looks like a malformed interactive fence."""
    lines = response_text.splitlines()
    for index, line in enumerate(lines):
        stripped_line = line.lstrip()
        fence_index = stripped_line.find("```")
        if fence_index == -1:
            continue

        fence_marker = stripped_line[fence_index + 3 :].strip()
        if _is_interactive_marker(fence_marker) or _is_inline_interactive_json(fence_marker):
            return True
        if fence_marker:
            continue

        if index + 1 >= len(lines):
            continue
        next_line = lines[index + 1].strip()
        if _is_inline_interactive_json(next_line):
            return True
        if not _is_interactive_marker(next_line):
            continue
        if index + 2 >= len(lines):
            continue
        payload_line = lines[index + 2].lstrip()
        if payload_line.startswith(("{", "[")):
            return True
    return False


def should_create_interactive_question(response_text: str) -> bool:
    """Check if the response contains an interactive question in JSON format.

    Args:
        response_text: The AI's response text

    Returns:
        True if an interactive code block is found

    """
    return bool(_find_interactive_match(response_text))


async def handle_reaction(
    client: nio.AsyncClient,
    event: nio.ReactionEvent,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> InteractiveSelection | None:
    """Handle a reaction event that might be an answer to a question.

    Args:
        client: The Matrix client
        event: The reaction event
        agent_name: The name of the agent handling this
        config: Application configuration
        runtime_paths: Explicit runtime context for agent detection

    Returns:
        Interactive selection details if this was a valid response, None otherwise

    """
    with _thread_lock:
        _refresh_active_questions_locked()
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
        if reaction_key not in question.options or event.sender == client.user_id:
            return None

        # Ignore reactions from other agents
        if is_agent_id(event.sender, config, runtime_paths):
            logger.debug("Ignoring reaction from agent", sender=event.sender, reaction=reaction_key)
            return None

        selected_value = question.options[reaction_key]

        with bound_log_context(room_id=question.room_id, thread_id=question.thread_id):
            logger.info(
                "Received answer via reaction",
                user=event.sender,
                reaction=reaction_key,
                value=selected_value,
            )

        # The emoji reaction itself is the user's response, so just consume the question.
        if _remove_active_question_locked(event.reacts_to):
            _save_active_questions_locked()

        return InteractiveSelection(
            question_event_id=event.reacts_to,
            selection_key=reaction_key,
            selected_value=selected_value,
            thread_id=question.thread_id,
        )


async def handle_text_response(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    event: TextResponseEvent,
    agent_name: str,
) -> InteractiveSelection | None:
    """Handle text responses to interactive questions (e.g., "1", "2", "3").

    Args:
        client: The Matrix client
        room: The room the message occurred in
        event: The message event
        agent_name: The name of the agent handling this

    Returns:
        Interactive selection details if this was a valid response, None otherwise

    """
    message_text = event.body.strip()

    # Look for numeric responses
    if not message_text.isdigit() or len(message_text) > 1:
        return None

    thread_info = EventInfo.from_event(event.source)
    thread_id = thread_info.thread_id

    # Find matching active questions in this room/thread
    with _thread_lock:
        _refresh_active_questions_locked()
        return _handle_text_response_locked(
            room_id=room.room_id,
            thread_id=thread_id,
            message_text=message_text,
            sender=event.sender,
            client_user_id=client.user_id,
            agent_name=agent_name,
        )

    return None


def _handle_text_response_locked(
    *,
    room_id: str,
    thread_id: str | None,
    message_text: str,
    sender: str,
    client_user_id: str | None,
    agent_name: str,
) -> InteractiveSelection | None:
    """Handle a numeric reply while holding ``_thread_lock``."""
    for question_event_id, question in list(_active_questions.items()):
        if question.room_id != room_id or question.thread_id != thread_id:
            continue
        if message_text not in question.options or sender == client_user_id:
            continue
        if agent_name != question.creator_agent:
            continue

        selected_value = question.options[message_text]
        with bound_log_context(room_id=room_id, thread_id=thread_id):
            logger.info(
                "Received answer via text",
                user=sender,
                text=message_text,
                value=selected_value,
            )
        if _remove_active_question_locked(question_event_id):
            _save_active_questions_locked()
        return InteractiveSelection(
            question_event_id=question_event_id,
            selection_key=message_text,
            selected_value=selected_value,
            thread_id=question.thread_id,
        )
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
    first_match = _find_interactive_match(response_text)

    if not first_match:
        if _should_warn_unparsed_interactive(response_text):
            logger.warning(
                "Interactive block not parsed",
                preview=_preview_text(response_text),
            )
        return _InteractiveResponse(response_text, None, None)

    try:
        interactive_data = json.loads(first_match.group(1))
    except json.JSONDecodeError as exc:
        logger.warning(
            "Interactive JSON parse failed",
            error=str(exc),
            preview=_preview_text(first_match.group(1)),
        )
        return _InteractiveResponse(response_text, None, None)

    if not isinstance(interactive_data, dict):
        logger.warning(
            "Interactive JSON payload must be an object",
            payload_type=type(interactive_data).__name__,
            preview=_preview_text(first_match.group(1)),
        )
        return _InteractiveResponse(response_text, None, None)

    interactive_payload = cast("dict[str, object]", interactive_data)
    question = interactive_payload.get("question", _DEFAULT_QUESTION)
    options = cast("list[dict[str, str]]", interactive_payload.get("options", []))

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
        _store_active_question_locked(
            event_id,
            _InteractiveQuestion(
                room_id=room_id,
                thread_id=thread_id,
                options=option_map,
                creator_agent=agent_name,
            ),
        )
        _save_active_questions_locked()
    with bound_log_context(room_id=room_id, thread_id=thread_id):
        logger.info("Registered interactive question", event_id=event_id, options=len(option_map))


def clear_interactive_question(event_id: str) -> None:
    """Remove one tracked interactive question when its message is edited away."""
    with _thread_lock:
        question = _active_questions.get(event_id)
        if not _remove_active_question_locked(event_id):
            return
        _save_active_questions_locked()
    with bound_log_context(
        room_id=question.room_id if question is not None else None,
        thread_id=question.thread_id if question is not None else None,
    ):
        logger.info("Cleared interactive question", event_id=event_id)


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
    global _persistence_file, _persistence_lock_file
    with _thread_lock:
        _active_questions.clear()
        _dirty_question_ids.clear()
        _deleted_question_ids.clear()
        _persistence_file = None
        _persistence_lock_file = None
