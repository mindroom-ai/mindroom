"""One-time removal of the queue file the reply path wrote for the old auto-flush."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_QUEUE_FILENAMES = ("memory_flush_state.json", "memory_flush_state.json.tmp")


# LEGACY_COMPAT: Push-model auto-flush queue file.
# Legacy format: `memory_flush_state.json`, and the `.tmp` copy a crash mid-write left, in the storage root, where each reply marked its session dirty for the worker.
# Last legacy release: v2026.10.335; replacement: v2026.10.336 reads stored conversations and keeps per-conversation progress in `tracking/memory_auto_flush.json`.
# Handling: both files are deleted when the new progress file is first created; turns still queued at the upgrade are not flushed, and no earlier conversation is flushed again.
# Coverage: tests/test_memory_auto_flush.py::test_the_first_pass_removes_the_old_queue_and_flushes_only_later_turns.
def remove_legacy_flush_queue(storage_root: Path) -> None:
    """Delete the old auto-flush queue, which the progress file replaces."""
    for name in _QUEUE_FILENAMES:
        (storage_root / name).unlink(missing_ok=True)
