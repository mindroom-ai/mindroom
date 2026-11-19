"""When a thread is due for its next automatic summary."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.thread_summary.state import trusted_summary_metadata
from mindroom.thread_tag_vocabulary import claim_vocabulary_check

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

_PREQUEUE_CONCURRENCY_MARGIN = 2
# A new thread's first reply brings it to two messages: the prompt and the answer.
_NEW_THREAD_SIZE_AFTER_FIRST_REPLY = 2


@dataclass
class _ThreadSummaryBaselines:
    """Each thread's summarized count, summary-write lock, and prompt-title state, shared by every bot in the process.

    A count only rises, and the trusted notices in a thread's history restore it after a restart.
    """

    _counts: dict[tuple[str, str], int] = field(default_factory=dict, init=False)
    _locks: defaultdict[tuple[str, str], asyncio.Lock] = field(
        default_factory=lambda: defaultdict(asyncio.Lock),
        init=False,
    )
    _provisional_titles: set[tuple[str, str]] = field(default_factory=set, init=False)
    _superseded_provisional_titles: set[tuple[str, str]] = field(default_factory=set, init=False)

    def claim_provisional_title(self, room_id: str, thread_id: str) -> bool:
        """Claim one thread's single prompt-only title attempt, so only the first responder in the process makes it."""
        if (room_id, thread_id) in self._provisional_titles:
            return False
        self._provisional_titles.add((room_id, thread_id))
        return True

    def supersede_provisional_title(self, room_id: str, thread_id: str) -> None:
        """Mark one thread's reply as delivered, so a prompt-only title not yet sent is stale and must not be."""
        self._superseded_provisional_titles.add((room_id, thread_id))

    def provisional_title_superseded(self, room_id: str, thread_id: str) -> bool:
        """Return whether a reply in one thread was delivered, which makes a prompt-only title stale."""
        return (room_id, thread_id) in self._superseded_provisional_titles

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


def _kept_title_path(runtime_paths: RuntimePaths, room_id: str, thread_id: str) -> Path:
    digest = hashlib.sha256(f"{room_id}\0{thread_id}".encode()).hexdigest()
    return tracking_dir(runtime_paths) / "kept_thread_titles" / f"{digest}.json"


def record_kept_title(
    runtime_paths: RuntimePaths,
    room_id: str,
    thread_id: str,
    message_count: int,
    *,
    initial_enrichment_complete: bool,
) -> None:
    """Save what a refresh that kept its title would have written in a notice, without posting one."""
    previous_count, previously_enriched = kept_title_record(runtime_paths, room_id, thread_id)
    payload = {
        "message_count": max(previous_count, message_count),
        "initial_enrichment_complete": previously_enriched or initial_enrichment_complete,
    }
    write_json_file_durable(_kept_title_path(runtime_paths, room_id, thread_id), payload)


def kept_title_record(runtime_paths: RuntimePaths, room_id: str, thread_id: str) -> tuple[int, bool]:
    """Return the message count and enrichment outcome of the thread's kept refreshes, or ``(0, False)``."""
    try:
        payload = json.loads(_kept_title_path(runtime_paths, room_id, thread_id).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return 0, False
    count = payload.get("message_count") if isinstance(payload, dict) else None
    if not isinstance(count, int) or isinstance(count, bool):
        return 0, False
    return count, payload.get("initial_enrichment_complete") is True


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


def first_reply_reaches_first_threshold(config: Config) -> bool:
    """Return whether the first automatic summary is due at a new thread's first reply.

    Only then may a prompt-only title come earlier; a higher first threshold keeps new threads untitled for longer.
    """
    return config.defaults.thread_summary_first_threshold <= _NEW_THREAD_SIZE_AFTER_FIRST_REPLY


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
