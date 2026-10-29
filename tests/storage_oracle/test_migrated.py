"""Comparison 2: every storage golden, legacy input, and recorded end state, converted, shows what was recorded.

The recordings are the Agno-layout store's: of each golden and legacy input opened as it opened them, and of each
generated and named sequence after its last action, beside the dump of the database that action left. Every
difference is one of the named masks below, each a deliberate rule of the conversion (spec v4 4.5) or of the current
store. A mask asserts the new behavior where it shows, then presents the recorded and the observed side as one store
would, and the pins keep which inputs use which masks, so a change in where the two stores part fails.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.history.summary_message import is_scope_summary
from mindroom.history.summary_rows import cut_at_event
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames, agno_schema_sql, run_from_row
from mindroom.session_storage.schema import TableNames, quote_identifier, table_names
from mindroom.session_storage.types import ConversationSession
from tests.llm.agno_dict_helpers import PAUSED_NOTICE_OPENINGS
from tests.storage_oracle.actions import Scenario
from tests.storage_oracle.backend import SESSION_ID
from tests.storage_oracle.determinism import deterministic
from tests.storage_oracle.observe import observe
from tests.storage_oracle.record import (
    FIXTURES,
    GOLDEN_DUMPS,
    SEQUENCES,
    decode,
    first_difference,
    observe_database,
    sequence_id,
    storage_name_of,
)

if TYPE_CHECKING:
    from pathlib import Path

# Each golden and legacy input: its dump and its recording.
_INPUTS = {
    **{f"goldens/{dump.stem}": (dump, FIXTURES / "goldens" / f"{dump.stem}.json") for dump in GOLDEN_DUMPS},
    **{
        f"legacy/{dump.stem}": (dump, dump.with_suffix(".json")) for dump in sorted((FIXTURES / "legacy").glob("*.sql"))
    },
}
MASKS: dict[str, str] = {
    "row_ids_fill_the_record": "spec v4 3.1 and the v2 codec: a run row's ids win, so a record without a session id "
    "reads with its row's",
    "history_copies_not_stored": "spec v4 3.15, row 6, and 4.5: a stored run's history copies are dropped",
    "stored_copy_hides_folded_messages": "spec v4 3.15, row 11",
    "fold_stores_cancelled_row": "spec v4 3.15, row 7, and 4.5: a fold whose run has no row gets a cancelled one",
    "repeated_message_id_derived": "spec v4 4.5, messages: a repeated id within one run gets a derived one",
    "embedded_members_become_member_rows": "spec v4 4.5: a paused team run's embedded members replace their rows",
    "members_of_members_join_their_team": "spec v4 4.5: members of members are re-parented to their top-level run",
    "undecodable_run_left_out": "spec v4 4.5 and decision 9: a row that does not decode is left out, as if never stored",
    "force_request_moves_into_metadata": "spec v4 4.5 and decision 5 (W9): a manual compaction request queued in the "
    "session state becomes its scope's metadata flag",
    "session_columns_encoded_once": "spec v4 4.5: session columns Agno 2 encoded twice are stored encoded once",
    "foreign_runs_take_the_database_agent": "#3115: an agent's database owns every top-level run in it, so a run "
    "naming another agent, which no release stored there, takes the agent's name as layout version 3 set it "
    "(``mindroom.session_storage.legacy_agent_session_owners``)",
    "paused_notice_reads_as_no_content": "#3106: a run whose content is a paused notice reads with no content "
    "(``mindroom.llm.legacy_run_content``)",
}
# The masks each golden and legacy input uses; every other input compares unmasked.
_PINS: dict[str, set[str]] = {
    "goldens/agent_code": {"stored_copy_hides_folded_messages"},
    "goldens/agent_helper": {"paused_notice_reads_as_no_content"},
    "goldens/agent_helper__before_run-delegation-approved": {"paused_notice_reads_as_no_content"},
    "goldens/team_squad": {"paused_notice_reads_as_no_content"},
    "goldens/team_squad__before_team-approved": {"paused_notice_reads_as_no_content"},
    "goldens/agno2_migrated": {"session_columns_encoded_once"},
    "goldens/agno_2_6_12_code_sessions": {"session_columns_encoded_once"},
    "legacy/captured__legacy_agno2_runs__an_unreadable_blob_is_cleared_without_rows": {"session_columns_encoded_once"},
    "legacy/captured__legacy_agno2_runs__blob_entries_a_read_could_not_address_are_dropped": {
        "row_ids_fill_the_record",
        "session_columns_encoded_once",
    },
    "legacy/captured__legacy_agno2_runs__blob_runs_become_rows_when_the_summary_rows_migrate": {
        "session_columns_encoded_once",
    },
    "legacy/captured__legacy_agno2_runs__blob_runs_keep_the_order_agno_read_them_in": {
        "row_ids_fill_the_record",
        "session_columns_encoded_once",
    },
    "legacy/captured__legacy_compaction_state__an_agno_2_database_migrates_before_the_owned_store_opens_it": {
        "session_columns_encoded_once",
    },
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
    "legacy/captured__archive_migration__two_scopes_keep_separate_summary_rows": {
        "foreign_runs_take_the_database_agent",
    },
    "legacy/synthetic__duplicate_message_id": {"repeated_message_id_derived"},
    "legacy/synthetic__embedded_paused_member_advanced": {"embedded_members_become_member_rows"},
    "legacy/synthetic__members_of_members": {"members_of_members_join_their_team"},
    "legacy/synthetic__orphan_segment": {"fold_stores_cancelled_row"},
    "legacy/synthetic__undecodable_run_data": {"undecodable_run_left_out"},
}
# The masks each recorded end state uses; every other end state compares unmasked.
_END_STATE_PINS: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        (f"generated/{seed}" for seed in (10, 11, 12, 15, 17, 23, 25, 28, 29, 34, 38)),
        frozenset({"fold_stores_cancelled_row"}),
    ),
    "generated/2": frozenset({"fold_stores_cancelled_row", "force_request_moves_into_metadata"}),
    "generated/3": frozenset({"history_copies_not_stored"}),
    "generated/13": frozenset({"fold_stores_cancelled_row", "history_copies_not_stored"}),
    "generated/20": frozenset({"embedded_members_become_member_rows"}),
    "generated/39": frozenset({"history_copies_not_stored"}),
    "generated/8": frozenset({"force_request_moves_into_metadata"}),
    "generated/19": frozenset({"force_request_moves_into_metadata"}),
}
# Where an older release also queued a manual compaction request: the scope keys listed in the session state.
_PENDING_KEY = "mindroom_pending_compaction_scope_keys"


@dataclass(frozen=True)
class _Input:
    """What an input's Agno-layout tables hold, read with plain SQL: the facts the masks need."""

    stored: set[str] = field(default_factory=set)
    folded: dict[str, set[str]] = field(default_factory=dict)
    rowless_fold_runs: dict[str, set[str]] = field(default_factory=dict)
    message_ids: dict[str, list[str]] = field(default_factory=dict)
    undecodable: set[str] = field(default_factory=set)

    def extra_run_ids(self) -> dict[str, list[str]]:
        """Return each session's runs that folds name but have no row, whose folded tool calls a recording read too."""
        return {session_id: sorted(run_ids) for session_id, run_ids in self.rowless_fold_runs.items()}


