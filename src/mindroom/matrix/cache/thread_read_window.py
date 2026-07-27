"""Budget and selection for bounded thread reads.

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

``event_bytes`` measures fetch cost, not context cost. An oversized body offloaded to sidecar
storage has a small payload and a large resolved body, which is correct here - the bound exists to
stop hauling megabytes across the wire, and sidecar hydration is separately bounded by the message
count in the window. The token budget stays at the compaction layer; this is not a token budget.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

# Both defaults are deliberately generous. Over-fetching costs time; under-fetching silently drops
# context the agent would have used, which surfaces as "the agent forgot something" with no error
# anywhere. So these are sized to never bind on a thread anyone would call normal.
#
# The byte ceiling is not derived from a token budget, deliberately: this bound measures what the
# read hauls off disk, and token accounting stays at the compaction layer. It is set from the two
# sizes that matter:
#
#   a large legitimate thread   200 messages x ~2 kB   ~400 kB   must never bind
#   the pathology being killed  1,000 x 20 kB          ~20 MB    must always bind
#
# 2 MiB sits ~5x above the first and ~10x below the second. Measured payload throughput through
# the driver is roughly 300 MB/s including json.loads, so the ceiling itself costs ~7 ms.
DEFAULT_THREAD_READ_MAX_MESSAGES = 200
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

    The newest message is always returned even when it alone exceeds the byte budget: a read that
    answered with nothing would be indistinguishable from an empty thread, and the caller asked for
    the tail. The running total only grows, so the messages that fit are always a prefix.
    """
    selected: list[str] = []
    consumed_bytes = 0
    stopped_at_max_bytes = False
    stopped_at_max_messages = False
    for candidate in newest_first_candidates:
        if budget.max_messages is not None and len(selected) >= budget.max_messages:
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
    "ThreadWindowSelection",
    "log_thread_window_selection",
    "select_thread_window_event_ids",
]
