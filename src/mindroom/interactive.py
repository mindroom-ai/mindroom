"""Interactive Q&A system using Matrix reactions as clickable buttons."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import nio

from mindroom.interactive_models import (
    InteractivePrompt,
    InteractiveSelection,
    interactive_prompt_content,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client import send_room_event_result
from mindroom.matrix.message_builder import build_reaction_content

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

_MAX_PROMPT_METADATA_BYTES = 8_000


@dataclass(frozen=True, slots=True)
class InteractiveMetadata:
    """Registration metadata extracted from one interactive response."""

    question_text: str
    option_map: dict[str, str]
    option_labels: dict[str, str]
    options_list: tuple[dict[str, str], ...]

    @classmethod
    def _from_parts(
        cls,
        option_map: dict[str, str] | None,
        options_list: Sequence[dict[str, str]] | None,
        *,
        question_text: str = "",
        option_labels: dict[str, str] | None = None,
    ) -> InteractiveMetadata | None:
        """Return copied metadata when both interactive registration parts exist."""
        if not option_map or not options_list:
            return None
        metadata = cls(
            question_text=question_text,
            option_map=dict(option_map),
            option_labels=dict(option_labels or {}),
            options_list=tuple(dict(item) for item in options_list),
        )
        encoded = json.dumps(
            {
                "option_labels": metadata.option_labels,
                "options": metadata.option_map,
                "question_text": metadata.question_text,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        if len(encoded) > _MAX_PROMPT_METADATA_BYTES:
            logger.warning(
                "Interactive prompt metadata exceeds the Matrix event budget",
                metadata_size_bytes=len(encoded),
                max_size_bytes=_MAX_PROMPT_METADATA_BYTES,
            )
            return None
        return metadata

    def options_as_list(self) -> list[dict[str, str]]:
        """Return a mutable copy for Matrix reaction-button registration."""
        return [dict(item) for item in self.options_list]


def build_prompt_content(
    metadata: InteractiveMetadata,
    *,
    creator_agent: str,
    source_event_id: str | None = None,
    membership_epoch: int | None = None,
) -> dict[str, object]:
    """Encode parsed interactive metadata into one Matrix prompt revision."""
    return interactive_prompt_content(
        InteractivePrompt(
            creator_agent=creator_agent,
            question_text=metadata.question_text,
            options=metadata.option_map,
            option_labels=metadata.option_labels,
            source_event_id=source_event_id,
            membership_epoch=membership_epoch,
        ),
    )


@dataclass(frozen=True, slots=True)
class _InteractiveResponse:
    """Result of parsing and formatting an interactive response."""

    formatted_text: str
    interactive_metadata: InteractiveMetadata | None = None

    @property
    def option_map(self) -> dict[str, str] | None:
        """Return the emoji/number mapping when this response is interactive."""
        if self.interactive_metadata is None:
            return None
        return dict(self.interactive_metadata.option_map)

    @property
    def options_list(self) -> list[dict[str, str]] | None:
        """Return the button option list when this response is interactive."""
        if self.interactive_metadata is None:
            return None
        return self.interactive_metadata.options_as_list()


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
_INLINE_INTERACTIVE_JSON_FENCE_PATTERN = r"```[ \t]*interactive(?:[ \t]+json)?[ \t]+(?:\{|\[)[^\r\n`]*```"
_MAX_OPTIONS = 5
_DEFAULT_QUESTION = "Please choose an option:"
_INSTRUCTION_TEXT = "React with an emoji or type the number to respond."


def _preview_text(text: str, max_length: int = 160) -> str:
    """Return a compact preview for warning logs."""
    compact = " ".join(text.split())
    if len(compact) <= max_length:
        return compact
    return f"{compact[: max_length - 3].rstrip()}..."


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


def build_selection_prompt(selection: InteractiveSelection) -> str:
    """Build the model prompt for one interactive option selection."""
    payload = {
        "question_event_id": selection.question_event_id,
        "thread_id": selection.thread_id,
        "question_text": selection.question_text,
        "selected_option": {
            "key": selection.selection_key,
            "label": selection.selected_label,
            "value": selection.selected_value,
        },
    }
    return (
        "The user selected an option for an earlier interactive question. "
        "Use the question_event_id and question_text below to bind the selection to the correct question.\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}"
    )


def _coerce_interactive_option(raw_option: object) -> dict[str, str] | None:
    """Return one normalized interactive option when the raw item is an object."""
    if not isinstance(raw_option, dict):
        return None

    option_data = cast("dict[object, object]", raw_option)
    label = str(option_data.get("label") or "Option")
    value = str(option_data.get("value") or label.lower())
    return {
        "emoji": str(option_data.get("emoji") or "❓"),
        "label": label,
        "value": value,
    }


def _coerce_interactive_payload(raw_json: str) -> tuple[str, list[dict[str, str]]] | None:
    """Return (question, capped options) when the fenced payload is a valid interactive object."""
    try:
        interactive_data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Interactive JSON parse failed",
            error=str(exc),
            preview=_preview_text(raw_json),
        )
        return None

    if not isinstance(interactive_data, dict):
        logger.warning(
            "Interactive JSON payload must be an object",
            payload_type=type(interactive_data).__name__,
            preview=_preview_text(raw_json),
        )
        return None

    question = str(interactive_data.get("question") or _DEFAULT_QUESTION)
    raw_options = interactive_data.get("options")
    if not isinstance(raw_options, list):
        logger.warning(
            "Interactive JSON options must be a list",
            options_type=type(raw_options).__name__,
            preview=_preview_text(raw_json),
        )
        return None

    options: list[dict[str, str]] = []
    for raw_option in raw_options:
        option = _coerce_interactive_option(raw_option)
        if option is None:
            continue
        options.append(option)
        if len(options) == _MAX_OPTIONS:
            break
    if not options:
        return None
    return question, options


def _render_question_text(question: str, options: list[dict[str, str]], *, include_instruction: bool) -> str:
    """Render one interactive question as display text."""
    option_lines = [f"{i}. {opt['emoji']} {opt['label']}" for i, opt in enumerate(options, 1)]
    parts = [question, "", *option_lines]
    if include_instruction:
        parts.extend(["", _INSTRUCTION_TEXT])
    return "\n".join(parts)


def _remove_inline_unparsed_interactive_fences(text: str) -> str:
    """Remove inline interactive JSON fences that the block parser cannot render."""
    cleaned_text, count = re.subn(
        _INLINE_INTERACTIVE_JSON_FENCE_PATTERN,
        "",
        text,
        flags=re.IGNORECASE,
    )
    if count == 0:
        return text

    logger.warning(
        "Interactive block not parsed",
        preview=_preview_text(text),
    )
    return cleaned_text.strip()


def parse_and_format_interactive(response_text: str, extract_mapping: bool = False) -> _InteractiveResponse:
    """Parse and format interactive content from response text.

    Each interactive block is replaced in place with its formatted question so
    surrounding prose keeps referring to the right spot. Only the first valid
    block carries registration metadata (reaction buttons); any additional
    blocks render as plain question text.

    Args:
        response_text: The response text containing interactive JSON
        extract_mapping: Whether to extract option mapping and return options list

    Returns:
        _InteractiveResponse with formatted_text, option_map, and options_list

    """
    matches = list(re.finditer(_INTERACTIVE_PATTERN, response_text, _INTERACTIVE_PATTERN_FLAGS))

    if not matches:
        if _should_warn_unparsed_interactive(response_text):
            logger.warning(
                "Interactive block not parsed",
                preview=_preview_text(response_text),
            )
        return _InteractiveResponse(response_text)

    first_payload = _coerce_interactive_payload(matches[0].group(1))
    if first_payload is None:
        return _InteractiveResponse(response_text)
    question, options = first_payload

    option_map: dict[str, str] | None = {} if extract_mapping else None
    option_labels: dict[str, str] | None = {} if extract_mapping else None
    if option_map is not None and option_labels is not None:
        for i, opt in enumerate(options, 1):
            emoji_char = opt["emoji"]
            label = opt["label"]
            value = opt["value"]
            option_map[emoji_char] = value
            option_map[str(i)] = value
            option_labels[emoji_char] = label
            option_labels[str(i)] = label

    rendered = [(matches[0], _render_question_text(question, options, include_instruction=True))]
    for extra_match in matches[1:]:
        extra_payload = _coerce_interactive_payload(extra_match.group(1))
        if extra_payload is None:
            rendered.append((extra_match, ""))
            continue
        extra_question, extra_options = extra_payload
        rendered.append((extra_match, _render_question_text(extra_question, extra_options, include_instruction=False)))
    if len(matches) > 1:
        logger.warning(
            "Multiple interactive blocks in one response; only the first gets reaction buttons",
            block_count=len(matches),
        )

    parts: list[str] = []
    last_end = 0
    for match, replacement in rendered:
        parts.append(response_text[last_end : match.start()])
        parts.append(replacement)
        last_end = match.end()
    parts.append(response_text[last_end:])
    final_text = _remove_inline_unparsed_interactive_fences("".join(parts).strip())

    return _InteractiveResponse(
        final_text,
        InteractiveMetadata._from_parts(
            option_map,
            options if extract_mapping else None,
            question_text=question,
            option_labels=option_labels,
        ),
    )


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
        reaction_response = await send_room_event_result(
            client,
            room_id,
            "m.reaction",
            build_reaction_content(event_id, emoji_char),
            operation="add_interactive_reaction",
        )
        if not isinstance(reaction_response, nio.RoomSendResponse):
            logger.warning("Failed to add reaction", emoji=emoji_char, error=str(reaction_response))
