"""Dependency-free value objects for interactive questions and selections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

_INTERACTIVE_PROMPT_KEY = "io.mindroom.interactive"


@dataclass(frozen=True, slots=True)
class InteractivePrompt:
    """One prompt revision carried by its Matrix event content."""

    creator_agent: str
    question_text: str
    options: dict[str, str]
    option_labels: dict[str, str]
    source_event_id: str | None = None
    membership_epoch: int | None = None


def interactive_prompt_content(prompt: InteractivePrompt) -> dict[str, object]:
    """Encode one prompt into namespaced Matrix message content."""
    payload: dict[str, object] = {
        "creator_agent": prompt.creator_agent,
        "option_labels": dict(prompt.option_labels),
        "options": dict(prompt.options),
        "question_text": prompt.question_text,
    }
    if prompt.source_event_id is not None:
        payload["source_event_id"] = prompt.source_event_id
    if prompt.membership_epoch is not None:
        payload["membership_epoch"] = prompt.membership_epoch
    return {_INTERACTIVE_PROMPT_KEY: payload}


def _string_mapping(value: object) -> dict[str, str] | None:
    """Return a copied string mapping, or reject malformed Matrix metadata."""
    if not isinstance(value, dict):
        return None
    mapping = cast("dict[object, object]", value)
    if not mapping or any(not isinstance(key, str) or not isinstance(item, str) for key, item in mapping.items()):
        return None
    return cast("dict[str, str]", dict(mapping))


def interactive_prompt_from_content(content: Mapping[str, object]) -> InteractivePrompt | None:
    """Decode one complete prompt from Matrix message content."""
    raw = content.get(_INTERACTIVE_PROMPT_KEY)
    if not isinstance(raw, dict):
        return None
    payload = cast("dict[str, object]", raw)
    creator_agent = payload.get("creator_agent")
    question_text = payload.get("question_text")
    source_event_id = payload.get("source_event_id")
    membership_epoch = payload.get("membership_epoch")
    options = _string_mapping(payload.get("options"))
    option_labels = _string_mapping(payload.get("option_labels"))
    if (
        not isinstance(creator_agent, str)
        or not creator_agent
        or not isinstance(question_text, str)
        or not question_text
        or options is None
        or option_labels is None
        or (source_event_id is not None and (not isinstance(source_event_id, str) or not source_event_id))
        or (membership_epoch is not None and (type(membership_epoch) is not int or membership_epoch < 0))
        or (source_event_id is None and membership_epoch is None)
    ):
        return None
    return InteractivePrompt(
        creator_agent=creator_agent,
        question_text=question_text,
        options=options,
        option_labels=option_labels,
        source_event_id=source_event_id,
        membership_epoch=membership_epoch,
    )


@dataclass(frozen=True, slots=True)
class InteractiveSelection:
    """One durable source's validated answer to an interactive question."""

    question_event_id: str
    question_text: str
    selection_key: str
    selected_label: str
    selected_value: str
    thread_id: str | None
