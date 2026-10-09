"""Replies an earlier release left paused for approval, adopted once per principal.

An upgrade runs while no reply is in flight, so what an earlier release can
leave for reply records is an approval continuation waiting for its decision
or settling its failure. The first start with reply records gives each such
reply a record, from the database only, and discards the continuations the
reply rules no longer model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom import reply_lifecycle as rl

from . import approval_continuations, journal, reply_messages
from .legacy_response_attempts import discard_continuation
from .replies import AppliedTransition, apply

if TYPE_CHECKING:
    from collections.abc import Callable

    from .approval_continuations import ApprovalContinuation
    from .backend import Transaction

# The generation of spans an earlier release ran: never active, so nothing they left is live.
_LEGACY_GENERATION = "legacy"


@dataclass(frozen=True, slots=True)
class LegacyPausedAnswer:
    """What an earlier release's approval continuation stored of the answer its pause showed."""

    text: str = ""
    tool_trace: tuple[dict[str, object], ...] = ()
    team_state: dict[str, object] | None = None


# LEGACY_COMPAT: Paused answers stored in approval continuation context.
# Legacy format: approval_continuations.context_json carrying response_text, response_tool_trace, and
# response_presentation_state, which earlier releases wrote at every pause before replies held what a pause shows.
# Last legacy release: v2026.10.214; replacement: the unreleased durable reply messages keep the paused answer as
# the reply's answer segment and no longer write these keys.
# Handling: adoption reads them once, from the stored context of a continuation it adopts, to build the paused
# reply's presentation; nothing else reads them, and an advance rewrites the context without them.
# Coverage: tests/test_legacy_reply_messages.py::test_a_waiting_approval_pauses_its_reply_with_what_it_showed.
def _legacy_paused_answer(transaction: Transaction, principal_id: str, approval_id: str) -> LegacyPausedAnswer:
    row = transaction.fetchone(
        "SELECT context_json FROM approval_continuations WHERE principal_id = ? AND approval_id = ?",
        (principal_id, approval_id),
    )
    stored = {} if row is None else json.loads(str(row["context_json"]))
    text = stored.get("response_text")
    trace = stored.get("response_tool_trace")
    state = stored.get("response_presentation_state")
    return LegacyPausedAnswer(
        text=text if isinstance(text, str) else "",
        tool_trace=tuple(dict(entry) for entry in trace if isinstance(entry, dict)) if isinstance(trace, list) else (),
        team_state=dict(state) if isinstance(state, dict) and state else None,
    )


@dataclass(frozen=True, slots=True)
class LegacyPresentations:
    """Encoders of what an earlier release showed, owned by the reply layer that defines presentations."""

    # What a paused approval showed, from what its continuation stored, as the named span's answer.
    paused: Callable[[ApprovalContinuation, LegacyPausedAnswer, str], str]


def _classified(transaction: Transaction, principal_id: str) -> bool:
    """Return whether this principal's earlier-release replies were adopted already."""
    row = transaction.fetchone(
        "SELECT 1 AS present FROM reply_legacy_classifications WHERE principal_id = ?",
        (principal_id,),
    )
    return row is not None


# LEGACY_COMPAT: Approval-paused agent and team replies without reply records.
# Legacy format: approval continuations an earlier release left before reply_messages existed, for a principal with no
# reply_legacy_classifications row.
# Last legacy release: v2026.10.214; replacement: the unreleased durable reply messages record every reply in
# reply_messages and reply_spans.
# Handling: once per principal at bot start, before owner_lost, the newest continuation of each reply pauses it, and
# its approval runtime resumes or settles it as any paused reply. Older continuations of the same reply are
# discarded with their cards and their sources settled unanswered. A paused edit regeneration resumes as any paused
# reply; the edited text it selected stays out of its turn record, which only a later edit of a coalesced sibling
# reads. An upgrade runs while no reply is in flight, so nothing else is adopted.
# Coverage: tests/test_legacy_reply_messages.py.
def classify(
    transaction: Transaction,
    principal_id: str,
    *,
    entity_name: str,
    presentations: LegacyPresentations,
    now_ns: int,
) -> tuple[AppliedTransition, ...]:
    """Give each reply an earlier release left paused a record, once per principal; return what to run after commit."""
    if _classified(transaction, principal_id):
        return ()
    applied = [
        _adopt(
            transaction,
            principal_id,
            continuation,
            entity_name=entity_name,
            presentations=presentations,
            now_ns=now_ns,
        )
        for continuation in _newest_continuations(transaction, principal_id, entity_name)
    ]
    transaction.execute(
        "INSERT INTO reply_legacy_classifications (principal_id, classified_at_ns) VALUES (?, ?)",
        (principal_id, now_ns),
    )
    return tuple(applied)