def _input_facts(dump: str) -> _Input:
    names = AgnoTableNames(storage_name_of(dump))
    facts = _Input()
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(dump)
        tables = table_names(connection)
        if names.runs in tables:
            for run_id, kind, run_data in connection.execute(
                f"SELECT run_id, run_type, run_data FROM {quote_identifier(names.runs)}",  # noqa: S608
            ):
                facts.stored.add(run_id)
                try:
                    data = json.loads(run_data)
                    run_from_row(kind, run_data)
                except ValueError:
                    facts.undecodable.add(run_id)
                    continue
                facts.message_ids[run_id] = [message["id"] for message in data.get("messages") or []]
        if names.summaries in tables:
            for session_id, run_id, segment in connection.execute(
                f"SELECT session_id, segment_run_id, segment FROM {quote_identifier(names.summaries)} "  # noqa: S608
                "WHERE segment IS NOT NULL",
            ):
                facts.folded.setdefault(run_id, set()).update(
                    message["id"] for message in json.loads(segment)["messages"]
                )
                if run_id not in facts.stored:
                    facts.rowless_fold_runs.setdefault(session_id, set()).add(run_id)
    return facts


def _adopting_agent(dump: str) -> str | None:
    """Return the agent whose sessions, all its, hold a top-level agent run naming another agent, which it adopts."""
    names = AgnoTableNames(storage_name_of(dump))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(dump)
        if not {names.sessions, names.runs} <= table_names(connection):
            return None
        owners = connection.execute(
            f"SELECT DISTINCT session_type, agent_id FROM {quote_identifier(names.sessions)}",  # noqa: S608
        ).fetchall()
        if len(owners) != 1 or owners[0][0] != "agent":
            return None
        agent = owners[0][1]
        foreign = connection.execute(
            f"SELECT 1 FROM {quote_identifier(names.runs)} WHERE parent_run_id IS NULL AND run_type = 'agent' "  # noqa: S608
            "AND agent_id IS NOT ?",
            (agent,),
        ).fetchone()
    return agent if foreign is not None else None


