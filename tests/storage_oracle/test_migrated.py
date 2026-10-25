"""Comparison 2: every storage golden and legacy input, converted to storage v2, shows what the Agno-layout store recorded.

Every difference is one of the named masks below, each a deliberate rule of the conversion (spec v4 4.5) or of the
v2 store. A mask asserts the new behavior where it shows, then presents the recorded and the observed side as one
store would, and ``_PINS`` keeps which inputs use which masks, so a change of where the two parts shows as a failure.
PLAN-3095 task 10f adds the recorded end states of the generated and named sequences.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.session_storage.codec import run_from_row
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_database
from mindroom.session_storage.schema import TableNames, table_names
from mindroom.session_storage.store_next import ConversationStore
from mindroom.usage.storage import quote_identifier
from tests.storage_oracle.legacy import EXTRA_RUN_IDS
from tests.storage_oracle.next_backend import V2_READERS
from tests.storage_oracle.record import FIXTURES, GOLDEN_DUMPS, decode, observe_database, storage_name_of
from tests.storage_oracle.test_recorded import first_difference

if TYPE_CHECKING:
    from pathlib import Path

# Each input: its dump and its recording.
_INPUTS = {
    **{f"goldens/{dump.stem}": (dump, FIXTURES / "goldens" / f"{dump.stem}.json") for dump in GOLDEN_DUMPS},
    **{
        f"legacy/{dump.stem}": (dump, dump.with_suffix(".json")) for dump in sorted((FIXTURES / "legacy").glob("*.sql"))
    },
}
MASKS: dict[str, str] = {
    "row_ids_fill_the_record": "spec v4 3.1 and the v2 codec: a run row's ids win, so a record without a session id "
    "reads with its row's",
    "stored_copy_hides_folded_messages": "spec v4 3.15, row 11",
    "fold_stores_cancelled_row": "spec v4 3.15, row 7, and 4.5: a fold whose run has no row gets a cancelled one",
    "repeated_message_id_derived": "spec v4 4.5, messages: a repeated id within one run gets a derived one",
    "embedded_members_become_member_rows": "spec v4 4.5: a paused team run's embedded members replace their rows",
    "members_of_members_join_their_team": "spec v4 4.5: members of members are re-parented to their top-level run",
    "undecodable_run_left_out": "spec v4 4.5 and decision 9: a row that does not decode is left out, as if never stored",
}
# The masks each input uses; every other input compares unmasked.
_PINS: dict[str, set[str]] = {
    "goldens/agent_code": {"stored_copy_hides_folded_messages"},
    "legacy/captured__legacy_agno2_runs__blob_entries_a_read_could_not_address_are_dropped": {
        "row_ids_fill_the_record",
    },
    "legacy/captured__legacy_agno2_runs__blob_runs_keep_the_order_agno_read_them_in": {"row_ids_fill_the_record"},
    "legacy/captured__legacy_compaction_state__an_agent_named_agno_migrates_the_agno_runs_table": {
        "row_ids_fill_the_record",
    },
    "legacy/captured__legacy_compaction_state__opening_storage_adopts_legacy_summary_tombstones_and_seen_ids_once": {
        "row_ids_fill_the_record",
    },
    "legacy/captured__legacy_compaction_state__opening_storage_leaves_uncompacted_scopes_and_their_seen_ids_alone": {
        "row_ids_fill_the_record",
    },
    "legacy/captured__legacy_compaction_state__opening_storage_numbers_rows_an_old_writer_stored_without_an_index": {
        "row_ids_fill_the_record",
    },
    "legacy/synthetic__duplicate_message_id": {"repeated_message_id_derived"},
    "legacy/synthetic__embedded_paused_member_advanced": {"embedded_members_become_member_rows"},
    "legacy/synthetic__members_of_members": {"members_of_members_join_their_team"},
    "legacy/synthetic__orphan_segment": {"fold_stores_cancelled_row"},
    "legacy/synthetic__undecodable_run_data": {"undecodable_run_left_out"},
}


@dataclass(frozen=True)
class _Input:
    """What an input's Agno-layout tables hold, read with plain SQL: the facts the masks need."""

    folded: dict[str, set[str]] = field(default_factory=dict)
    rowless_fold_runs: set[str] = field(default_factory=set)
    message_ids: dict[str, list[str]] = field(default_factory=dict)
    undecodable: set[str] = field(default_factory=set)


