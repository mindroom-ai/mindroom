"""Budget and selection for bounded thread reads.

What this read is for, and what it is not
-----------------------------------------
The reduction comes from collapsing edits, not from dropping messages. Production threads run
~94% edit rows, so returning one winning edit per message instead of every edit ever seen took a
2,021-row thread to 41 rows without losing a message. Dropping messages buys nothing on top of
that and costs a great deal: see the message-bound comment below.

The root fix is upstream of here. Nothing in the codebase reads a superseded edit - every consumer
takes the latest, or bulk-deletes - so only the winning edit per (original, sender) needs to be
persisted at all. Pruning superseded edits at write time would remove those rows at the source and
make this read's collapse unnecessary. That is a write-path change with redaction and
arrival-order consequences, so it is not done here, but it is the better place to solve this.

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

A thread read is bounded in two dimensions because neither alone is right:

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

``event_bytes`` measures fetch cost, not context cost. An oversized body offloaded to sidecar
storage has a small payload and a large resolved body, which is correct here - the bound exists to
stop hauling megabytes across the wire, and sidecar hydration is separately bounded by the message
count in the window. The token budget stays at the compaction layer; this is not a token budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

# The message bound is a guard against a degenerate thread, NOT a window onto a normal one.
#
# It was 200, and that was wrong. A fixed count slides: turn N reads M1..M200, turn N+1 reads
# M2..M201, so the prompt prefix changes every turn and provider prefix caching cannot hit the
# history block. The unbounded read it replaced was append-only and cached by construction, so the
# window introduced that churn rather than inheriting it. Worse, the read is upstream of
# compaction, so anything it drops can never be summarized - the compactor cannot compress
# messages it was never handed.
#
# The reduction this read is actually for comes from collapsing edits, not from dropping messages.
# Production threads run ~94% edit rows; returning one winning edit per message instead of every
# edit ever seen took a 2,021-row thread to 41 rows with no message lost. The count now sits far
# above any real thread and only stops a pathological one - a thread of a million empty messages
# would otherwise pass the byte bound and then cost a million sidecar hydrations and a fold over a
# million rows, neither of which the byte bound can see.
DEFAULT_THREAD_READ_MAX_MESSAGES = 5_000

# The byte ceiling is the real bound, and it is not derived from a token budget: this measures what
# the read hauls off disk, and token accounting stays at the compaction layer. Set from the two
# sizes that matter:
#
#   a large legitimate thread   200 messages x ~2 kB   ~400 kB   must never bind
#   the pathology being killed  1,000 x 20 kB          ~20 MB    must always bind
#
# 2 MiB sits ~5x above the first and ~10x below the second, and costs ~7 ms at measured throughput.
DEFAULT_THREAD_READ_MAX_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ThreadReadBudget:
    """How much of one thread a reader is willing to pay for."""

    max_messages: int | None = None
    max_bytes: int | None = None

    @property
    def is_bounded(self) -> bool:
        """Return whether this budget constrains the read at all."""
        return self.max_messages is not None or self.max_bytes is not None


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
    "DEFAULT_THREAD_READ_MAX_MESSAGES",
    "UNBOUNDED_THREAD_READ",
    "ThreadReadBudget",
    "ThreadWindowCandidate",
    "ThreadWindowRead",
    "ThreadWindowSelection",
    "log_thread_window_selection",
    "select_thread_window_event_ids",
]