def _adopted(dump: str, agent: str) -> str:
    """Return ``dump`` with its top-level agent runs naming ``agent``, as its conversion leaves them."""
    names = AgnoTableNames(storage_name_of(dump))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(dump)
        connection.execute(
            f"UPDATE {quote_identifier(names.runs)} SET agent_id = ?1, run_data = json_set(run_data, '$.agent_id', ?1) "  # noqa: S608
            "WHERE parent_run_id IS NULL AND run_type = 'agent'",
            (agent,),
        )
        return "\n".join(connection.iterdump()) + "\n"


def _without_runs(dump: str, run_ids: set[str]) -> str:
    """Return ``dump`` without the run rows ``run_ids``."""
    names = AgnoTableNames(storage_name_of(dump))
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.executescript(dump)
        connection.execute(
            f"DELETE FROM {quote_identifier(names.runs)} WHERE run_id IN (SELECT value FROM json_each(?))",  # noqa: S608
            (json.dumps(sorted(run_ids)),),
        )
        return "\n".join(connection.iterdump()) + "\n"


def _end_state(dump: str, scenario: Scenario, root: Path) -> dict[str, Any]:
    """Restore a recorded end state, open it as the runtime does, and observe it at its last action's clock."""
    storage_name = storage_name_of(dump)
    database = TableNames(storage_name).database_path(root)
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(dump)
    with deterministic() as determinism:
        determinism.at(len(scenario.actions) - 1)
        store = create_state_storage(storage_name, root, HistoryScope(kind=scenario.kind, scope_id=storage_name))
        observed = observe(store, SESSION_ID, HistoryScope(kind=scenario.kind, scope_id=storage_name))
    return json.loads(json.dumps(observed, sort_keys=True))


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


def _unminted(observed: dict[str, Any]) -> None:
    """Give the scope summary message each request mints one id, since only the observing process names it."""
    resumes = observed["resume_requests"] if isinstance(observed["resume_requests"], dict) else {}
    requests = [observed["request"], *(resume["request"] for resume in resumes.values())]
    for message in (message for request in requests if isinstance(request, list) for message in request):
        if is_scope_summary(Message.from_dict(message)):
            message["id"] = "minted"


def _rows_fill_records(recorded: dict[str, Any], observed: dict[str, Any], session_id: str, used: set[str]) -> None:
    for run in _runs(observed):
        assert run["session_id"] == session_id
    for run in _runs(recorded):
        if "session_id" not in run:
            run["session_id"] = session_id
            used.add("row_ids_fill_the_record")


def _paused_notices_read_as_no_content(recorded: dict[str, Any], observed: dict[str, Any], used: set[str]) -> None:
    for run in _runs(observed):
        assert not str(run.get("content", "")).startswith(PAUSED_NOTICE_OPENINGS), run["run_id"]
    for run in _runs(recorded):
        if str(run.get("content", "")).startswith(PAUSED_NOTICE_OPENINGS):
            del run["content"]
            used.add("paused_notice_reads_as_no_content")


def _drop_history_copies(recorded: dict[str, Any], observed: dict[str, Any], used: set[str]) -> None:
    for run in _runs(observed):
        assert not any(message.get("from_history") for message in run["messages"]), run["run_id"]
    for run in _runs(recorded):
        kept = [message for message in run["messages"] if not message.get("from_history")]
        if kept != run["messages"]:
            run["messages"] = kept
            used.add("history_copies_not_stored")


def _hide_folded(recorded: dict[str, Any], facts: _Input, used: set[str]) -> None:
    for run in recorded["list_runs"] if isinstance(recorded["list_runs"], list) else []:
        folded = facts.folded.get(run["run_id"], set())
        kept = [message for message in run["messages"] if message["id"] not in folded]
        if kept != run["messages"]:
            run["messages"] = kept
            used.add("stored_copy_hides_folded_messages")


