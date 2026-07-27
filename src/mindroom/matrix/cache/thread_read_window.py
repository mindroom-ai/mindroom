"""Budget and selection for bounded thread reads.

What this read is for, and what it is not
-----------------------------------------
The reduction comes from collapsing edits, not from dropping messages. Production threads run
~94% edit rows, so returning one winning edit per message instead of every edit ever seen took a
2,021-row thread to 41 rows without losing a message. Dropping messages buys nothing on top of
that and costs a great deal: see the message-bound comment below.

The root fix is upstream of here, and it is a write-path change rather than a read one. Threads run
~94% edit rows because every superseded edit is retained. Pruning them at write time removes those
rows at the source and makes this read's collapse unnecessary.

That retention is deliberate, so pruning is a trade rather than a cleanup: redacting the current
winning edit is contractually supposed to reveal the previous one, which only works while the older
rows exist (``test_redacting_latest_edit_falls_back_to_previous_cached_edit``). The trade looks
sound because the case is close to unreachable. Redacting a *message* already removes the original
and every dependent edit together, and mindroom-cinny's delete targets the original event ID -
``MessageDeleteItem`` passes ``mEvent.getId()``, and a replacement is only ever reached through
``replacingEvent()``, which is never a redaction target there. Reaching the rollback path needs the
raw API, ``/redact <edit-event-id>``, or moderation tooling. Element was not checked; check it
before building this.

The contract to implement, if it is built:

* keep only the current legitimate edit per (original, sender);
* redacting an already-pruned edit tombstones it and is otherwise a no-op;
* redacting the retained winner deletes it, marks the thread stale, and refetches full history.
  ``invalidate_after_redaction`` in ``thread_writes`` already does this on the live redaction path,
  so this is a simplification of existing machinery rather than new machinery;
* if the homeserver is unreachable at that moment, fail or degrade explicitly - never serve the
  original body as confirmed full history;
* keep tombstones, so out-of-order sync cannot resurrect a deleted edit.

Three things that cost real time to learn
-----------------------------------------
1. Never benchmark this path without ANALYZE. On unanalyzed PostgreSQL an unseen namespace
   estimates one row against thousands, every join degrades to a nested loop with a join filter,
   and BOTH the bounded and unbounded reads collapse - the unbounded one by 77x. A comparison made
   in that state measured the planner, not the query, and reversed the apparent verdict.
2. The edit-ranking universe must be exactly the rows the read returns, and the grouping key must
   be exactly the fold's grouping key. Rank over a wider universe and a row the outer query later
   discards suppresses the in-thread runner-up; group by a coarser key and a foreign edit
   suppresses the author's own. Both shipped as bugs before being caught.
3. Sizes must come from a stored column. Computing length(event_json) at read time detoasts every
   candidate on PostgreSQL and reads every overflow page on SQLite, which is the cost the bound
   exists to avoid.

Collapse is not truncation, and only one of them is on by default
-----------------------------------------------------------------
Every read collapses. Nothing truncates unless its caller asks, and no caller currently does.

An earlier revision bounded every read at 2 MiB. That was wrong for a reason worth keeping: no
consumer of a thread read wants a recent tail. The model history refresh, dispatch context, thread
summaries and export all treat what they receive as the whole thread and cannot distinguish a
truncated read from a short one, so a default bound does not give them a tail - it deletes the
oldest half of their input and tells them nothing. Truncation is retained as an opt-in for a caller
that genuinely wants a window, and is expressed in two dimensions because neither alone is right:

* A message count alone truncates a thread of a thousand one-character messages that costs a
  kilobyte to return, and happily hauls back twenty twenty-kilobyte messages.
* A byte budget alone cannot express "the agent only wants the recent tail" for a thread whose
  messages are all tiny.

Selection runs over ``events.event_bytes``, a size recorded at write time from a payload already in
hand. Reading ``length(event_json)`` instead would detoast every candidate on PostgreSQL and read
every overflow page on SQLite, which is the cost the bound exists to avoid.

``thread_events`` holds one row per event, and an edit is an event, so a bound applied to raw rows
selects one message and a pile of its own edits. Candidates are therefore originals, and each
selected original carries its latest edit along with it.

PostgreSQL exposure worth knowing: the bounded read is two joins deeper than the unbounded one,
so it degrades further when table statistics are stale. On unanalyzed tables an unseen namespace or
room estimates one row against thousands, every join becomes a nested loop with a join filter, and
a bounded read measured 1367 ms against 10.5 ms once analyzed. Autovacuum closes this within its
naptime and a freshly created database has nothing to analyze, so the practical window is the
minute after a bulk refill. Any benchmark of this path must ANALYZE first or it measures the
planner, not the query - the unbounded read degrades 77x under the same conditions.

``event_bytes`` measures what returning a row costs, including anything it hydrates. An oversized
body offloaded to sidecar storage leaves a stub of a few hundred bytes that resolves to megabytes,
so the stub is charged the size its writer recorded before offloading rather than its stored
weight. This is a fetch budget, not a token budget - the token budget stays at the compaction
layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

# There is deliberately no default message bound. ``max_messages`` remains available for callers
# that genuinely want a message-shaped window, but nothing on the model-facing path passes one.
#
# A count was tried and removed. It slides: turn N reads M1..M200, turn N+1 reads M2..M201, so the
# prompt prefix changes every turn and provider prefix caching cannot hit the history block, while
# the unbounded read it replaced was append-only and cached by construction. It also sits upstream
# of compaction, so whatever it dropped was gone before anything could summarize it.
#
# It was also justified with an argument that does not survive arithmetic: that a count is needed
# to stop a degenerate thread of a million tiny messages the byte bound cannot see. A million
# messages cannot fit in 2 MiB - tiny messages still cost bytes - so the byte bound already caps
# the count implicitly at roughly twenty thousand of them.
#
# The one case a byte bound could not originally see was sidecars: an offloaded body has a small
# stored payload and a large resolved one, so ~7,000 stubs fit inside the budget and each then
# hydrates. That is now charged at ingestion from the size the writer records on the stub, which
# works on a cold cache too - the alternative, measuring the plaintext where it is cached, only has
# a size to read after something has already paid to download it.
#
# This constant has no caller: it is the size an opt-in bound should use, kept as the one place
# that number is justified rather than as a default anything inherits.
DEFAULT_THREAD_READ_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ThreadReadBudget:
    """How much of one thread a reader is willing to pay for."""

    max_messages: int | None = None
    max_bytes: int | None = None


UNBOUNDED_THREAD_READ = ThreadReadBudget()


@dataclass(frozen=True, slots=True)
class ThreadWindowRead:
    """One windowed read plus whether the window left anything out.

    ``truncated`` is load-bearing beyond diagnostics: a caller that labels a truncated window as
    full history makes completeness-dependent planning operate on a partial thread and lets the
    model-history refresh be skipped. It has to be the real answer, not an approximation, because
    over-claiming corrupts context while under-claiming forces an avoidable refetch every turn.
    """

    events: list[dict[str, Any]] | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class ThreadWindowCandidate:
    """One selectable thread message and the bytes returning it would cost."""

    event_id: str
    window_bytes: int


@dataclass(frozen=True, slots=True)
class ThreadWindowSelection:
    """The messages one budget admitted, and which bound stopped the walk."""

    event_ids: list[str]
    selected_bytes: int
    candidate_count: int
    stopped_at_max_bytes: bool = False
    stopped_at_max_messages: bool = False

    @property
    def truncated(self) -> bool:
        """Return whether any message was left out of the window."""
        return len(self.event_ids) < self.candidate_count


def select_thread_window_event_ids(
    newest_first_candidates: list[ThreadWindowCandidate],
    *,
    budget: ThreadReadBudget,
) -> ThreadWindowSelection:
    """Return the messages that fit one budget, walking a thread from its newest message back.

    The newest message is always returned even when it alone exceeds either budget: a read that
    answered with nothing would be indistinguishable from an empty thread, and the caller asked for
    the tail. That floor applies to ``max_messages=0`` too. The running total only grows, so the
    messages that fit are always a prefix.
    """
    selected: list[str] = []
    consumed_bytes = 0
    stopped_at_max_bytes = False
    stopped_at_max_messages = False
    for candidate in newest_first_candidates:
        if selected and budget.max_messages is not None and len(selected) >= budget.max_messages:
            stopped_at_max_messages = True
            break
        next_total = consumed_bytes + candidate.window_bytes
        if budget.max_bytes is not None and selected and next_total > budget.max_bytes:
            stopped_at_max_bytes = True
            break
        selected.append(candidate.event_id)
        consumed_bytes = next_total
    return ThreadWindowSelection(
        event_ids=selected,
        selected_bytes=consumed_bytes,
        candidate_count=len(newest_first_candidates),
        stopped_at_max_bytes=stopped_at_max_bytes,
        stopped_at_max_messages=stopped_at_max_messages,
    )


def log_thread_window_selection(
    selection: ThreadWindowSelection,
    *,
    budget: ThreadReadBudget,
    logger: BoundLogger,
    room_id: str,
    thread_id: str,
) -> None:
    """Report a bound that actually fired.

    The byte bound is sized never to bind on a normal thread, so if it does it is a signal rather
    than routine truncation. Truncating silently is indistinguishable from a context bug when
    someone reports that the agent forgot something.
    """
    if not selection.truncated:
        return
    logger.info(
        "Thread read window truncated",
        room_id=room_id,
        thread_id=thread_id,
        returned_messages=len(selection.event_ids),
        thread_messages=selection.candidate_count,
        selected_bytes=selection.selected_bytes,
        stopped_at_max_bytes=selection.stopped_at_max_bytes,
        stopped_at_max_messages=selection.stopped_at_max_messages,
        max_messages=budget.max_messages,
        max_bytes=budget.max_bytes,
    )


__all__ = [
    "DEFAULT_THREAD_READ_MAX_BYTES",
    "UNBOUNDED_THREAD_READ",
    "ThreadReadBudget",
    "ThreadWindowCandidate",
    "ThreadWindowRead",
    "ThreadWindowSelection",
    "log_thread_window_selection",
    "select_thread_window_event_ids",
]
