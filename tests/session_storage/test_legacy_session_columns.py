"""Databases below layout version 4 convert once: the meta row names their owner, and each row's state gets a column.

The fixtures under ``tests/fixtures/conversation_layout_3`` are databases v2026.10.667's store wrote, with what every
reader showed of them at that release. Each test restores one, opens it as the runtime does, and checks what the readers
show now, what the converted file holds, and what an interrupted conversion and that older release do with it.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from mindroom.constants import resolve_runtime_paths
from mindroom.history.types import HistoryScope
from mindroom.session_storage import legacy_session_columns, preflight
from mindroom.session_storage.agent_storage import create_state_storage, prepare_conversation_database
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_storage
from mindroom.session_storage.legacy_conversion_backup import ConversionSpaceError
from mindroom.session_storage.legacy_session_columns import BACKUP_SUFFIX
from mindroom.session_storage.preflight import session_storage_preflight
from mindroom.session_storage.schema import LOG_VERSION, SESSION_COLUMNS, TableNames
from mindroom.session_storage.store import decoded_mapping
from mindroom.usage.stats_storage import _decode_session_usage
from mindroom.usage.storage import SYSTEM_USAGE_OWNER, SYSTEM_USAGE_STORAGE_NAME
from tests.session_storage.layout_3_readers import observe_database
from tests.storage_oracle.determinism import deterministic
from tests.storage_oracle.record import first_difference, storage_name_of

if TYPE_CHECKING:
    from mindroom.session_storage.store import ConversationStore

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "conversation_layout_3"
_TEAM = HistoryScope(kind="team", scope_id="crew")
_OWNERS = {"agent_code": HistoryScope(kind="agent", scope_id="code"), "team_crew": _TEAM, "system": SYSTEM_USAGE_OWNER}
# The step of the storage oracle's clock each recording's readers ran at, which a probe request's times read.
_STEPS = {"agent_code": 7, "team_crew": 3, "system": 2}
# The conversation a team bot's interrupted first reply started on its agent path: v2026.10.667 stored an agent row for
# it, so a team turn's load returned nothing and the usage report counted its usage under no team.
_AGENT_PATH = "agent-path"
# The sessions columns v2026.10.667 required of an existing database, frozen from its ``session_storage/schema.py``;
# its preflight archives a sessions directory that lacks any.
_OLDER_RELEASE_SESSION_COLUMNS = frozenset(
    {
        "session_id",
        "session_type",
        "agent_id",
        "team_id",
        "user_id",
        "session_data",
        "metadata",
        "created_at",
        "updated_at",
    },
)
# Opens a database and dies inside its conversion, once the state moved and before the columns drop, so the file keeps
# a hot journal.
_DIE_MID_CONVERSION = """
import os, sys
from pathlib import Path
from mindroom.history.types import HistoryScope
from mindroom.session_storage import legacy_session_columns
from mindroom.session_storage.agent_storage import create_state_storage

