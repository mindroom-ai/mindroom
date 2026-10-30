"""Opening a conversation database through the factories: the preflight, the link checks, the conversion, then the store."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord
from mindroom.session_storage import agent_storage
from mindroom.session_storage import store as store_module
from mindroom.session_storage.agent_storage import (
    create_agent_state_storage,
    create_session_storage,
    prepare_conversation_database,
)
from mindroom.session_storage.legacy_agno3_conversion import BACKUP_SUFFIX
from mindroom.session_storage.schema import TableNames, table_names
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import build_requester_execution_identity
from tests.conftest import bind_runtime_paths, create_agno_2_sessions_db, delete_runs, runtime_paths_for, seed_session
from tests.history_helpers import stored_run_ids

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.session_storage.store import ConversationStore


_NAMES = TableNames("code")
_OWNER = HistoryScope(kind="agent", scope_id="code")


def _open(tmp_path: Path) -> ConversationStore:
    return create_agent_state_storage("code", tmp_path)


def _tables(db_file: Path) -> set[str]:
    with closing(sqlite3.connect(db_file)) as connection:
        return table_names(connection)


def _run(run_id: str) -> RunRecord:
    return RunRecord(run_id=run_id, agent_id="code", session_id="session-1")


def test_the_conversion_runs_under_the_preflight_lock_before_the_store_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive check, conversion, then the store, all while the recovery lock is held."""
    order: list[str] = []

    @contextmanager
    def preflight(*_args: object, **_kwargs: object) -> Iterator[None]:
        order.append("preflight")
        yield
        order.append("preflight released")

    real_store = agent_storage.ConversationStore

    def open_store(*args: object, **kwargs: object) -> ConversationStore:
        order.append("open")
        return real_store(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_storage, "session_storage_preflight", preflight)
    monkeypatch.setattr(agent_storage, "prepare_conversation_database", lambda *_a, **_k: order.append("conversion"))
    monkeypatch.setattr(agent_storage, "ConversationStore", open_store)

    _open(tmp_path)

    assert order == ["preflight", "conversion", "open", "preflight released"]


def test_a_new_database_gets_every_table_when_it_opens(tmp_path: Path) -> None:
    """Opening creates the file with every storage v2 table and no backup, so no write creates a table."""
    store = _open(tmp_path)

    names = TableNames("code")
    assert store.path == tmp_path / "sessions" / "code.db"
    assert _tables(store.path) == {
        names.sessions,
        names.usage,
        names.log_meta,
        names.log_runs,
        names.log_messages,
        names.log_tools,
        names.log_summaries,
        names.log_removed,
        "sqlite_sequence",
    }
    assert sorted(path.name for path in store.path.parent.iterdir()) == ["code.db"]


