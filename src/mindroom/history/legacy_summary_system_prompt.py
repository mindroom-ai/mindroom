"""Recognize paused requests whose system message still carries Agno's summary block."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.models.message import Message

_LEGACY_SUMMARY_TAG = "<summary_of_previous_interactions>"
_LEGACY_SUMMARY_BLOCK = re.compile(
    r"Here is a brief summary of your previous interactions:\n\n<summary_of_previous_interactions>\n.*?"
    r"</summary_of_previous_interactions>\n\n(?:Note: this information is from previous interactions[^\n]*\n\n)?",
    re.DOTALL,
)


# LEGACY_COMPAT: Paused runs whose saved system message embeds the session summary.
# Legacy format: A run paused for approval stores its system message, which Agno's builder rendered with
# ``session.summary`` inside a ``<summary_of_previous_interactions>`` block when add_session_summary_to_context was on
# (configured agents, teams, and authored subagents through the interim persona path).
# Last legacy release: v2026.10.346; replacement: the next release renders the summary as a history message.
# Handling: Resuming such a run inserts no summary message, so its request keeps the single summary it was paused
# with and the paused tool call's signed reasoning stays valid against an unchanged prefix. A mid-turn compaction
# of that resumed request folds the paused tool call anyway, so it also removes the block from the system message,
# including the stored one behind an approval receipt's projected copy.
# Coverage: tests/test_history_summary_message.py::test_resuming_a_pre_release_pause_keeps_its_single_summary;
# tests/test_mid_turn_compaction.py::test_legacy_resume_then_mid_turn_compaction_leaves_one_summary;
# tests/test_approval_receipt.py::test_publishing_keeps_a_legacy_summary_strip_made_on_the_outbound_copy.
def system_message_embeds_summary(messages: Sequence[Message]) -> bool:
    """Return whether a leading system or developer message already carries Agno's summary block."""
    for message in messages:
        if message.role not in {"system", "developer"}:
            return False
        if isinstance(message.content, str) and _LEGACY_SUMMARY_TAG in message.content:
            return True
    return False


def strip_embedded_summary(message: Message) -> None:
    """Remove Agno's summary block from a system or developer message in place, keeping the message's identity."""
    if isinstance(message.content, str) and _LEGACY_SUMMARY_TAG in message.content:
        message.content = _LEGACY_SUMMARY_BLOCK.sub("", message.content)
