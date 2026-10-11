"""Recognize paused requests whose system message still carries Agno's summary block."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.models.message import Message

_LEGACY_SUMMARY_TAG = "<summary_of_previous_interactions>"


# LEGACY_COMPAT: Paused runs whose saved system message embeds the session summary.
# Legacy format: A run paused for approval stores its system message, which Agno's builder rendered with
# ``session.summary`` inside a ``<summary_of_previous_interactions>`` block when add_session_summary_to_context was on
# (configured agents, teams, and authored subagents through the interim persona path).
# Last legacy release: v2026.10.231; replacement: the next release renders the summary as the first history message.
# Handling: Resuming such a run inserts no summary message, so its request keeps the single summary it was paused
# with and the paused tool call's signed reasoning stays valid against an unchanged prefix.
# Coverage: tests/test_history_summary_message.py::test_resuming_a_pre_release_pause_keeps_its_single_summary.
def system_message_embeds_summary(messages: Sequence[Message]) -> bool:
    """Return whether a leading system or developer message already carries Agno's summary block."""
    for message in messages:
        if message.role not in {"system", "developer"}:
            return False
        if isinstance(message.content, str) and _LEGACY_SUMMARY_TAG in message.content:
            return True
    return False
