"""Knowledge-graph ingestion progress earlier releases kept as run keys in creation order."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.history.session_runs import start_seq

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.session_storage.store import LoggedRun

# A released ingestion marker is `[created_at, run_id, piece]`.
_RELEASED_MARKER_LENGTH = 3


# LEGACY_COMPAT: Knowledge-graph ingestion progress kept as run keys.
# Legacy format: A conversation's entry in `knowledge_graph/ingestion/<graph>.json` whose `markers` map each episode
# kind to `[created_at, run_id, piece]`, the run key of the last run ingested and how many of its lines were covered
# (`RUN_DONE` when all), with a `recheck_at` time; a kind without a marker had every run new. The current markers
# are `[seq, piece]`.
# Last legacy release: v2026.10.696; replacement: the next release keeps the log place of that run in each marker.
# Handling: When ingestion next reads that conversation, each kind's marker M becomes `[place, RUN_DONE]`, the place
# before the first completed turn whose `(created_at, run_id)` is after M, or not before it when M stopped part-way
# through a split run, so that run is rendered again from its start and its done pieces repeat once, repeating their
# extraction; a kind without a marker starts at the log's beginning, and an entry with no marker at all starts as a
# conversation first seen does. The entry is saved in the same write as that pass's progress. No turn the released code had not ingested is lost. A turn it skipped, one that sorted before an ingested
# turn created in the same second, is not guaranteed to be recovered. An ingested turn is rendered again when an
# uningested one precedes it in the log, which repeats its extraction cost and can add provenance, since episodes carry
# no stable id.
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
    """Return the place after the turns a released marker covers: up to its run, and that run once it was done.

    Part-way through a split run, that run is rendered again from its start.
    """
    key = (int(marker[0]), str(marker[1]))
    if int(marker[2]) >= run_done:
        return start_seq(turns, older=lambda run: (run.created_at, run.run_id) <= key)
    return start_seq(turns, older=lambda run: (run.created_at, run.run_id) < key)
