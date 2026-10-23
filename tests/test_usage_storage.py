"""Usage persists independently of conversation replay and in the same write transaction."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import MessageMetrics, ModelMetrics, RunMetrics
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.schema import TableNames
from mindroom.session_storage.store import ConversationStore
from mindroom.session_storage.types import ConversationSession
from mindroom.usage.stats_storage import UsageSessionRow, UsageStorageSource, iter_usage_storage_rows
from mindroom.usage.storage import COMPACTED_REQUESTS_METADATA_KEY, project_requests, project_usage
from tests.history_helpers import compact_runs

if TYPE_CHECKING:
    from mindroom.session_storage.store import ConversationStore


def _session() -> ConversationSession:
    """Return the conversation's row; each save gets its own, since a save updates the session it is given."""
    return ConversationSession(kind="agent", session_id="session", owner_id="code")


@pytest.fixture
def usage_db(tmp_path: Path) -> ConversationStore:
    """Use the production storage owner with a real session row."""
    storage = create_state_storage("code", tmp_path)
    storage.ensure_session_row(_session())
    return storage


def _save(storage: ConversationStore, run: RunRecord) -> None:
    storage.save_run(_session(), run, record_usage=True)


def _run(tokens: int = 10, *, run_id: str = "run-1", parent_run_id: str | None = None) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        session_id="session",
        agent_id="code",
        parent_run_id=parent_run_id,
        user_id="@alice:example.test",
        model_provider="test-provider",
        model="test-model",
        created_at=1_700_000_000,
        content="private response content",
        messages=[Message(role="user", content="private request content")],
        metadata={"requester_id": "@alice:example.test", "extra": "private metadata content"},
        metrics=RunMetrics(
            input_tokens=tokens - 3,
            output_tokens=3,
            total_tokens=tokens,
            cache_read_tokens=2,
            cache_write_tokens=4,
            details={
                "model": [
                    ModelMetrics(
                        id="test-model",
                        provider="test-provider",
                        input_tokens=tokens - 3,
                        output_tokens=3,
                        total_tokens=tokens,
                        cache_read_tokens=2,
                        cache_write_tokens=4,
                    ),
                ],
            },
        ),
    )


def _rows(storage: ConversationStore) -> list[tuple[str | None, object]]:
    with sqlite3.connect(storage.path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'code_sessions_usage'",
        ).fetchone(), "Usage storage must be initialized by the run writer"
        return [
            (run_id, json.loads(payload) if payload is not None else None)
            for run_id, payload in connection.execute(
                "SELECT run_id, usage_data FROM code_sessions_usage ORDER BY id",
            )
        ]


def test_run_save_keeps_only_usage_fields(usage_db: ConversationStore) -> None:
    """Copying the full run or dropping provider counters breaks the durable usage contract."""
    _save(usage_db, _run())

    rows = _rows(usage_db)
    assert len(rows) == 1
    run_id, payload = rows[0]
    assert run_id == "run-1"
    assert isinstance(payload, dict)
    assert payload["created_at"] == 1_700_000_000
    assert payload["metadata"] == {"requester_id": "@alice:example.test"}
    assert payload["metrics"]["total_tokens"] == 10
    assert payload["metrics"]["cache_read_tokens"] == 2
    assert payload["metrics"]["cache_write_tokens"] == 4
    assert payload["metrics"]["details"]["model"][0]["provider"] == "test-provider"
    assert "private" not in json.dumps(payload)
    assert "messages" not in payload
    assert "content" not in payload


def test_new_empty_session_has_complete_empty_usage(usage_db: ConversationStore) -> None:
    """An empty current session must not look like an unmigrated source."""
    assert _rows(usage_db) == []


def test_concurrent_saves_keep_run_and_usage_snapshots_consistent(usage_db: ConversationStore) -> None:
    """Competing writes may finish in either order, but both representations must agree."""
    _save(usage_db, _run())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_save, usage_db, _run(tokens)) for tokens in (20, 30)]
        for future in futures:
            future.result()
    session = usage_db.load_session("session", "agent")
    assert session is not None
    assert session.runs[0].metrics.total_tokens in (20, 30)
    assert _rows(usage_db)[0][1]["metrics"]["total_tokens"] == session.runs[0].metrics.total_tokens


def test_repeated_saves_replace_the_usage_snapshot(usage_db: ConversationStore) -> None:
    """Checkpoints and continuation updates must not add the same snapshot twice."""
    for tokens in (10, 10, 30):
        _save(usage_db, _run(tokens))

    rows = _rows(usage_db)
    assert len(rows) == 1
    assert rows[0][1]["metrics"]["total_tokens"] == 30