def _new_id() -> str:
    return uuid4().hex


def _newest_continuations(
    transaction: Transaction,
    principal_id: str,
    entity_name: str,
) -> tuple[ApprovalContinuation, ...]:
    """Return the newest live continuation of each earlier-release reply, discarding the rest.

    An older continuation of the same reply is discarded with its cards and
    its sources settled unanswered.
    """
    newest: dict[str, ApprovalContinuation] = {}
    for continuation in approval_continuations.for_principal(transaction, principal_id):
        if continuation.entity_name != entity_name:
            continue
        event_id = continuation.response_event_id
        if reply_messages.for_event(transaction, principal_id, event_id) is not None:
            continue
        older = newest.get(event_id)
        if older is not None:
            # The newer pause is what the reply shows.
            discard_continuation(
                transaction,
                principal_id,
                older.approval_id,
                older.source_event_ids,
                why="older_continuation_of_one_reply",
            )
        newest[event_id] = continuation
    return tuple(newest.values())


def _adopt(
    transaction: Transaction,
    principal_id: str,
    continuation: ApprovalContinuation,
    *,
    entity_name: str,
    presentations: LegacyPresentations,
    now_ns: int,
) -> AppliedTransition:
    """Pause the reply ``continuation`` paused, which its approval runtime then resumes or settles."""
    sources = continuation.sources
    # The span whose pause created the continuation, holding its sources as one paused now does.
    paused = rl.Span(
        span_id=_new_id(),
        reply_id=_new_id(),
        kind=rl.SpanKind.TURN,
        delivery_id=continuation.source_event_ids[0],
        sources=rl.SpanSources(
            pending=sources.pending_event_ids,
            logical=sources.logical_source_event_ids,
            discovery=sources.discovery_event_ids,
        ),
        bot_generation=_LEGACY_GENERATION,
        claimed_at_ns=now_ns,
        base_sequence=0,
        approval_id=continuation.approval_id,
        outcome=rl.SpanOutcome.PAUSED,
        ended_at_ns=now_ns,
    )
    reply = rl.Reply(
        reply_id=paused.reply_id,
        entity_name=entity_name,
        room_id=continuation.room_id,
        thread_id=continuation.thread_id,
        membership_epoch=journal.current_membership_epoch(transaction, principal_id, continuation.room_id),
        state=rl.ReplyState.PAUSED,
        last_span_id=paused.span_id,
        presentation=presentations.paused(
            continuation,
            _legacy_paused_answer(transaction, principal_id, continuation.approval_id),
            paused.span_id,
        ),
        revision=1,
        reply_sequence=0,
        created_at_ns=now_ns,
        updated_at_ns=now_ns,
        event_id=continuation.response_event_id,
    )
    applied = apply(transaction, principal_id, rl.Transition(outcome=rl.Outcome.APPLIED, reply=reply, spans=(paused,)))
    # The continuation names the span that paused its reply, as one paused now does.
    transaction.execute(
        "UPDATE approval_continuations SET span_id = ? WHERE principal_id = ? AND approval_id = ? AND span_id IS NULL",
        (paused.span_id, principal_id, continuation.approval_id),
    )
    return applied