def _hide_fold_rows(observed: dict[str, Any], facts: _Input, used: set[str]) -> set[str]:
    """Leave out the rows the conversion stored for folds whose run had none; return their ids.

    Such a run's members that had rows stay listed, but no longer load with it, since no load reached them before.
    """
    rowless = set().union(*facts.rowless_fold_runs.values())
    for run in _runs(observed):
        if run["run_id"] in rowless:
            assert (run["status"], run["messages"]) == ("CANCELLED", [])
    created = {
        run["run_id"]
        for run in _runs(observed)
        if run["run_id"] in rowless or (run.get("parent_run_id") in rowless and run["run_id"] not in facts.stored)
    }
    if not created:
        return created
    used.add("fold_stores_cancelled_row")
    if isinstance(observed["list_runs"], list):
        observed["list_runs"] = [run for run in observed["list_runs"] if run["run_id"] not in created]
    if isinstance(observed["loaded_runs"], list):
        observed["loaded_runs"] = [
            run
            for run in observed["loaded_runs"]
            if run["run_id"] not in rowless and run.get("parent_run_id") not in rowless
        ]
    return created


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


def _states(observed: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the session states ``observed`` shows, the loaded session's and the row's, where each is an object."""
    session, row = observed["session"], observed["row"]
    data = row.get("session_data") if isinstance(row, dict) else None
    shown = (
        session.get("session_state") if isinstance(session, dict) else None,
        data.get("session_state") if isinstance(data, dict) else None,
    )
    return [state for state in shown if isinstance(state, dict)]


def _force_requests_in_metadata(recorded: dict[str, Any], observed: dict[str, Any], used: set[str]) -> None:
    """Show a request an older release queued in the session state as the metadata flag the conversion made of it."""
    for state in _states(observed):
        assert _PENDING_KEY not in state
    requested = [state.pop(_PENDING_KEY) for state in _states(recorded) if _PENDING_KEY in state]
    if not requested:
        return
    metadata = recorded["session"]["metadata"] or {}
    compaction = metadata.get("mindroom_compaction", {"version": 2, "states": {}})
    flags = {scope_key: {"force_compact_before_next_run": True} for scope_key in requested[0]}
    states = {**compaction["states"], **flags}
    recorded["session"]["metadata"] = {**metadata, "mindroom_compaction": {**compaction, "states": states}}
    used.add("force_request_moves_into_metadata")


def _session_columns_encoded_once(recorded: dict[str, Any], observed: dict[str, Any], used: set[str]) -> None:
    """Show session data Agno 2 encoded twice, which a read of the row decodes to text, as the object stored once."""
    row, old = observed["row"], recorded["row"]
    if not (isinstance(row, dict) and isinstance(old, dict)):
        return
    assert not isinstance(row.get("session_data"), str)
    if isinstance(old.get("session_data"), str):
        old["session_data"] = json.loads(old["session_data"])
        used.add("session_columns_encoded_once")


def _mask_session(
    recorded: dict[str, Any],
    observed: dict[str, Any],
    facts: _Input,
    session_id: str,
    used: set[str],
) -> set[str]:
    """Show one session's recorded and observed observables as one store would; return the runs the fold mask hid."""
    _rows_fill_records(recorded, observed, session_id, used)
    _paused_notices_read_as_no_content(recorded, observed, used)
    _session_columns_encoded_once(recorded, observed, used)
    _force_requests_in_metadata(recorded, observed, used)
    _drop_history_copies(recorded, observed, used)
    _hide_folded(recorded, facts, used)
    created = _hide_fold_rows(observed, facts, used)
    _restore_repeated_ids(observed, facts, used)
    _embedded_members(recorded, used)
    _members_join_team(recorded, used)
    return created


def _masked(
    dump: str,
    recorded: dict[str, Any],
    observed: dict[str, Any],
    facts: _Input,
    root: Path,
) -> set[str]:
    """Show an input's recorded and observed sessions as one store would, mask by mask; return the masks used."""
    used: set[str] = set()
    extra = facts.extra_run_ids()
    if facts.undecodable:
        # The Agno-layout store failed every read of the session; leaving the row out reads as never having stored it.
        for entry in recorded["sessions"].values():
            assert "JSONDecodeError" in entry["observed"]["list_runs"]["error"]
        expected = observe_database(_without_runs(dump, facts.undecodable), root, extra)
        recorded.clear()
        recorded.update(expected)
        used.add("undecodable_run_left_out")
    if (agent := _adopting_agent(dump)) is not None:
        # The Agno-layout store read each run under the agent it named; the conversion adopts them as layout version 3
        # did at the agent's own open, so the recording is what the same database shows with its runs adopted.
        recorded.clear()
        recorded.update(observe_database(_adopted(dump, agent), root, extra))
        used.add("foreign_runs_take_the_database_agent")
    for session_id, entry in observed["sessions"].items():
        if "observed" not in entry:
            continue
        old = recorded["sessions"][session_id]["observed"]
        created = _mask_session(old, entry["observed"], facts, session_id, used)
        entry["run_ids"] = sorted((set(entry["run_ids"]) - created) | set(extra.get(session_id, [])))
    return used


@pytest.mark.parametrize("name", sorted(_INPUTS))
def test_a_converted_input_shows_its_recording_but_for_named_masks(name: str, tmp_path: Path) -> None:
    """Each golden and legacy input, converted, gives every recorded observable once its masks undo their changes."""
    dump, recording = _INPUTS[name]
    text = dump.read_text(encoding="utf-8")
    recorded = decode(recording.read_text(encoding="utf-8"))
    facts = _input_facts(text)
    observed = observe_database(text, tmp_path / "converted", facts.extra_run_ids())
    used = _masked(text, recorded, observed, facts, tmp_path / "expected")
    assert first_difference(recorded, observed) is None, first_difference(recorded, observed)
    assert used == _PINS.get(name, set())


@pytest.mark.parametrize("path", SEQUENCES, ids=sequence_id)
def test_a_converted_end_state_shows_its_last_recorded_observables_but_for_named_masks(
    path: Path,
    tmp_path: Path,
) -> None:
    """Each recorded sequence's final database, converted, shows what the recording kept after its last action."""
    recorded = decode(path.read_text(encoding="utf-8"))
    dump = path.with_suffix(".sql").read_text(encoding="utf-8")
    final = recorded["final"]
    observed = _end_state(dump, Scenario.from_json(recorded["scenario"]), tmp_path)
    used: set[str] = set()
    _mask_session(final, observed, _input_facts(dump), SESSION_ID, used)
    _unminted(final)
    _unminted(observed)
    assert first_difference(final, observed) is None, first_difference(final, observed)
    assert used == _END_STATE_PINS.get(sequence_id(path), frozenset())


def test_a_fold_run_a_retirement_removed_converts_without_a_row(tmp_path: Path) -> None:
    """A run a retirement removed after it folded stays without a row, so a redaction of what it read cuts nothing.

    The Agno-layout store's retirement deleted the run and kept its fold's summary row behind the barrier, so the run
    never loaded nor held an event again; no recording holds that shape.
    """
    names = AgnoTableNames("code")
    database = names.database_path(tmp_path)
    database.parent.mkdir(parents=True)
    segment = {"run_id": "x", "metadata": {"matrix_seen_event_ids": ["$read-by-x"]}, "messages": [], "tools": []}
    with closing(sqlite3.connect(database)) as connection, connection:
        for statement in agno_schema_sql(names):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO code_sessions (session_id, session_type, agent_id, created_at) VALUES ('s', 'agent', 'code', 0)",
        )
        connection.execute(
            "INSERT INTO code_sessions_summaries (session_id, scope_key, after_run_index, summary, legacy, created_at, "
            "segment_run_id, segment) VALUES ('s', 'agent:code', -1, 'legacy summary', 1, 0, NULL, NULL), "
            "('s', 'agent:code', -1, 'S:legacy,x~', 0, 0, 'x', ?), ('s', 'agent:code', -1, NULL, 0, 0, NULL, NULL)",
            (json.dumps(segment),),
        )
        connection.execute("INSERT INTO code_sessions_removed_runs (run_id, session_id) VALUES ('x', 's')")
    store = create_state_storage("code", tmp_path, HistoryScope(kind="agent", scope_id="code"))
    session = ConversationSession(session_id="s", kind="agent", owner_id="code")
    later = RunRecord(run_id="r0", agent_id="code", session_id="s", status=RunStatus.completed)

    assert store.save_run(session, later, record_usage=False) == "written"
    assert store.get_run("x") is None
    assert store.save_run(session, RunRecord(run_id="x", agent_id="code"), record_usage=False) == "removed"
    assert not cut_at_event(store, session_id="s", scope_key="agent:code", event_id="$read-by-x", match="any")
    assert [run.run_id for run in store.list_runs("s")] == ["r0"]


def test_every_mask_is_used_and_every_pin_names_an_input() -> None:
    """Each named mask explains a difference some input shows, and no pin is stale."""
    assert set(_PINS) <= set(_INPUTS)
    assert set(_END_STATE_PINS) <= {sequence_id(path) for path in SEQUENCES}
    assert set().union(*_PINS.values(), *_END_STATE_PINS.values()) == set(MASKS)