def _input_facts(dump: str) -> _Input:
    names = TableNames(storage_name_of(dump))
    facts = _Input()
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(dump)
        tables = table_names(connection)
        runs = quote_identifier(names.runs)
        stored = set()
        if names.runs in tables:
            for run_id, kind, run_data in connection.execute(f"SELECT run_id, run_type, run_data FROM {runs}"):  # noqa: S608
                stored.add(run_id)
                try:
                    data = json.loads(run_data)
                    run_from_row(kind, run_data)
                except ValueError:
                    facts.undecodable.add(run_id)
                    continue
                facts.message_ids[run_id] = [message["id"] for message in data.get("messages") or []]
        if names.summaries in tables:
            for run_id, segment in connection.execute(
                f"SELECT segment_run_id, segment FROM {quote_identifier(names.summaries)} WHERE segment IS NOT NULL",  # noqa: S608
            ):
                facts.folded.setdefault(run_id, set()).update(
                    message["id"] for message in json.loads(segment)["messages"]
                )
                if run_id not in stored:
                    facts.rowless_fold_runs.add(run_id)
    return facts


def _open_converted(storage_name: str, root: Path) -> ConversationStore:
    names = TableNames(storage_name)
    database = names.database_path(root)
    migrate_conversation_database(database, names)
    return ConversationStore(database)


def _observed(dump: str, root: Path, extra_run_ids: dict[str, list[str]] | None) -> dict[str, Any]:
    return observe_database(dump, root, extra_run_ids, open_store=_open_converted, readers=V2_READERS)


def _without_runs(dump: str, run_ids: set[str]) -> str:
    """Return ``dump`` without the run rows ``run_ids``."""
    names = TableNames(storage_name_of(dump))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(dump)
        connection.execute(
            f"DELETE FROM {quote_identifier(names.runs)} WHERE run_id IN (SELECT value FROM json_each(?))",  # noqa: S608
            (json.dumps(sorted(run_ids)),),
        )
        return "\n".join(connection.iterdump()) + "\n"


