"""When a thread is due for its next automatic summary."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.thread_summary.state import trusted_summary_metadata
from mindroom.thread_tag_vocabulary import claim_vocabulary_check

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

_PREQUEUE_CONCURRENCY_MARGIN = 2


@dataclass
class _ThreadSummaryBaselines:
    """Each thread's summarized message count and summary-write lock, shared by every bot in the process.

    A count only rises, and the trusted notices in a thread's history restore it after a restart.
    """

    _counts: dict[tuple[str, str], int] = field(default_factory=dict, init=False)
    _locks: defaultdict[tuple[str, str], asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock),
        init=False,
    )

    def lock(self, room_id: str, thread_id: str) -> asyncio.Lock:
        """Return the lock every summary write to one thread holds."""
        return self._locks[room_id, thread_id]

    def count(self, room_id: str, thread_id: str) -> int:
        """Return the message count one thread was last summarized at, or 0."""
        return self._counts.get((room_id, thread_id), 0)

    def record(self, room_id: str, thread_id: str, message_count: int) -> None:
        """Raise one thread's summarized message count to ``message_count``."""
        if message_count > self.count(room_id, thread_id):
            self._counts[room_id, thread_id] = message_count

    def next_threshold(self, room_id: str, thread_id: str, config: Config) -> int:
        """Return the message count at which one thread's next automatic summary is due."""
        last_summarized_count = self.count(room_id, thread_id)
        if last_summarized_count <= 0:
            return config.defaults.thread_summary_first_threshold
        return last_summarized_count + config.defaults.thread_summary_subsequent_interval


THREAD_SUMMARY_BASELINES = _ThreadSummaryBaselines()


def thread_summary_message_count_hint(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    trusted_sender_ids: Collection[str],
) -> int:
    """Return the thread's size after the reply being sent, counted from the history the reply read.

    No room authorizer is at hand here, so only trusted notices are left out and a person's pin counts as a message.
    That can only over-count, which queues a pass a little early; the pass recounts under the full rules.
    """
    ordinary = sum(
        1
        for message in thread_history
        if trusted_summary_metadata(message, trusted_sender_ids=trusted_sender_ids) is None
    )
    return ordinary + 1


def should_queue_thread_summary(
    room_id: str,
    thread_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    message_count_hint: int | None,
) -> bool:
    """Return whether summary generation or room-vocabulary upkeep is due."""
    if message_count_hint is None:
        return True
    threshold = THREAD_SUMMARY_BASELINES.next_threshold(room_id, thread_id, config)
    return message_count_hint >= threshold - _PREQUEUE_CONCURRENCY_MARGIN or claim_vocabulary_check(
        room_id,
        config,
        runtime_paths,
        now=datetime.now(UTC),
    )
