"""Opening a conversation database through the factories: migrations first, then the owned store, tables on first write."""

from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord
from mindroom.session_storage import agent_storage
from mindroom.session_storage.agent_storage import create_session_storage, create_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import build_requester_execution_identity
from tests.conftest import bind_runtime_paths, create_agno_2_sessions_db, runtime_paths_for, seed_session
from tests.history_helpers import stored_run_ids

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.session_storage.store import ConversationStore


def _open(tmp_path: Path) -> ConversationStore:
    return create_state_storage("code", tmp_path)


def _tables(db_file: Path) -> set[str]:
    with closing(sqlite3.connect(db_file)) as connection:
        return {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _run(run_id: str) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id="code",
        session_id="session-1",
        messages=[Message(role="system", content="prompt"), Message(role="user", content=run_id)],
    )


def test_migrations_run_in_order_under_the_preflight_lock_before_the_store_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Archive check, usage ledger, summary rows, then the store, all while the recovery lock is held."""
    order: list[str] = []

    @contextmanager
    def preflight(*_args: object, **_kwargs: object) -> Iterator[None]:
        order.append("preflight")
        yield
        order.append("preflight released")

    real_open = agent_storage.open_conversation_store

    def open_store(*args: object, **kwargs: object) -> ConversationStore:
        order.append("open")
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(agent_storage, "session_storage_preflight", preflight)
    monkeypatch.setattr(agent_storage, "migrate_usage_database", lambda *_a, **_k: order.append("usage"))
    monkeypatch.setattr(agent_storage, "migrate_to_summary_rows", lambda *_a, **_k: order.append("summary rows"))
    monkeypatch.setattr(agent_storage, "open_conversation_store", open_store)

    _open(tmp_path)

    assert order == ["preflight", "usage", "summary rows", "open", "preflight released"]


def test_a_fresh_database_gets_its_tables_from_the_first_write_only(tmp_path: Path) -> None:
    """Opening creates the file and nothing in it; the first run save creates every table at once."""
    store = _open(tmp_path)

    assert store.path == tmp_path / "sessions" / "code.db"
    assert _tables(store.path) == set()

    store.save_run(
        ConversationSession(kind="agent", session_id="session-1", owner_id="code"),
        _run("r1"),
        record_usage=True,
    )

    assert _tables(store.path) >= {
        "code_sessions",
        "code_sessions_runs",
        "code_sessions_usage",
        "code_sessions_summaries",
    }


def test_a_storage_name_that_leaves_the_sessions_directory_opens_nothing(tmp_path: Path) -> None:
    """The opener refuses a name that is not a file directly inside the sessions directory, and creates nothing."""
    with pytest.raises(ValueError, match="must identify a file directly inside its sessions directory"):
        create_state_storage("../outside", tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_stored_runs_leave_out_the_prompt_roles_of_the_history_settings(tmp_path: Path) -> None:
    """Every factory-opened store applies the one prompt-role policy, so a run's system message is not stored."""
    store = _open(tmp_path)
    store.save_run(
        ConversationSession(kind="agent", session_id="session-1", owner_id="code"),
        _run("r1"),
        record_usage=True,
    )

    loaded = store.load_session("session-1", "agent")

    assert loaded is not None
    assert [message.role for message in loaded.runs[0].messages] == ["user"]


def test_an_agno_2_database_is_migrated_once_and_then_keeps_its_log_order(tmp_path: Path) -> None:
    """A real 2.6.12 database gets its usage ledger and summary rows, its blob runs become rows, and later saves append."""
    db_file = create_agno_2_sessions_db(tmp_path / "sessions" / "code.db")

    store = _open(tmp_path)

    assert {"code_sessions_usage", "code_sessions_summaries"} <= _tables(db_file)
    with closing(sqlite3.connect(db_file)) as connection:
        assert [run_id for (run_id,) in connection.execute("SELECT run_id FROM code_sessions_usage ORDER BY id")] == [
            "run-1",
            "run-2",
            "run-3",
        ]
        assert connection.execute("SELECT runs FROM code_sessions WHERE session_id = 'session-1'").fetchone() == (None,)
        # Opening a pre-existing file must keep its rollback journal.
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    loaded = store.load_session("session-1", "agent")
    assert loaded is not None
    assert [run.run_id for run in loaded.runs] == ["run-1", "run-2", "run-3"]

    store.delete_run_subtrees(["run-2"])
    store.save_runs(loaded, [_run("run-4")])
    reopened = _open(tmp_path).load_session("session-1", "agent")

    assert reopened is not None
    assert [run.run_id for run in reopened.runs] == ["run-1", "run-3", "run-4"]
    assert stored_run_ids(_open(tmp_path), "session-1") == ["run-1", "run-3", "run-4"]


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

    assert for_alice.load_session("session-alice", "agent") is not None
    assert for_bob.path != for_alice.path
    assert for_bob.load_session("session-alice", "agent") is None
