"""Which self-authored revisions are transport rather than conversation content.

A streamed answer reaches Matrix as one original followed by a run of
``m.replace`` edits, each carrying a little more of the same reply. Those edits
are how the answer travels, not what it says: the conversation gained exactly
one message, whose body is whatever the stream finally settled on. Reducing
every progress echo into the projection rewrites that row once per edit and
arrives where it would have arrived anyway.

The rule is deliberately narrow, and each clause is load-bearing:

- Only replacements are skipped. The initial placeholder is an original and is
  the logical message the terminal body will land on, so dropping it would
  leave the answer with nothing to revise.
- Only this bot's own replacements are skipped. A status is a claim, not a
  permission, so a user-authored edit reduces normally whatever it advertises.
- Only non-terminal statuses are skipped. ``completed`` is the answer, and
  ``cancelled``, ``error``, and ``interrupted`` are the answer being cut short;
  prompt preparation tells those four apart from one another and from a stream
  still running, so all four must reach the projection.

The last clause is also why a crash mid-stream is safe. The logical row stays
at the placeholder, and startup stale-stream cleanup rewrites the visible body
with a terminal status whose echo then reduces like any other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.constants import (
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.event_journal import replacement_target, visible_content

if TYPE_CHECKING:
    from mindroom.event_journal import ProjectedEvent

# The only two statuses that promise another revision is coming. Everything
# else is the last thing this message will ever say, including the three ways a
# stream ends badly.
_TRANSPORT_STREAM_STATUSES = frozenset({STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING})


def is_transport_progress_revision(event: ProjectedEvent, *, self_sender: str) -> bool:
    """Return whether one event is this bot's own still-running streaming edit."""
    if event.sender != self_sender:
        return False
    if replacement_target(event.content) is None:
        return False
    return visible_content(event.content).get(STREAM_STATUS_KEY) in _TRANSPORT_STREAM_STATUSES