def test_a_storage_name_that_leaves_the_sessions_directory_opens_nothing(tmp_path: Path) -> None:
    """The opener refuses a name that is not a file directly inside the sessions directory, and creates nothing."""
    with pytest.raises(ValueError, match="must identify a file directly inside its sessions directory"):
        create_agent_state_storage("../outside", tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_an_agno_2_database_is_migrated_once_and_then_keeps_its_log_order(tmp_path: Path) -> None:
    """A real 2.6.12 database converts once, its usage ledger seeded and its blob runs rows, and later saves append."""
    db_file = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")
    original = db_file.read_bytes()

    store = _open(tmp_path)

    assert {"code_sessions_usage", "code_sessions_log_runs", "code_sessions_log_summaries"} <= _tables(db_file)
    with closing(sqlite3.connect(db_file)) as connection:
        assert [run_id for (run_id,) in connection.execute("SELECT run_id FROM code_sessions_usage ORDER BY id")] == [
            "run-1",
            "run-2",
            "run-3",
        ]
        assert "runs" not in {row[1] for row in connection.execute("PRAGMA table_info(code_sessions)")}
        # Opening a pre-existing file must keep its rollback journal.
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    loaded = store.load_session("session-1")
    assert loaded is not None
    assert [run.run_id for run in loaded.runs] == ["run-1", "run-2", "run-3"]

    delete_runs(store, ["run-2"], session_id="session-1")
    store.save_run(loaded, _run("run-4"), record_usage=False)
    reopened = _open(tmp_path).load_session("session-1")

    assert reopened is not None
    assert [run.run_id for run in reopened.runs] == ["run-1", "run-3", "run-4"]
    assert stored_run_ids(_open(tmp_path), "session-1") == ["run-1", "run-3", "run-4"]
    # No reopen archived the converted file, and its backup keeps the original.
    assert not list(tmp_path.glob("sessions.incompatible-*"))
    assert db_file.with_name(f"code.db{BACKUP_SUFFIX}").read_bytes() == original


def test_a_process_prepares_each_database_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Later opens of a database the process prepared take neither the recovery lock nor the open-time checks."""
    first = _open(tmp_path)

    def refused(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a later open prepared the database again")

    monkeypatch.setattr(agent_storage, "session_storage_preflight", refused)
    monkeypatch.setattr(agent_storage, "prepare_conversation_database", refused)

    assert _open(tmp_path) == first
    assert _open(tmp_path).load_session("session-1") is None


def test_an_open_after_the_database_was_deleted_creates_it_again(tmp_path: Path) -> None:
    """A prepared database deleted and not yet recreated gets every table again at its next open."""
    store = _open(tmp_path)
    seed_session(store, ConversationSession(kind="agent", session_id="session-1", owner_id="code"))
    store.path.unlink()

    reopened = _open(tmp_path)

    assert reopened.load_session("session-1") is None
    assert _NAMES.log_runs in _tables(reopened.path)


def test_a_database_moved_over_a_prepared_one_is_prepared_at_the_next_open(tmp_path: Path) -> None:
    """Another file at a prepared database's path, here an Agno 2 one, converts at the next open."""
    _open(tmp_path)
    moved = create_agno_2_sessions_db(tmp_path / "elsewhere" / "code.db")
    moved.replace(_NAMES.database_path(tmp_path))

    loaded = _open(tmp_path).load_session("session-1")

    assert loaded is not None
    assert [run.run_id for run in loaded.runs] == ["run-1", "run-2", "run-3"]


@pytest.mark.parametrize("planted", ["database link", "directory link", "file for the directory"])
def test_a_link_or_file_swapped_in_after_a_prepared_open_is_refused(tmp_path: Path, planted: str) -> None:
    """A link at a prepared database or its sessions directory, even to that same file, fails the next open as the first.

    So does a file in the sessions directory's place.
    """
    database = _open(tmp_path).path
    if planted == "database link":
        database.rename(tmp_path / "moved.db")
        database.symlink_to(tmp_path / "moved.db")
    else:
        database.parent.rename(tmp_path / "moved")
        if planted == "directory link":
            database.parent.symlink_to(tmp_path / "moved", target_is_directory=True)
        else:
            database.parent.write_text("not a directory")

    with pytest.raises(ValueError, match="owned"):
        _open(tmp_path)


def test_a_private_agents_store_is_the_requesters_own(tmp_path: Path) -> None:
    """A per-user private agent's session store is scoped to the requester, so another requester never sees its sessions."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    agent = AgentConfig(display_name="Mind", private=AgentPrivateConfig(per="user", root="mind_data"))
    config = bind_runtime_paths(Config(agents={"mind": agent}), runtime_paths)
    runtime_paths = runtime_paths_for(config)
    alice, bob = (
        build_requester_execution_identity(agent_name="mind", runtime_paths=runtime_paths, requester_id=requester)
        for requester in ("@alice:example.org", "@bob:example.org")
    )
    seed_session(
        create_session_storage("mind", config, runtime_paths, execution_identity=alice),
        ConversationSession(kind="agent", session_id="session-alice", owner_id="mind"),
    )

    for_alice = create_session_storage("mind", config, runtime_paths, execution_identity=alice)
    for_bob = create_session_storage("mind", config, runtime_paths, execution_identity=bob)

    assert for_alice.load_session("session-alice") is not None
    assert for_bob.path != for_alice.path
    assert for_bob.load_session("session-alice") is None


def test_a_current_database_costs_reading_its_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening a database in the current layout reads only its marker and version, beside checking SQLite's file."""
    database, names = _open(tmp_path).path, _NAMES
    statements: list[str] = []
    connect = store_module.connect_conversation_database

    def traced(path: Path, *, timeout: float) -> sqlite3.Connection:
        connection = connect(path, timeout=timeout)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store_module, "connect_conversation_database", traced)
    prepare_conversation_database(database, names, _OWNER)

    # SQLite traces the pragma a table-valued function runs as a comment of its own.
    run = [statement for statement in statements if not statement.startswith("--")]
    assert len(run) == 3
    assert "pragma_database_list" in run[0]
    assert run[1].startswith("SELECT 1 FROM sqlite_master")
    assert run[2].startswith("SELECT version FROM")


