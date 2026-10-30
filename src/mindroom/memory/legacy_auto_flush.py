"""Auto-flush state earlier releases wrote: the reply path's queue file, and progress and note tags in creation order."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.history.session_runs import start_seq

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from mindroom.session_storage.store import LoggedRun

_QUEUE_FILENAMES = ("memory_flush_state.json", "memory_flush_state.json.tmp")
# The released tag's spelling, which stays as released whatever the current tag becomes.
_RELEASED_TAG_PREFIX = "[auto_flush:"


# LEGACY_COMPAT: Push-model auto-flush queue file.
# Legacy format: `memory_flush_state.json`, and the `.tmp` copy a crash mid-write left, in the storage root, where each reply marked its session dirty for the worker.
# Last legacy release: v2026.10.335; replacement: v2026.10.336 reads stored conversations and keeps per-conversation progress in `tracking/memory_auto_flush.json`.
# Handling: both files are deleted when the new progress file is first created; turns still queued at the upgrade are not flushed, and no earlier conversation is flushed again.
# Coverage: tests/test_memory_auto_flush.py::test_the_first_pass_removes_the_old_queue_and_flushes_only_later_turns.
def remove_legacy_flush_queue(storage_root: Path) -> None:
    """Delete the old auto-flush queue, which the progress file replaces."""
    for name in _QUEUE_FILENAMES:
        (storage_root / name).unlink(missing_ok=True)


# LEGACY_COMPAT: Memory auto-flush progress kept as the run key of the last turn flushed.
# Legacy format: A conversation's entry in `tracking/memory_auto_flush.json` with a `marker` list, the
# `[created_at, run_id]` of the last turn flushed or given up in creation order (empty before the first flush), and
# a `recheck_at` time; the current entry keeps `seq` instead.
# Last legacy release: v2026.10.725; replacement: the next release keeps the log place of that turn as `seq`.
# Handling: When auto-flush next reads that conversation, a marker M becomes the place before the first completed
# turn whose `(created_at, run_id)` is after M, saved in the same progress write as that pass's; an empty marker
# starts like a conversation first seen. No turn the released code had not flushed is lost. A turn it skipped, one
# that sorted before a flushed turn created in the same second, is not guaranteed to be recovered, since the marker
# cannot tell it from one flushed normally. A flushed turn is read once more when an unflushed one precedes it in the
# log, between turns created in the same second, so one extraction can repeat facts. An entry
# held back for an approval is read again once that turn's approval ends and saves it, which updates the conversation.
# Coverage: tests/test_memory_auto_flush.py::test_released_progress_reads_only_the_turns_after_its_marker and
# tests/test_memory_auto_flush.py::test_released_progress_that_never_flushed_starts_like_a_conversation_first_seen.
def released_flush_start(entry: Mapping[str, Any], turns: Sequence[LoggedRun]) -> int | None:
    """Return the place a released entry's unflushed turns follow, or None when it is current or never flushed."""
    marker = entry.get("marker")
    if not isinstance(marker, list) or not marker:
        return None
    key = (int(marker[0]), str(marker[1]))
    return start_seq(turns, older=lambda run: (run.created_at, run.run_id) <= key)


# LEGACY_COMPAT: Memory auto-flush notes tagged with the last turn of their batch.
# Legacy format: A daily-note entry starting `[auto_flush:<session_id>:<run_id>]`, naming the last turn of a batch
# taken in creation order; current notes start `[auto_flush:<session_id>@<seq>]`.
# Last legacy release: v2026.10.725; replacement: the next release tags each note with its last turn's log place.
# Handling: A note written before its progress was saved covers the pending turns whose `(created_at, run_id)` is at
# most its run's and whose place in the log is at most its run's, the turns its batch had read, so neither a retry nor
# the progress conversion writes it again. A turn created after it but placed earlier, or created before it but
# completed after it, such as one approved after the upgrade, is still flushed.
# Coverage: tests/test_memory_auto_flush.py::test_a_released_note_keeps_its_turn_from_being_flushed_again,
# tests/test_memory_auto_flush.py::test_a_turn_completed_after_a_released_note_is_flushed_once.
def released_note_turns(notes: Iterable[str], session_id: str, turns: Sequence[LoggedRun]) -> list[LoggedRun]:
    """Return the turns among ``turns`` that a released note of the conversation names."""
    prefix = f"{_RELEASED_TAG_PREFIX}{session_id}:"
    named = {note[len(prefix) :].partition("]")[0] for note in notes if note.startswith(prefix)}
    return [turn for turn in turns if turn.run.run_id in named]


def released_note_covers(named: Sequence[LoggedRun], turn: LoggedRun) -> bool:
    """Return whether a released note naming one of ``named`` covers ``turn``: at most its run in both orders."""
    key = (turn.run.created_at, turn.run.run_id)
    return any(key <= (note.run.created_at, note.run.run_id) and turn.seq <= note.seq for note in named)
