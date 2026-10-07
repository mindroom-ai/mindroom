"""Presentations of what an earlier release's paused replies showed, for their adoption into reply records.

``event_journal.legacy_reply_messages`` adopts an earlier release's paused
replies from the database at the first start with reply records; this module
encodes what that release stored into the reply layer's presentations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.event_journal.legacy_reply_messages import LegacyPausedAnswer, LegacyPresentations
from mindroom.reply_presentation import (
    AGENT_PLACEHOLDER,
    TEAM_PLACEHOLDER,
    Presentation,
    Segment,
    encode_presentation,
)
from mindroom.tool_system.events import deserialize_tool_trace

if TYPE_CHECKING:
    from mindroom.event_journal import ApprovalContinuation


def _paused(continuation: ApprovalContinuation, answer: LegacyPausedAnswer, span_id: str) -> str:
    """What a waiting approval showed: the paused text and trace the continuation kept."""
    presentation = Presentation(
        segments=(
            Segment(
                kind="answer",
                text=answer.text,
                span_id=span_id,
                tool_trace=tuple(deserialize_tool_trace(answer.tool_trace)),
                team_state=answer.team_state,
            ),
        )
        if answer.text or answer.tool_trace
        else (),
        placeholder=TEAM_PLACEHOLDER if continuation.entity_kind == "team" else AGENT_PLACEHOLDER,
        show_tool_calls=continuation.show_tool_calls,
    )
    return encode_presentation(presentation)


LEGACY_PRESENTATIONS = LegacyPresentations(
    empty=lambda team: encode_presentation(Presentation(placeholder=TEAM_PLACEHOLDER if team else AGENT_PLACEHOLDER)),
    paused=_paused,
)