legacy_session_columns._drop_unkept_session_columns = lambda connection, names: os._exit(0)
create_state_storage(sys.argv[1], Path(sys.argv[2]), HistoryScope(kind=sys.argv[3], scope_id=sys.argv[4]))
"""


def _storage_name(name: str) -> str:
    return storage_name_of((_FIXTURES / f"{name}.sql").read_text(encoding="utf-8"))


def _restore(name: str, root: Path) -> Path:
    """Write fixture ``name`` under ``root`` as v2026.10.667 left it, and return its database file."""
    database = TableNames(_storage_name(name)).database_path(root)
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript((_FIXTURES / f"{name}.sql").read_text(encoding="utf-8"))
    return database


def _open(name: str, root: Path) -> ConversationStore:
    """Open fixture ``name``'s database under ``root`` as its owner's store opens it."""
    return create_state_storage(_storage_name(name), root, _OWNERS[name])


def _dump(database: Path) -> str:
    with closing(sqlite3.connect(database)) as connection:
        return "\n".join(connection.iterdump())


def _query(database: Path, sql: str, *parameters: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql, parameters).fetchall()


def _recorded(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _observed(name: str, root: Path) -> dict[str, Any]:
    with deterministic() as determinism:
        determinism.at(_STEPS[name])
        return observe_database(_open(name, root), _OWNERS[name])


# What every reader reads.


@pytest.mark.parametrize("name", ["agent_code", "system"])
def test_every_reader_reads_a_converted_database_as_the_release_that_wrote_it_read_it(
    name: str,
    tmp_path: Path,
) -> None:
    """Loads, history views, stored runs, metadata, updated sessions, and usage rows are what v2026.10.667 showed."""
    _restore(name, tmp_path)

    observed = _observed(name, tmp_path)

    assert first_difference(_recorded(name), observed) is None, first_difference(_recorded(name), observed)


def test_a_team_database_reads_as_before_but_for_the_conversation_its_agent_path_started(tmp_path: Path) -> None:
    """The team's own conversation reads as v2026.10.667 read it; the one its agent path started now loads for the team.

    That release loaded nothing for the team in it and reported its usage under no team; it now loads with both of its
    runs, replays the team's turn, and reports its usage under the team.
    """
    recorded = _recorded("team_crew")
    _restore("team_crew", tmp_path)

    observed = _observed("team_crew", tmp_path)

    was, now = recorded["sessions"].pop(_AGENT_PATH)["observed"], observed["sessions"].pop(_AGENT_PATH)["observed"]
    assert was["session"] is None
    assert now["session"]["kind"] == "team"
    assert now["session"]["owner_id"] == "crew"
    assert [run["run_id"] for run in now["loaded_runs"]] == ["interrupted", "t2"]
    assert [message["content"] for message in now["request"]][-3:-1] == ["team question t2", "team answer t2"]
    for key in ("list_runs", "ledger", "folded_tool_call_ids", "session_runs", "row"):
        assert now[key] == was[key]
    agent_path_rows = [entry for entry in recorded["usage_rows"] if entry["row"]["row_key"] == _AGENT_PATH]
    assert {(entry["row"]["entity_kind"], entry["row"]["entity_id"]) for entry in agent_path_rows} == {
        ("agent", "crew"),
    }
    for entry in agent_path_rows:
        entry["row"]["entity_kind"] = "team"
    assert first_difference(recorded, observed) is None, first_difference(recorded, observed)


# What the converted file holds.


@pytest.mark.parametrize("name", sorted(_OWNERS))
def test_a_converted_database_records_its_owner_and_keeps_state_in_its_own_column(name: str, tmp_path: Path) -> None:
    """The meta row names the owner at version 4 and keeps its counter; the three owner columns and their index are gone."""
    database = _restore(name, tmp_path)
    names = TableNames.of_database(database)
    [(next_seq,)] = _query(database, f'SELECT next_seq FROM "{names.log_meta}"')  # noqa: S608
    states = dict(_query(database, f'SELECT session_id, session_data FROM "{names.sessions}"'))  # noqa: S608

    _open(name, tmp_path)

    owner = _OWNERS[name]
    assert _query(database, f'SELECT * FROM "{names.log_meta}"') == [  # noqa: S608
        (1, LOG_VERSION, next_seq, owner.kind, owner.scope_id),
    ]
    columns = [column for (column,) in _query(database, "SELECT name FROM pragma_table_info(?)", names.sessions)]
    assert columns == list(SESSION_COLUMNS)
    indexes = {index for (index,) in _query(database, "SELECT name FROM pragma_index_list(?)", names.sessions)}
    assert {index for index in indexes if not index.startswith("sqlite_autoindex")} == set()
    for session_id, state, data in _query(
        database,
        f'SELECT session_id, session_state, session_data FROM "{names.sessions}"',  # noqa: S608
    ):
        old = json.loads(states[session_id])
        assert json.loads(state) == old.pop("session_state")
        # Nothing left is no session data, as a new row stores it.
        assert data == (json.dumps(old) if old else None)


@pytest.mark.parametrize("name", sorted(_OWNERS))
def test_a_converted_database_converts_once(name: str, tmp_path: Path) -> None:
    """Opening a converted database again, or sweeping it, changes nothing, and its backup keeps the original bytes."""
    database = _restore(name, tmp_path)
    original = database.read_bytes()
    _open(name, tmp_path)
    converted = _dump(database)

    _open(name, tmp_path)

    assert _dump(database) == converted
    assert database.with_name(f"{database.name}{BACKUP_SUFFIX}").read_bytes() == original


def test_a_failed_conversion_leaves_the_database_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An error after the state moved rolls the whole conversion back; the next open converts it as one that never failed."""
    database = _restore("agent_code", tmp_path)
    original = _dump(database)

    def fail(*_: object) -> None:
        msg = "the conversion failed"
        raise RuntimeError(msg)

    with monkeypatch.context() as patched:
        patched.setattr(legacy_session_columns, "_drop_unkept_session_columns", fail)
        with pytest.raises(RuntimeError, match="the conversion failed"):
            _open("agent_code", tmp_path)
    assert _dump(database) == original

    expected = tmp_path / "expected"
    _restore("agent_code", expected)
    assert _observed("agent_code", tmp_path) == _observed("agent_code", expected)