def _runs(observed: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every run dict ``observed`` shows: its listed and loaded runs and the members they embed."""
    shown = [run for key in ("list_runs", "loaded_runs") if isinstance(observed[key], list) for run in observed[key]]
    return [*shown, *(member for run in shown for member in run.get("member_responses") or [])]


def _messages(value: object) -> list[dict[str, Any]]:
    """Return every message dict inside ``value``."""
    if isinstance(value, list):
        return [message for item in value for message in _messages(item)]
    if not isinstance(value, dict):
        return []
    inner = [message for item in value.values() for message in _messages(item)]
    return [value, *inner] if "role" in value and "id" in value else inner


def _rows_fill_records(recorded: dict[str, Any], observed: dict[str, Any], session_id: str, used: set[str]) -> None:
    for run in _runs(observed):
        assert run["session_id"] == session_id
    for run in _runs(recorded):
        if "session_id" not in run:
            run["session_id"] = session_id
            used.add("row_ids_fill_the_record")


def _hide_folded(recorded: dict[str, Any], facts: _Input, used: set[str]) -> None:
    for run in recorded["list_runs"] if isinstance(recorded["list_runs"], list) else []:
        folded = facts.folded.get(run["run_id"], set())
        kept = [message for message in run["messages"] if message["id"] not in folded]
        if kept != run["messages"]:
            run["messages"] = kept
            used.add("stored_copy_hides_folded_messages")


def _hide_fold_rows(entry: dict[str, Any], facts: _Input, extra: list[str], used: set[str]) -> None:
    observed = entry["observed"]
    created = {
        run["run_id"]
        for run in _runs(observed)
        if run["run_id"] in facts.rowless_fold_runs or run.get("parent_run_id") in facts.rowless_fold_runs
    }
    for run in _runs(observed):
        if run["run_id"] in facts.rowless_fold_runs:
            assert (run["status"], run["messages"]) == ("CANCELLED", [])
    if created:
        used.add("fold_stores_cancelled_row")
        for key in ("list_runs", "loaded_runs"):
            observed[key] = [run for run in observed[key] if run["run_id"] not in created]
        entry["run_ids"] = sorted((set(entry["run_ids"]) - created) | set(extra))


def _restore_repeated_ids(observed: dict[str, Any], facts: _Input, used: set[str]) -> None:
    for run in _runs(observed):
        ids = [message["id"] for message in run["messages"]]
        assert len(set(ids)) == len(ids), run["run_id"]
    original: dict[str, str] = {}
    for run_id, ids in facts.message_ids.items():
        for position, message_id in enumerate(ids):
            if message_id in ids[:position]:
                original[f"{run_id}:{position}"] = message_id
    for message in _messages(observed):
        if message["id"] in original:
            message["id"] = original[message["id"]]
            used.add("repeated_message_id_derived")


def _embedded_members(recorded: dict[str, Any], used: set[str]) -> None:
    embedded = {member["run_id"]: member for run in _runs(recorded) for member in run.get("member_responses") or []}
    for key in ("list_runs", "loaded_runs"):
        runs = recorded[key] if isinstance(recorded[key], list) else []
        for position, run in enumerate(runs):
            member = embedded.get(run["run_id"])
            if member is not None and member != run:
                runs[position] = deepcopy(member)
                used.add("embedded_members_become_member_rows")


def _members_join_team(recorded: dict[str, Any], used: set[str]) -> None:
    parents = {run["run_id"]: run.get("parent_run_id") for run in _runs(recorded)}
    for run in _runs(recorded):
        parent = run.get("parent_run_id")
        while parents.get(parent) is not None:
            parent = parents[parent]
        if parent != run.get("parent_run_id"):
            run["parent_run_id"] = parent
            used.add("members_of_members_join_their_team")


def _masked(
    name: str,
    recorded: dict[str, Any],
    observed: dict[str, Any],
    facts: _Input,
    root: Path,
) -> set[str]:
    """Show ``recorded`` and ``observed`` as one store would, mask by mask; return the masks that changed either."""
    used: set[str] = set()
    dump, _ = _INPUTS[name]
    extra = EXTRA_RUN_IDS.get(dump.stem, {})
    if facts.undecodable:
        # The Agno-layout store failed every read of the session; leaving the row out reads as never having stored it.
        for entry in recorded["sessions"].values():
            assert "JSONDecodeError" in entry["observed"]["list_runs"]["error"]
        expected = _observed(_without_runs(dump.read_text(encoding="utf-8"), facts.undecodable), root, extra)
        recorded.clear()
        recorded.update(expected)
        used.add("undecodable_run_left_out")
    for session_id, entry in observed["sessions"].items():
        old = recorded["sessions"][session_id]
        if "observed" not in entry:
            continue
        _rows_fill_records(old["observed"], entry["observed"], session_id, used)
        _hide_folded(old["observed"], facts, used)
        _hide_fold_rows(entry, facts, extra.get(session_id, []), used)
        _restore_repeated_ids(entry["observed"], facts, used)
        _embedded_members(old["observed"], used)
        _members_join_team(old["observed"], used)
    return used


@pytest.mark.parametrize("name", sorted(_INPUTS))
def test_a_converted_input_shows_its_recording_but_for_named_masks(name: str, tmp_path: Path) -> None:
    """Each golden and legacy input, converted, gives every recorded observable once its masks undo their changes."""
    dump, recording = _INPUTS[name]
    text = dump.read_text(encoding="utf-8")
    recorded = decode(recording.read_text(encoding="utf-8"))
    observed = _observed(text, tmp_path / "converted", EXTRA_RUN_IDS.get(dump.stem))
    used = _masked(name, recorded, observed, _input_facts(text), tmp_path / "expected")
    assert first_difference(recorded, observed) is None, first_difference(recorded, observed)
    assert used == _PINS.get(name, set())


def test_every_mask_is_used_and_every_pin_names_an_input() -> None:
    """Each named mask explains a difference some input shows, and no pin is stale."""
    assert set(_PINS) <= set(_INPUTS)
    assert set().union(*_PINS.values()) == set(MASKS)