@pytest.mark.parametrize("link", ["database", "dangling", "directory"])
def test_a_link_at_the_database_or_its_directory_is_refused(tmp_path: Path, link: str) -> None:
    """An open never opens a link's target, nor creates a dangling one's."""
    target, names = _open(tmp_path / "target").path, _NAMES
    before = target.read_bytes()
    database = names.database_path(tmp_path / "store")
    database.parent.parent.mkdir(parents=True)
    if link == "directory":
        database.parent.symlink_to(target.parent, target_is_directory=True)
    else:
        database.parent.mkdir()
        database.symlink_to(target if link == "database" else tmp_path / "created.db")

    with pytest.raises(ValueError, match="link"):
        prepare_conversation_database(database, names, _OWNER)

    assert target.read_bytes() == before
    assert not (tmp_path / "created.db").exists()
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]


def test_a_link_planted_after_the_check_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link swapped in between the check and the connect is caught by the file SQLite reports it opened."""
    target, names = _open(tmp_path / "target").path, _NAMES
    before = target.read_bytes()
    database = names.database_path(tmp_path / "store")
    database.parent.mkdir(parents=True)
    connect = store_module.connect_conversation_database

    def swapped(path: Path, *, timeout: float) -> sqlite3.Connection:
        path.symlink_to(target)
        return connect(path, timeout=timeout)

    monkeypatch.setattr(store_module, "connect_conversation_database", swapped)
    with pytest.raises(ValueError, match="instead"):
        prepare_conversation_database(database, names, _OWNER)

    assert target.read_bytes() == before


def test_a_sessions_directory_swapped_for_a_link_after_the_check_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link swapped in for the sessions directory between the check and the connect gets no v2 tables written."""
    outside = tmp_path / "outside"
    outside.mkdir()
    database = _NAMES.database_path(tmp_path / "store")
    database.parent.mkdir(parents=True)
    connect = store_module.connect_conversation_database

    def swapped(path: Path, *, timeout: float) -> sqlite3.Connection:
        path.parent.rename(tmp_path / "moved")
        path.parent.symlink_to(outside, target_is_directory=True)
        return connect(path, timeout=timeout)

    monkeypatch.setattr(store_module, "connect_conversation_database", swapped)
    with pytest.raises(ValueError, match="instead"):
        prepare_conversation_database(database, _NAMES, _OWNER)

    # SQLite's open may have left an empty file at the link's target, never a database with tables.
    planted = outside / database.name
    assert not planted.exists() or planted.stat().st_size == 0


def test_a_sessions_directory_swapped_for_a_link_before_the_resolve_is_refused(tmp_path: Path) -> None:
    """A link swapped in for the sessions directory while the open resolves its expected path is refused too."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sessions = _NAMES.database_path(tmp_path / "store").parent
    sessions.mkdir(parents=True)

    class SwappedOnFirstResolve(Path):
        def resolve(self, strict: bool = False) -> Path:
            if not sessions.is_symlink():
                sessions.rename(tmp_path / "moved")
                sessions.symlink_to(outside, target_is_directory=True)
            return super().resolve(strict=strict)

    database = SwappedOnFirstResolve(_NAMES.database_path(tmp_path / "store"))
    with pytest.raises(ValueError, match="instead"):
        prepare_conversation_database(database, _NAMES, _OWNER)

    assert sessions.is_symlink()
    planted = outside / database.name
    assert not planted.exists() or planted.stat().st_size == 0
