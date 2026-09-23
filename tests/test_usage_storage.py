"""Usage persists independently of conversation replay and in the same write transaction."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.metrics import MessageMetrics, ModelMetrics, RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.tools.function import Function
from sqlalchemy.exc import IntegrityError

from mindroom.agent_storage import create_state_storage
from mindroom.history.storage import record_compaction_chunk
from mindroom.history.types import HistoryScope
from mindroom.synthetic_model import SyntheticModel
from mindroom.usage_stats_storage import UsageSessionRow, UsageStorageSource, iter_usage_storage_rows

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.models.response import ModelResponse


@pytest.fixture
def usage_db(tmp_path: Path) -> Iterator[SqliteDb]:
    """Use the production storage owner with a real session row."""
    storage = create_state_storage("code", tmp_path, subdir="sessions", session_table="code_sessions")
    assert isinstance(storage, SqliteDb)
    storage.upsert_session(AgentSession(session_id="session", agent_id="code", user_id="@alice:example.test"))
    try:
        yield storage
    finally:
        storage.close()


def _run(tokens: int = 10, *, run_id: str = "run-1", parent_run_id: str | None = None) -> RunOutput:
    return RunOutput(
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


def _rows(storage: SqliteDb) -> list[tuple[str | None, object]]:
    with sqlite3.connect(storage.db_file) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'code_sessions_usage'",
        ).fetchone(), "Usage storage must be initialized by the run writer"
        return [
            (run_id, json.loads(payload) if payload is not None else None)
            for run_id, payload in connection.execute(
                "SELECT run_id, usage_data FROM code_sessions_usage ORDER BY id",
            )
        ]


def test_run_save_keeps_only_usage_fields(usage_db: SqliteDb) -> None:
    """Copying the full run or dropping provider counters breaks the durable usage contract."""
    usage_db.upsert_run(_run(), session_id="session")

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


def test_new_empty_session_has_complete_empty_usage(usage_db: SqliteDb) -> None:
    """An empty current session must not look like an unmigrated source."""
    assert _rows(usage_db) == []


class _MeteredModel(SyntheticModel):
    def invoke(self, messages: list[Message], **kwargs: Any) -> ModelResponse:  # noqa: ANN401
        response = super().invoke(messages, **kwargs)
        response.response_usage = MessageMetrics(input_tokens=7, output_tokens=3, total_tokens=10, cache_read_tokens=2)
        return response


def test_real_agno_continuation_replaces_the_accumulated_snapshot(usage_db: SqliteDb) -> None:
    """Use Agno's actual pause/resume metric accumulation, not a hand-built continuation payload."""

    def run_shell_command(args: list[str]) -> str:
        return str(args)

    actor = Agent(
        id="code",
        db=usage_db,
        telemetry=False,
        model=_MeteredModel(id="test-model", chars_per_second=0, tool_call_probability=1),
        tools=[Function(name="run_shell_command", entrypoint=run_shell_command, requires_confirmation=True)],
    )
    paused = actor.run("Run the tool", session_id="session", user_id="@alice:example.test")
    assert paused.status == RunStatus.paused
    assert _rows(usage_db)[0][1]["metrics"]["total_tokens"] == 10
    saved = usage_db.get_session("session", session_type=SessionType.AGENT)
    assert isinstance(saved, AgentSession)
    assert saved.session_data["session_metrics"]["total_tokens"] == 10
    assert paused.requirements
    paused.requirements[0].confirm()

    resumed = actor.continue_run(paused, requirements=paused.requirements, session_id="session")

    assert resumed.status == RunStatus.completed
    assert resumed.run_id == paused.run_id
    rows = _rows(usage_db)
    assert len(rows) == 1
    assert rows[0][1]["metrics"]["total_tokens"] == 20
    assert rows[0][1]["metrics"]["cache_read_tokens"] == 4
    saved = usage_db.get_session("session", session_type=SessionType.AGENT)
    assert isinstance(saved, AgentSession)
    assert saved.session_data["session_metrics"]["total_tokens"] == 20
    assert saved.session_data["session_metrics"]["cache_read_tokens"] == 4


