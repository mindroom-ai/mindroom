"""Internal paid calls appear in admin usage without becoming conversations."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING

import pytest
from agno.metrics import MessageMetrics, RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.session.agent import AgentSession

from mindroom import helper_usage
from mindroom.agent_storage import create_session_storage
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.helper_usage import record_system_usage
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.usage_stats import collect_admin_usage, collect_private_usage, collect_self_usage
from mindroom.usage_stats_storage import discover_admin_usage_sources
from mindroom.usage_storage import SYSTEM_USAGE_ENTITY, quote_identifier
from tests.conftest import seed_session

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_system_usage_is_content_free_idempotent_and_does_not_create_conversation_counts(tmp_path: Path) -> None:
    """Count one metered system request while preserving ordinary conversation counts."""
    config = Config(agents={"code": AgentConfig(display_name="Code")})
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage", process_env={},
    )
    before = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True, include_requests=True)
    assert before.coverage.scanned_sources == 1  # the configured agent is checked even before its DB exists
    assert before.request_coverage is not None
    assert before.request_coverage.scanned_sources == 1
    assert all(
        source.path_label != "system/sessions/system.db"
        for source in discover_admin_usage_sources(config=config, runtime_paths=paths)
    )

    response = RunOutput(
        run_id="stable-run",
        session_id="secret-session",
        user_id="secret-user",
        content="secret output",
        model="metered-model",
        model_provider="test-provider",
        created_at=1_723_837_600,
        metrics=RunMetrics(input_tokens=7, output_tokens=3, cache_read_tokens=2, total_tokens=10),
        messages=[
            Message(
                role="assistant",
                content="secret message",
                created_at=1_723_837_600,
                metrics=MessageMetrics(input_tokens=7, output_tokens=3, cache_read_tokens=2, total_tokens=10),
            ),
        ],
    )
    await record_system_usage(response, runtime_paths=paths, kind="routing")
    await record_system_usage(response, runtime_paths=paths, kind="routing")
    system_only = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert system_only.totals.total_tokens == 10
    assert system_only.session_count == 0
    assert system_only.request_coverage is not None
    assert system_only.request_coverage.unavailable_sources == before.request_coverage.unavailable_sources

    ordinary = create_session_storage("code", config, paths, execution_identity=None)
    try:
        seed_session(
            ordinary,
            AgentSession(
                session_id="ordinary",
                agent_id="code",
                user_id="@person:example.test",
                session_data={"session_metrics": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}},
                runs=[
                    RunOutput(
                        run_id="ordinary-run",
                        user_id="@person:example.test",
                        model="metered-model",
                        model_provider="test-provider",
                        created_at=1_723_837_600,
                        metrics=RunMetrics(input_tokens=4, output_tokens=1, total_tokens=5),
                    ),
                ],
            ),
        )
    finally:
        ordinary.close()

    report = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True, include_requests=True)
    assert report.totals.total_tokens == 15
    assert report.session_count == 1
    assert sum(row.run_count for row in report.model_breakdown) == 1
    assert sum(row.session_count for row in report.cumulative_model_breakdown) == 1
    system = next(row for row in report.breakdown if row.key == SYSTEM_USAGE_ENTITY)
    assert system.totals.total_tokens == 10
    assert system.session_count == system.run_count == 0
    assert sum(row.totals.total_tokens for row in report.daily_breakdown or ()) == 15
    assert report.request_breakdown is not None
    assert [
        (row.kind, row.totals.total_tokens) for row in report.request_breakdown if row.entity == SYSTEM_USAGE_ENTITY
    ] == [("routing", 10)]
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 1  # ordinary lacks request detail

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@person:example.test",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="ordinary",
    )
    self_report = collect_self_usage(
        agent_name="code",
        requester_id="@person:example.test",
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
    )
    assert self_report.totals.total_tokens == 5
    private_report = collect_private_usage(requester_id="@person:example.test", config=config, runtime_paths=paths)
    assert private_report.totals.total_tokens == 0

    db = paths.storage_root / "system" / "sessions" / "system.db"
    with sqlite3.connect(db) as connection:
        tables = [name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        persisted = " ".join(
            str(row)
            for table in tables
            for row in connection.execute(f"SELECT * FROM {quote_identifier(table)}")  # noqa: S608
        )
    assert "secret" not in persisted


@pytest.mark.asyncio
async def test_system_usage_storage_failure_logs_without_content_but_cancellation_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage failure is diagnostic only; task cancellation retains normal async behavior."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage", process_env={})
    response = RunOutput(run_id="paid", content="PRIVATE RESULT", metrics=RunMetrics(total_tokens=1))
    logged: list[tuple[str, dict[str, object]]] = []

    class CapturedLogger:
        def warning(self, event: str, **fields: object) -> None:
            logged.append((event, fields))

    monkeypatch.setattr(helper_usage, "logger", CapturedLogger())

    async def fail_write(*_args: object, **_kwargs: object) -> None:
        private_detail = "PRIVATE FAILURE DETAIL"
        raise OSError(private_detail)

    monkeypatch.setattr(helper_usage, "run_session_storage_operation", fail_write)
    await record_system_usage(response, runtime_paths=paths, kind="routing")
    assert logged == [("system_usage_recording_failed", {"kind": "routing", "error_type": "OSError"})]

    async def cancel_write(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(helper_usage, "run_session_storage_operation", cancel_write)
    with pytest.raises(asyncio.CancelledError):
        await record_system_usage(response, runtime_paths=paths, kind="routing")
