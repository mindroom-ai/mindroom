"""Shared pure helpers for Matrix thread cache policies.

``thread_cache_rejection_reason`` is the single gate for durable thread snapshots, and it asks one
question: has a gap been recorded against this thread since its snapshot was installed?

There is deliberately no validation timestamp, no age rule and no restart rule. A snapshot that is
present and not gap-marked is served; anything else is refetched. See ``thread_cache_state`` for the
two rules that govern the marker itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

    from .thread_cache_state import ThreadCacheGap


def latest_visible_thread_event_id(history: Sequence[ResolvedVisibleMessage]) -> str | None:
    """Return the latest visible event ID from one resolved thread history."""
    if not history:
        return None
    return history[-1].visible_event_id or history[-1].event_id or None


def thread_cache_gap_reason(gap: ThreadCacheGap) -> str:
    """Return the label for one recorded gap, naming the unlabelled case rather than dropping it."""
    return gap.gap_reason or "thread_gap_marked"


def thread_cache_rejection_reason(gap: ThreadCacheGap | None) -> str | None:
    """Return why one durable thread snapshot must be rejected, if at all."""
    return None if gap is None else thread_cache_gap_reason(gap)