def test_concurrent_saves_keep_run_and_usage_snapshots_consistent(usage_db: SqliteDb) -> None:
    """Competing writes may finish in either order, but both representations must agree."""
    usage_db.upsert_run(_run(), session_id="session")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(usage_db.upsert_run, _run(tokens), "session") for tokens in (20, 30)]
        for future in futures:
            future.result()
    session = usage_db.get_session("session", session_type=SessionType.AGENT)
    assert isinstance(session, AgentSession)
    assert session.runs[0].metrics.total_tokens in (20, 30)
    assert _rows(usage_db)[0][1]["metrics"]["total_tokens"] == session.runs[0].metrics.total_tokens


def test_repeated_saves_replace_the_usage_snapshot(usage_db: SqliteDb) -> None:
    """Checkpoints and continuation updates must not add the same snapshot twice."""
    for tokens in (10, 10, 30):
        usage_db.upsert_run(_run(tokens), session_id="session")

    rows = _rows(usage_db)
    assert len(rows) == 1
    assert rows[0][1]["metrics"]["total_tokens"] == 30


def test_run_cleanup_preserves_usage_before_regeneration(usage_db: SqliteDb) -> None:
    """Replay cleanup cannot erase incurred usage, and the replacement run adds its own work."""
    usage_db.upsert_run(_run(), session_id="session")
    usage_db.delete_runs(["run-1"])
    usage_db.upsert_run(_run(20, run_id="run-2"), session_id="session")

    assert [(run_id, payload["metrics"]["total_tokens"]) for run_id, payload in _rows(usage_db)] == [
        ("run-1", 10),
        ("run-2", 20),
    ]
    session = usage_db.get_session("session", session_type=SessionType.AGENT)
    assert session is not None
    assert [run.run_id for run in session.runs] == ["run-2"]


def test_usage_write_failure_rolls_back_the_run(usage_db: SqliteDb) -> None:
    """A failed usage write cannot leave the conversation update committed alone."""
    usage_db.upsert_run(_run(), session_id="session")
    _rows(usage_db)
    with sqlite3.connect(usage_db.db_file) as connection:
        connection.execute(
            "CREATE TRIGGER fail_usage_update BEFORE UPDATE ON code_sessions_usage "
            "BEGIN SELECT RAISE(ABORT, 'usage write failed'); END",
        )

    with pytest.raises(IntegrityError, match="usage write failed"):
        usage_db.upsert_run(_run(30), session_id="session")

    session = usage_db.get_session("session", session_type=SessionType.AGENT)
    assert session is not None
    assert session.runs[0].metrics.total_tokens == 10
    assert _rows(usage_db)[0][1]["metrics"]["total_tokens"] == 10


def test_session_erasure_removes_usage(usage_db: SqliteDb) -> None:
    """The durable usage table must respect explicit whole-session erasure."""
    usage_db.upsert_run(_run(), session_id="session")
    assert _rows(usage_db)

    assert usage_db.delete_session("session")

    assert _rows(usage_db) == []


def test_nested_run_usage_retains_parent_identity(usage_db: SqliteDb) -> None:
    """Losing parent identity would let reporting count nested usage twice."""
    usage_db.upsert_run(_run(), session_id="session")
    usage_db.upsert_run(_run(5, run_id="member", parent_run_id="run-1"), session_id="session")
    usage_db.delete_runs(["run-1"])

    rows = _rows(usage_db)
    assert len(rows) == 2
    assert rows[1][1]["parent_run_id"] == "run-1"


def test_reporting_keeps_usage_after_real_compaction_and_reopen(usage_db: SqliteDb) -> None:
    """Compaction may delete every replay run without changing usage attribution or dates."""
    usage_db.upsert_run(_run(), session_id="session")
    source = UsageStorageSource(
        path=Path(usage_db.db_file),
        path_label="code.db",
        scope="shared_agent",
        expected_session_table="code_sessions",
        source_agent_id="code",
        allowed_agent_ids=frozenset({"code"}),
        requester_isolated=False,
    )
    before = list(iter_usage_storage_rows(source))
    assert isinstance(before[0], UsageSessionRow)
    assert before[0].runs[0].metrics["total_tokens"] == 10
    session = usage_db.get_session("session", session_type=SessionType.AGENT)
    assert isinstance(session, AgentSession)
    record_compaction_chunk(
        storage=usage_db,
        persisted_session=session,
        working_session=deepcopy(session),
        scope=HistoryScope(kind="agent", scope_id="code"),
        compacted_run_ids=["run-1"],
    )
    usage_db.close()
    assert list(iter_usage_storage_rows(source)) == before