def test_a_conversion_killed_midway_is_rolled_back_and_converted_on_the_next_open(tmp_path: Path) -> None:
    """A process that dies inside the conversion leaves a hot journal; the next open rolls it back and converts."""
    database = _restore("team_crew", tmp_path)
    original = _dump(database)

    subprocess.run(
        [sys.executable, "-I", "-c", _DIE_MID_CONVERSION, _storage_name("team_crew"), str(tmp_path), "team", "crew"],
        check=True,
    )

    assert Path(f"{database}-journal").exists()
    with closing(sqlite3.connect(database)) as connection:
        assert "\n".join(connection.iterdump()) == original
    expected = tmp_path / "expected"
    _restore("team_crew", expected)
    assert _observed("team_crew", tmp_path) == _observed("team_crew", expected)


def test_the_version_is_read_again_under_the_write_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An opener that read an older version before another one converted the file leaves it, and its backup, alone."""
    database = _restore("agent_code", tmp_path)
    original = database.read_bytes()
    names = TableNames.of_database(database)
    leave = legacy_session_columns.leave_write_ahead_log
    converted: list[str] = []

    def other_opener_converts_first(connection: sqlite3.Connection, path: Path) -> None:
        monkeypatch.setattr(legacy_session_columns, "leave_write_ahead_log", leave)
        prepare_conversation_database(path, names, _OWNERS["agent_code"])
        converted.append(_dump(database))
        leave(connection, path)

    monkeypatch.setattr(legacy_session_columns, "leave_write_ahead_log", other_opener_converts_first)

    prepare_conversation_database(database, names, _OWNERS["agent_code"])

    assert _dump(database) == converted[0]
    assert database.with_name(f"{database.name}{BACKUP_SUFFIX}").read_bytes() == original


# The startup sweep.


@pytest.mark.asyncio
async def test_the_startup_sweep_converts_agent_team_and_system_databases(tmp_path: Path) -> None:
    """An agent's database records the agent its directory names, a team's the scope its rows name, the system's the ledger."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    root = paths.storage_root
    databases = {
        "agent_code": _restore("agent_code", root / "agents/code"),
        "team_crew": _restore("team_crew", root / "teams" / _storage_name("team_crew")),
        "system": _restore("system", root / SYSTEM_USAGE_STORAGE_NAME),
    }

    await migrate_conversation_storage(paths)

    for name, database in databases.items():
        meta = TableNames.of_database(database).log_meta
        [(version, kind, owner_id)] = _query(database, f'SELECT version, owner_kind, owner_id FROM "{meta}"')  # noqa: S608
        assert (version, HistoryScope(kind=kind, scope_id=owner_id)) == (LOG_VERSION, _OWNERS[name])


