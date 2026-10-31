"""Which stored runs and messages the model's history shows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord

if TYPE_CHECKING:
    from mindroom.history.types import HistoryScope
    from mindroom.llm.messages import Message
    from mindroom.llm.runs import AnyRunRecord

# A paused, stopped, or failed run is not part of the conversation the model sees.
NON_VISIBLE_RUN_STATUSES = frozenset({RunStatus.paused, RunStatus.cancelled, RunStatus.error})


def is_model_history_visible_run(run: AnyRunRecord) -> bool:
    """Return whether one stored run replays as model history: a finished top-level run."""
    return run.parent_run_id is None and run.status not in NON_VISIBLE_RUN_STATUSES


def run_in_scope(run: AnyRunRecord, scope: HistoryScope) -> bool:
    """Return whether a run belongs to a history scope: a team run to its team's, an agent run to its agent's."""
    if scope.kind == "team":
        return isinstance(run, TeamRunRecord) and run.team_id == scope.scope_id
    return isinstance(run, RunRecord) and run.agent_id == scope.scope_id


def replays_in_scope(run: AnyRunRecord, scope: HistoryScope) -> bool:
    """Return whether a stored run replays as ``scope``'s model history: a visible top-level run of the scope."""
    return is_model_history_visible_run(run) and run_in_scope(run, scope)


# A queued-message notice, MindRoom's instruction to pause for a newer message, is stored as a user message marked so.
QUEUED_MESSAGE_NOTICE_MARKER_KEY = "mindroom_queued_message_notice"


def is_queued_notice_message(message: Message) -> bool:
    """Return whether a message is a queued-message notice MindRoom wrote."""
    provider_data = message.provider_data
    marker = provider_data.get(QUEUED_MESSAGE_NOTICE_MARKER_KEY) if isinstance(provider_data, dict) else None
    # LEGACY_COMPAT: Queued-message notices marked "persisted" after delivery.
    # Legacy format: a stored notice whose marker is ``"persisted"`` instead of ``True``; once a reply was delivered,
    # MindRoom rewrote the notices its stored runs held into one marked so, and a reply that opened a scope rewrote a
    # notice a crashed reply had left with ``True``.
    # Last legacy release: v2026.10.750, whose post-delivery rewrite wrote the marker; replacement: the next release,
    # which stores each notice once, as its run held it, with ``True``.
    # Handling: such a notice reads as a notice, as one marked ``True`` does; both replay alike, so no row is rewritten.
    # Coverage: tests/test_agent_turn_runtime.py::test_a_notice_an_earlier_release_stored_replays_as_it_is_stored
    return marker is True or marker == "persisted"
