"""Dependency-free value objects for interactive questions and selections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InteractiveQuestion:
    """One delivered question that can still accept a selection."""

    question_event_id: str
    revision_event_id: str
    room_id: str
    thread_id: str | None
    creator_agent: str
    question_text: str
    options: dict[str, str]
    option_labels: dict[str, str]


@dataclass(frozen=True, slots=True)
class InteractiveSelection:
    """One durable source's validated answer to an interactive question."""

    question_event_id: str
    question_text: str
    selection_key: str
    selected_label: str
    selected_value: str
    thread_id: str | None
