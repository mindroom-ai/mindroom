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


def select_thread_window_event_ids(
    newest_first_candidates: list[ThreadWindowCandidate],
    *,
    budget: ThreadReadBudget,
) -> list[str]:
    """Return the event IDs that fit one budget, walking a thread from its newest message back.

    The newest message is always returned even when it alone exceeds the byte budget: a read that
    answered with nothing would be indistinguishable from an empty thread, and the caller asked for
    the tail. The running total only grows, so the messages that fit are always a prefix.
    """
    selected: list[str] = []
    consumed_bytes = 0
    for candidate in newest_first_candidates:
        if budget.max_messages is not None and len(selected) >= budget.max_messages:
            break
        next_total = consumed_bytes + candidate.window_bytes
        if budget.max_bytes is not None and selected and next_total > budget.max_bytes:
            break
        selected.append(candidate.event_id)
        consumed_bytes = next_total
    return selected


__all__ = [
    "DEFAULT_THREAD_READ_MAX_BYTES",
    "DEFAULT_THREAD_READ_MAX_MESSAGES",
    "UNBOUNDED_THREAD_READ",
    "ThreadReadBudget",
    "ThreadWindowCandidate",
    "select_thread_window_event_ids",
]