@pytest.mark.asyncio
async def test_the_startup_sweep_leaves_a_team_database_no_row_names_to_its_scopes_open(tmp_path: Path) -> None:
    """A team's database whose rows name no scope its name fits keeps its layout until that scope's store opens it."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    team_root = paths.storage_root / "teams" / _storage_name("team_crew")
    database = _restore("team_crew", team_root)
    names = TableNames.of_database(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f'DELETE FROM "{names.sessions}"')  # noqa: S608
    before = _dump(database)

    with capture_logs() as logs:
        await migrate_conversation_storage(paths)

    assert _dump(database) == before
    assert [entry for entry in logs if entry["event"] == "conversation_conversion_skipped_store"] == []
    _open("team_crew", team_root)
    assert _query(database, f'SELECT version, owner_kind, owner_id FROM "{names.log_meta}"') == [  # noqa: S608
        (LOG_VERSION, "team", "crew"),
    ]


@pytest.mark.parametrize("spare", [-1, 0])
def test_converting_needs_twice_the_databases_size_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spare: int,
) -> None:
    """The backup and the conversion's journal need twice the file's size; below it the open fails and changes nothing."""
    database = _restore("agent_code", tmp_path)
    original = _dump(database)
    needed = 2 * database.stat().st_size
    statvfs = os.statvfs
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda target: (
            SimpleNamespace(f_bavail=needed + spare, f_frsize=1) if isinstance(target, int) else statvfs(target)
        ),
    )

    if spare == 0:
        _open("agent_code", tmp_path)
        assert _query(database, 'SELECT version FROM "code_sessions_log_meta"') == [(LOG_VERSION,)]
        return
    with pytest.raises(ConversionSpaceError, match=f"needs {needed} bytes"):
        _open("agent_code", tmp_path)
    assert _dump(database) == original
    assert not database.with_name(f"{database.name}{BACKUP_SUFFIX}").exists()


def test_a_write_ahead_log_database_is_checkpointed_before_its_backup(tmp_path: Path) -> None:
    """A commit only the log holds reaches both the converted file and its backup, and the file leaves the mode."""
    source = _restore("agent_code", tmp_path / "source")
    writer = sqlite3.connect(source, isolation_level=None)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute('UPDATE code_sessions SET metadata = \'{"only": "in the log"}\'')
    database = TableNames.of_database(source).database_path(tmp_path)
    database.parent.mkdir(parents=True)
    for suffix in ("", "-wal"):
        shutil.copyfile(f"{source}{suffix}", f"{database}{suffix}")
    writer.close()

    _open("agent_code", tmp_path)

    assert not Path(f"{database}-wal").exists()
    assert _query(database, "PRAGMA journal_mode") == [("delete",)]
    for copy in (database, database.with_name(f"{database.name}{BACKUP_SUFFIX}")):
        assert {metadata for (metadata,) in _query(copy, "SELECT metadata FROM code_sessions")} == {
            '{"only": "in the log"}',
        }


@pytest.mark.asyncio
async def test_the_startup_sweep_takes_a_team_databases_owner_from_the_rows_its_name_fits(tmp_path: Path) -> None:
    """Rows naming another team, as no release wrote, or only the team bot's agent path, still give the team its own."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    database = _restore("team_crew", paths.storage_root / "teams" / _storage_name("team_crew"))
    names = TableNames.of_database(database)
    sessions = f'"{names.sessions}"'
    with closing(sqlite3.connect(database)) as connection, connection:
        # The other team's row comes first, so a sweep that took the first team its rows name would take it.
        connection.execute(f"CREATE TEMP TABLE kept AS SELECT * FROM {sessions} WHERE session_id = 'agent-path'")  # noqa: S608
        connection.execute(f"DELETE FROM {sessions}")  # noqa: S608
        connection.execute(
            f"INSERT INTO {sessions} (session_id, session_type, team_id, created_at) "  # noqa: S608
            "VALUES ('elsewhere', 'team', 'another-team', 1)",
        )
        connection.execute(f"INSERT INTO {sessions} SELECT * FROM kept")  # noqa: S608

    with capture_logs() as logs:
        await migrate_conversation_storage(paths)

    assert _query(database, f'SELECT owner_kind, owner_id FROM "{names.log_meta}"') == [("team", "crew")]  # noqa: S608
    assert [entry for entry in logs if entry["event"] == "conversation_conversion_skipped_store"] == []


# Going back to v2026.10.667.


