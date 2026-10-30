"""Progress that background readers saved, and notes they tagged, in creation order before they followed log order."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.history.session_runs import start_seq

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from mindroom.session_storage.store import LoggedRun

_RELEASED_TAG_PREFIX = "[auto_flush:"
# A released ingestion marker is `[created_at, run_id, piece]`.
_RELEASED_MARKER_LENGTH = 3


# LEGACY_COMPAT: Memory auto-flush progress kept as the run key of the last turn flushed.
# Legacy format: A conversation's entry in `tracking/memory_auto_flush.json` with a `marker` list, the
# `[created_at, run_id]` of the last turn flushed or given up in creation order (empty before the first flush), and
# a `recheck_at` time; the current entry keeps `seq` instead.
# Last legacy release: v2026.10.696; replacement: the next release keeps the log place of that turn as `seq`.
# Handling: When auto-flush next reads that conversation, a marker M becomes the place before the first completed
# turn whose `(created_at, run_id)` is after M, saved in the same progress write as that pass's; an empty marker
# starts like a conversation first seen. No turn the released code had not flushed is lost. A turn it skipped,
# such as one approved after the turns after it were flushed, is not guaranteed to be recovered, since the marker
# cannot tell it from one flushed normally. A flushed turn is read once more when an unflushed one precedes it in the
# log (after an approval, or between turns created in the same second), so one extraction can repeat facts. An entry
# held back for an approval is read again once that turn's approval ends and saves it, which updates the conversation.
# Coverage: tests/test_memory_auto_flush.py::test_released_progress_reads_only_the_turns_after_its_marker and
# tests/test_memory_auto_flush.py::test_released_progress_that_never_flushed_starts_like_a_conversation_first_seen.
def released_flush_start(entry: Mapping[str, Any], turns: Sequence[LoggedRun]) -> int | None:
    """Return the place a released entry's unflushed turns follow, or None when it is current or never flushed."""
    marker = entry.get("marker")
    if not isinstance(marker, list) or not marker:
        return None
    return _start_after(marker, turns, inclusive=True)


# LEGACY_COMPAT: Knowledge-graph ingestion progress kept as run keys.
# Legacy format: A conversation's entry in `knowledge_graph/ingestion/<graph>.json` whose `markers` map each episode
# kind to `[created_at, run_id, piece]`, the run key of the last run ingested and how many of its lines were covered
# (`RUN_DONE` when all), with a `recheck_at` time; a kind without a marker had every run new. The current markers
# are `[seq, piece]`.
# Last legacy release: v2026.10.696; replacement: the next release keeps the log place of that run in each marker.
# Handling: When ingestion next reads that conversation, each kind's marker M becomes `[place, RUN_DONE]`, the place
# before the first completed turn whose `(created_at, run_id)` is after M, or not before it when M stopped part-way
# through a split run, so that run is rendered again from its start and its done pieces repeat once, repeating their
# extraction; a kind without a marker starts at the log's beginning. The entry is saved in the same write as that pass's
# progress. No turn the released code had not ingested is lost. A turn it skipped, such as one approved after the turns
# after it were ingested, is not guaranteed to be recovered. An ingested turn is rendered again when an uningested one
# precedes it in the log, which repeats its extraction cost and can add provenance, since episodes carry no stable id.
# Coverage: tests/test_knowledge_graph_ingestion.py::test_released_progress_ingests_only_the_turns_after_its_markers
# and tests/test_knowledge_graph_ingestion.py::test_released_progress_part_way_through_a_run_renders_it_again.
def released_ingestion_markers(
    markers: Mapping[str, Sequence[int | str]],
    turns: Sequence[LoggedRun],
    *,
    kinds: Sequence[str],
    run_done: int,
) -> dict[str, list[int]] | None:
    """Return the current markers of every kind for released ``markers``, or None when they are current or empty."""
    if not markers or any(len(marker) != _RELEASED_MARKER_LENGTH for marker in markers.values()):
        return None
    return {
        kind: [_released_place(marker, turns, run_done) if (marker := markers.get(kind)) else 0, run_done]
        for kind in kinds
    }


def _released_place(marker: Sequence[int | str], turns: Sequence[LoggedRun], run_done: int) -> int:
    # Part-way through a split run, that run is rendered again from its start.
    return _start_after(marker, turns, inclusive=int(marker[2]) >= run_done)


def _start_after(marker: Sequence[int | str], turns: Sequence[LoggedRun], *, inclusive: bool) -> int:
    """Return the place after the turns a released marker's run key covers: up to it, and the run itself if inclusive."""
    key = (int(marker[0]), str(marker[1]))
    if inclusive:
        return start_seq(turns, older=lambda run: (run.created_at, run.run_id) <= key)
    return start_seq(turns, older=lambda run: (run.created_at, run.run_id) < key)


# LEGACY_COMPAT: Memory auto-flush notes tagged with the last turn of their batch.
# Legacy format: A daily-note entry starting `[auto_flush:<session_id>:<run_id>]`, naming the last turn of a batch
# taken in creation order; current notes start `[auto_flush:<session_id>@<seq>]`.
# Last legacy release: v2026.10.696; replacement: the next release tags each note with its last turn's log place.
# Handling: A note written before its progress was saved covers the pending turns whose `(created_at, run_id)` is at
# most its run's, as the released code read it, so neither a retry nor the progress conversion writes it again while
# a turn created after it, but placed earlier in the log, is still flushed.
# Coverage: tests/test_memory_auto_flush.py::test_a_released_note_keeps_its_turn_from_being_flushed_again.
def released_note_bound(notes: Iterable[str], session_id: str, turns: Sequence[LoggedRun]) -> tuple[int, str] | None:
    """Return the newest run key a released note of the conversation names among ``turns``, or None."""
    prefix = f"{_RELEASED_TAG_PREFIX}{session_id}:"
    named = {note[len(prefix) :].partition("]")[0] for note in notes if note.startswith(prefix)}
    return max(((turn.run.created_at, turn.run.run_id) for turn in turns if turn.run.run_id in named), default=None)
