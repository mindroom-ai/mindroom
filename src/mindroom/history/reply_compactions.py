"""Record a reply's automatic history compactions on the reply itself.

``ReplyCompactions`` holds the entries one reply carries under
``io.mindroom.compactions``. ``ReplyCompactionLifecycle`` writes them as the
compaction runs; a requested (manual) compaction keeps its separate notice.
"""

from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.delivery_gateway import MatrixCompactionLifecycle
    from mindroom.history.types import (
        CompactionLifecycleFailure,
        CompactionLifecycleProgress,
        CompactionLifecycleStart,
        CompactionOutcome,
    )

_TERMINAL_STATUSES = frozenset({"success", "failed", "timeout"})


class ReplyCompactions:
    """The compaction entries of one reply, in start order, with a version bumped on every change."""

    def __init__(self) -> None:
        self._entries: list[dict[str, object]] = []
        self._tool_count: Callable[[], int] | None = None
        self._changed: Callable[[], None] | None = None
        self.version = 0

    def bind(self, *, tool_count: Callable[[], int], changed: Callable[[], None]) -> None:
        """Attach the delivery showing the reply: its visible tool count, and a nudge to write a change."""
        self._tool_count = tool_count
        self._changed = changed

    def unbind(self) -> None:
        """Detach the delivery, which no longer shows the reply."""
        self._tool_count = None
        self._changed = None

    def start(self, event: CompactionLifecycleStart) -> str:
        """Record a running compaction anchored after the tools shown so far, and return its id."""
        entry_id = uuid4().hex
        entry: dict[str, object] = {
            "id": entry_id,
            "phase": event.phase,
            "mode": event.mode,
            # Read once: the compaction sits where the reply stood when it began.
            "after_tool": self._tool_count() if self._tool_count is not None else 0,
            "status": "running",
            "started_at": time.time_ns() // 1_000_000,
            "summary_model": event.summary_model,
            "before_tokens": event.before_tokens,
            "runs_before": event.runs_before,
        }
        if event.history_budget_tokens is not None:
            entry["history_budget_tokens"] = event.history_budget_tokens
        self._entries.append(entry)
        self._note_change()
        return entry_id

    def update(self, entry_id: str, **fields: object) -> None:
        """Set the given fields on one entry; a ``None`` field keeps what the entry had, never writing null.

        A terminal status drops ``runs_remaining``, which only a running entry shows.
        """
        entry = next((entry for entry in self._entries if entry["id"] == entry_id), None)
        if entry is None:
            return
        entry.update({key: value for key, value in fields.items() if value is not None})
        if entry["status"] in _TERMINAL_STATUSES:
            entry.pop("runs_remaining", None)
        self._note_change()

    def content(self) -> list[dict[str, object]] | None:
        """Return a copy of the entries for the reply's content, or ``None`` when there are none."""
        if not self._entries:
            return None
        return copy.deepcopy(self._entries)

    def _note_change(self) -> None:
        self.version += 1
        if self._changed is not None:
            self._changed()


class ReplyCompactionLifecycle:
    """Compaction lifecycle that records automatic compactions on the reply and sends manual ones as notices."""

    def __init__(self, recorder: ReplyCompactions, notice: MatrixCompactionLifecycle) -> None:
        self._recorder = recorder
        self._notice = notice
        # The handles the notice returned, so a manual compaction's later events reach it.
        self._notice_ids: set[str] = set()

    async def start(self, event: CompactionLifecycleStart) -> str | None:
        """Send a manual compaction's notice, or record an automatic one on the reply."""
        if event.mode == "manual":
            notice_id = await self._notice.start(event)
            if notice_id is not None:
                self._notice_ids.add(notice_id)
            return notice_id
        return self._recorder.start(event)

    async def progress(self, event: CompactionLifecycleProgress) -> None:
        """Report persisted compaction progress to whichever sink the compaction started in."""
        if event.notice_event_id is None:
            return
        if event.notice_event_id in self._notice_ids:
            await self._notice.progress(event)
            return
        self._recorder.update(
            event.notice_event_id,
            status="running",
            after_tokens=event.after_tokens,
            compacted_run_count=event.compacted_run_count,
            runs_remaining=event.runs_remaining,
            duration_ms=event.duration_ms,
            summary_model=event.summary_model,
        )

    async def complete_success(self, outcome: CompactionOutcome) -> None:
        """Report a successful compaction to whichever sink it started in."""
        entry_id = outcome.lifecycle_notice_event_id
        if entry_id is None:
            return
        if entry_id in self._notice_ids:
            await self._notice.complete_success(outcome)
            return
        self._recorder.update(
            entry_id,
            status="success",
            after_tokens=outcome.after_tokens,
            compacted_run_count=outcome.compacted_run_count,
            history_budget_tokens=outcome.history_budget_tokens,
            duration_ms=outcome.duration_ms,
            summary_model=outcome.summary_model,
        )

    async def complete_failure(self, event: CompactionLifecycleFailure) -> None:
        """Report a failed compaction to whichever sink it started in."""
        if event.notice_event_id is None:
            return
        if event.notice_event_id in self._notice_ids:
            await self._notice.complete_failure(event)
            return
        self._recorder.update(
            event.notice_event_id,
            status=event.status,
            failure_reason=event.failure_reason,
            duration_ms=event.duration_ms,
            summary_model=event.summary_model,
        )