@pytest.mark.parametrize("name", sorted(_OWNERS))
def test_an_older_release_archives_a_converted_sessions_directory_and_reads_the_restored_backup(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2026.10.667's preflight moves a converted directory aside with its backup; the backup put back reads as before."""
    database = _restore(name, tmp_path)
    original = database.read_bytes()
    _open(name, tmp_path)
    backup = database.with_name(f"{database.name}{BACKUP_SUFFIX}")
    monkeypatch.setattr(preflight, "_REQUIRED_SESSION_COLUMNS", _OLDER_RELEASE_SESSION_COLUMNS)
    names = TableNames.of_database(database)

    with session_storage_preflight(tmp_path, names, timeout_seconds=1):
        pass

    [archive] = tmp_path.glob("sessions.incompatible-*")
    assert sorted(path.name for path in archive.iterdir()) == sorted([database.name, backup.name])
    assert not database.exists()

    # The downgrade: move the backup back from the archive; the older release then keeps the directory.
    (archive / backup.name).replace(database)
    with session_storage_preflight(tmp_path, names, timeout_seconds=1):
        pass

    assert list(tmp_path.glob("sessions.incompatible-*")) == [archive]
    assert database.read_bytes() == original


# The conversion of any row an older release, or worker code, could have left.

_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3),
    max_leaves=8,
)
_SESSION_DATA = st.one_of(
    st.none(),
    st.text(max_size=12),
    st.builds(json.dumps, _JSON),
    st.builds(
        lambda state, metrics, extra: json.dumps({**extra, **state, **metrics}),
        st.one_of(st.just({}), st.builds(lambda value: {"session_state": value}, _JSON)),
        st.one_of(st.just({}), st.builds(lambda value: {"session_metrics": value}, _JSON)),
        st.dictionaries(st.sampled_from(["session_name", "images", "x"]), _JSON, max_size=2),
    ),
    st.builds(lambda value: json.dumps(json.dumps({"session_state": value})), _JSON),
)


def _state_as_layout_3_loaded_it(raw: object) -> dict[str, Any]:
    """Return the state v2026.10.667's load read from a row's ``session_data``, frozen from its ``_session_state``."""
    state = (decoded_mapping(raw) or {}).get("session_state")
    return state if isinstance(state, dict) else {}


def _usage_as_read(raw: object) -> object:
    """Return what the usage report reads of a row's totals, or the error it reports."""
    try:
        return _decode_session_usage(raw)
    except (RecursionError, TypeError, ValueError) as error:
        return type(error)


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(rows=st.lists(_SESSION_DATA, min_size=1, max_size=6))
def test_every_row_converts_to_what_its_readers_read_before(rows: list[str | None]) -> None:
    """Each reader of a converted row reads its state, its totals, and the rest of ``session_data`` as before."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = _restore("agent_code", root)
        names = TableNames.of_database(database)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(f'DELETE FROM "{names.sessions}"')  # noqa: S608
            connection.executemany(
                f'INSERT INTO "{names.sessions}" (session_id, session_type, agent_id, user_id, session_data, '  # noqa: S608
                "metadata, created_at, updated_at) VALUES (?, 'agent', 'code', ?, ?, ?, ?, ?)",
                [
                    (f"s{index}", f"@u{index}:x", raw, json.dumps({"n": index}), index, 2 * index)
                    for index, raw in enumerate(rows)
                ],
            )

        # The JSON columns' numeric affinity stores text that reads as a number as that number, as SQLite always did.
        stored = dict(_query(database, f'SELECT session_id, session_data FROM "{names.sessions}"'))  # noqa: S608
        store = _open("agent_code", root)
        converted = {
            session_id: (data, rest)
            for session_id, data, *rest in _query(
                database,
                "SELECT session_id, session_data, user_id, metadata, created_at, updated_at "  # noqa: S608
                f'FROM "{names.sessions}"',
            )
        }
        loaded = {f"s{index}": store.load_session(f"s{index}", "agent") for index in range(len(rows))}
        again = _dump(database)
        _open("agent_code", root)
        assert _dump(database) == again

    for index in range(len(rows)):
        raw = stored[f"s{index}"]
        data, rest = converted[f"s{index}"]
        session = loaded[f"s{index}"]
        assert session is not None
        assert session.session_state == _state_as_layout_3_loaded_it(raw)
        assert _usage_as_read(data) == _usage_as_read(raw)
        held = decoded_mapping(raw, null={})
        if held is None:
            assert data == raw
        else:
            held.pop("session_state", None)
            assert (decoded_mapping(data, null={}) or {}) == held
        assert rest == [f"@u{index}:x", json.dumps({"n": index}), index, 2 * index]