def test_a_refused_save_of_a_removed_run_still_records_usage(usage_db: ConversationStore) -> None:
    """The provider calls behind a late save of a cut run were made, so their usage stays recorded."""
    with sqlite3.connect(usage_db.path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS code_sessions_removed_runs (run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL)",
        )
        connection.execute("INSERT INTO code_sessions_removed_runs (run_id, session_id) VALUES ('run-1', 'session')")

    _save(usage_db, _run())

    assert [(run_id, payload["metrics"]["total_tokens"]) for run_id, payload in _rows(usage_db)] == [("run-1", 10)]
    with sqlite3.connect(usage_db.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM code_sessions_runs").fetchone() == (0,)


def test_run_cleanup_preserves_usage_before_regeneration(usage_db: ConversationStore) -> None:
    """Replay cleanup cannot erase incurred usage, and the replacement run adds its own work."""
    _save(usage_db, _run())
    usage_db.delete_run_subtrees(["run-1"])
    _save(usage_db, _run(20, run_id="run-2"))

    assert [(run_id, payload["metrics"]["total_tokens"]) for run_id, payload in _rows(usage_db)] == [
        ("run-1", 10),
        ("run-2", 20),
    ]
    session = usage_db.load_session("session", "agent")
    assert session is not None
    assert [run.run_id for run in session.runs] == ["run-2"]


def test_usage_write_failure_rolls_back_the_run(usage_db: ConversationStore) -> None:
    """A failed usage write cannot leave the conversation update committed alone."""
    _save(usage_db, _run())
    _rows(usage_db)
    with sqlite3.connect(usage_db.path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_usage_update BEFORE UPDATE ON code_sessions_usage "
            "BEGIN SELECT RAISE(ABORT, 'usage write failed'); END",
        )

    with pytest.raises(sqlite3.IntegrityError, match="usage write failed"):
        _save(usage_db, _run(30))

    session = usage_db.load_session("session", "agent")
    assert session is not None
    assert session.runs[0].metrics.total_tokens == 10
    assert _rows(usage_db)[0][1]["metrics"]["total_tokens"] == 10


def test_session_erasure_removes_usage(usage_db: ConversationStore) -> None:
    """The durable usage table must respect explicit whole-session erasure."""
    _save(usage_db, _run())
    assert _rows(usage_db)

    with usage_db.transaction(write=True) as connection:
        connection.execute("DELETE FROM code_sessions WHERE session_id = 'session'")

    assert _rows(usage_db) == []


def test_nested_run_usage_retains_parent_identity(usage_db: ConversationStore) -> None:
    """Losing parent identity would let reporting count nested usage twice."""
    _save(usage_db, _run())
    _save(usage_db, _run(5, run_id="member", parent_run_id="run-1"))
    usage_db.delete_run_subtrees(["run-1"])

    rows = _rows(usage_db)
    assert len(rows) == 2
    assert rows[1][1]["parent_run_id"] == "run-1"


def test_reporting_keeps_usage_after_real_compaction_and_reopen(usage_db: ConversationStore) -> None:
    """Compaction may delete every replay run without changing usage attribution or dates."""
    _save(usage_db, _run())
    source = UsageStorageSource(
        path=Path(usage_db.path),
        path_label="code.db",
        scope="shared_agent",
        names=TableNames("code"),
        source_agent_id="code",
        allowed_agent_ids=frozenset({"code"}),
        requester_isolated=False,
    )
    before = list(iter_usage_storage_rows(source))
    assert isinstance(before[0], UsageSessionRow)
    assert before[0].runs[0].metrics["total_tokens"] == 10
    session = usage_db.load_session("session", "agent")
    assert session is not None
    compact_runs(usage_db, session, HistoryScope(kind="agent", scope_id="code"), ["run-1"], "summary")
    assert session.runs == []
    assert list(iter_usage_storage_rows(source)) == before


def test_a_request_names_the_model_that_answered_it_and_a_retried_request_s_sum_is_no_request() -> None:
    """A request's stored model comes from its message; a message summing retried attempts describes no one request."""
    answered = {"mindroom_model": {"id": "reply-model", "provider": "test-provider"}}
    single = Message(
        role="assistant",
        metrics=MessageMetrics(input_tokens=100, output_tokens=1),
        provider_data=answered,
    )
    retried = Message(
        role="assistant",
        metrics=MessageMetrics(input_tokens=300, output_tokens=3),
        provider_data={**answered, "mindroom_aggregate_usage": True},
    )

    requests = project_requests([single.to_dict(), retried.to_dict()]) or []

    assert [(request["model"], request["model_provider"], request["metrics"]) for request in requests] == [
        ("reply-model", "test-provider", {"input_tokens": 100, "output_tokens": 1}),
    ]


def test_carried_request_usage_is_not_counted_again_while_its_message_is_saved() -> None:
    """A failed reply can keep the messages a compaction folded; their carried usage must not count twice."""
    folded = Message(role="assistant", content="step", metrics=MessageMetrics(input_tokens=100, output_tokens=1))
    later = Message(role="assistant", content="done", metrics=MessageMetrics(input_tokens=50, output_tokens=1))
    carried = [{**request, "message_id": folded.id} for request in project_requests([folded.to_dict()]) or []]
    run = RunRecord(
        run_id="run",
        status=RunStatus.error,
        messages=[folded, later],
        metadata={COMPACTED_REQUESTS_METADATA_KEY: carried},
    )

    requests = project_usage(run.to_dict())["requests"]

    assert [request["metrics"]["input_tokens"] for request in requests] == [100, 50]
    assert all("message_id" not in request for request in requests)
