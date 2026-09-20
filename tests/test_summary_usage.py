"""Incurred summary usage survives compaction, including rejected output."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

import pytest
from agno.db.sqlite import SqliteDb
from agno.metrics import MessageMetrics, ModelMetrics, RunMetrics
from agno.models.response import ModelResponse
from sqlalchemy import event

from mindroom.history.compaction import (
    SummaryModel,
    _generate_compaction_summary_with_retry,
    compact_scope_history,
)
from mindroom.history.storage import record_summary_usage
from mindroom.history.summary_call import _CompactionSummaryTimeoutError, generate_compaction_summary
from mindroom.history.types import HistoryScopeState
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.usage_stats import collect_admin_usage, collect_self_usage
from tests.history_helpers import (
    _ALL_HISTORY_SETTINGS,
    RecordingModel,
    _completed_run,
    _forced_compaction_context,
    _session,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _SummaryModel(RecordingModel):
    responses: list[ModelResponse] = field(default_factory=list)

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        """Return provider counters through Agno's real response machinery."""
        return self.responses.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["A complete summary.", ""])
@pytest.mark.parametrize("has_requester", [True, False])
async def test_summary_cost_survives_success_or_rejected_output(  # noqa: PLR0915 - reconcile every view after one compaction
    tmp_path: Path,
    content: str,
    has_requester: bool,
) -> None:
    """Every returned billable response counts once, without becoming an AI message."""
    run = _completed_run("original")
    run.model = "answer-model"
    run.model_provider = "test-provider"
    run.user_id = "@previous:localhost"
    run.metrics = RunMetrics(input_tokens=7, output_tokens=3, total_tokens=10)
    run.metrics.details = {
        "model": [
            ModelMetrics(id="answer-model", provider="test-provider", input_tokens=7, output_tokens=3, total_tokens=10),
        ],
    }
    session = _session("session", runs=[run])
    session.user_id = "@previous:localhost"
    session.session_data = {"session_metrics": run.metrics.to_dict()}
    config, paths, storage, scope, context = _forced_compaction_context(tmp_path, session=session)
    assert isinstance(storage, SqliteDb)
    disposed: list[bool] = []
    event.listen(storage.db_engine, "engine_disposed", lambda _engine: disposed.append(True))
    model = _SummaryModel(
        id="summary-model",
        provider="test-provider",
        responses=[
            ModelResponse(
                content=content,
                response_usage=MessageMetrics(
                    input_tokens=100,
                    output_tokens=10,
                    total_tokens=110,
                    cache_read_tokens=40,
                    reasoning_tokens=2,
                ),
            ),
        ],
    )
    try:
        with tool_runtime_context(context if has_requester else None):
            operation = compact_scope_history(
                storage=storage,
                session=session,
                scope=scope,
                state=HistoryScopeState(force_compact_before_next_run=True),
                history_settings=_ALL_HISTORY_SETTINGS,
                available_history_budget=None,
                summary_model=SummaryModel(model=model, name="summary", input_budget_tokens=10_000),
                replay_window_tokens=None,
                threshold_tokens=None,
                summary_prompt="Summarize.",
                summary_timeout_seconds=10,
            )
            if content:
                assert await operation is not None
            else:
                with pytest.raises(RuntimeError, match="no result"):
                    await operation

        # A summary write borrows the conversation adapter; its caller owns closing it.
        assert not disposed
        report = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True, include_requests=True)
        assert report.totals.total_tokens == 120
        assert report.totals.cache_read_tokens == 40
        assert report.totals.reasoning_tokens == 2
        assert sum(row.totals.total_tokens for row in report.model_breakdown) == 120
        assert sum(row.totals.total_tokens for row in report.cumulative_model_breakdown) == 120
        assert sum(row.totals.total_tokens for row in report.daily_breakdown) == 120
        assert sum(row.totals.total_tokens for row in report.user_breakdown) == 120
        assert sum(row.run_count for row in report.model_breakdown) == 1
        assert sum(row.run_count for row in report.daily_breakdown) == 1
        assert report.breakdown[0].run_count == 1
        assert report.breakdown[0].session_count == 1
        expected_requester = "@user:localhost" if has_requester else None
        summary_user = next(row for row in report.user_breakdown if row.user_id == expected_requester)
        assert summary_user.totals.total_tokens == 110
        assert summary_user.run_count == 0
        assert report.request_breakdown is not None
        assert len(report.request_breakdown) == 1
        request = report.request_breakdown[0]
        assert request.kind == "compaction_summary"
        assert request.model == "summary-model"
        assert request.model_provider == "test-provider"
        assert request.user_id == expected_requester
        assert request.totals.total_tokens == 110
        assert request.totals.cache_read_tokens == 40
        if not has_requester:
            identity = build_tool_execution_identity(
                channel="matrix",
                agent_name="test_agent",
                runtime_paths=paths,
                requester_id="@previous:localhost",
                room_id=None,
                thread_id=None,
                resolved_thread_id=None,
                session_id="session",
            )
            own = collect_self_usage(
                agent_name="test_agent",
                requester_id="@previous:localhost",
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
            )
            assert own.totals.total_tokens == 10
            assert own.model_breakdown[0].totals.total_tokens == 10
            assert own.coverage.unavailable_sources == 1
            assert own.model_coverage.unavailable_sources == 1
        assert next(row for row in report.cumulative_model_breakdown if row.model == "summary-model").session_count == 1

        with storage.db_engine.connect() as connection:
            snapshots = connection.exec_driver_sql("SELECT usage_data FROM test_agent_sessions_usage").scalars().all()
        summary = next(
            json.loads(value) for value in snapshots if json.loads(value).get("kind") == "compaction_summary"
        )
        assert "A complete summary" not in json.dumps(summary)
        assert "question" not in json.dumps(summary)
        assert summary["requests"][0]["metrics"]["input_tokens"] == 100
        # Repeated exports and session upserts cannot add the same summary twice.
        storage.upsert_session(session)
        assert collect_admin_usage(config=config, runtime_paths=paths).totals == report.totals
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_retry_keeps_cost_of_rejected_attempt(tmp_path: Path) -> None:
    """A successful retry adds its cost without replacing the earlier paid response."""
    run = _completed_run("original")
    session = _session("session", runs=[run])
    config, paths, storage, scope, _context = _forced_compaction_context(tmp_path, session=session)
    assert isinstance(storage, SqliteDb)
    model = _SummaryModel(
        id="summary-model",
        provider="test-provider",
        responses=[
            ModelResponse(
                content="",
                response_usage=MessageMetrics(input_tokens=100, output_tokens=10, total_tokens=110),
            ),
            ModelResponse(
                content="Complete.",
                response_usage=MessageMetrics(input_tokens=50, output_tokens=5, total_tokens=55),
            ),
        ],
    )
    try:
        result = await _generate_compaction_summary_with_retry(
            summary_model=SummaryModel(model=model, name="summary", input_budget_tokens=100_000),
            previous_summary=None,
            compactable_runs=[run],
            initial_summary_input="Long input. " * 30_000,
            initial_included_runs=[run],
            session_id="session",
            scope=scope,
            history_settings=_ALL_HISTORY_SETTINGS,
            summary_prompt="Summarize.",
            timeout_seconds=10,
            on_response=partial(record_summary_usage, storage=storage, session_id="session", requester_id=None),
        )
        assert result.summary.summary == "Complete."
        report = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True)
        assert report.totals.total_tokens == 165
        assert report.model_breakdown[0].totals.total_tokens == 165
        assert report.model_breakdown[0].run_count == 0
        assert report.daily_breakdown[0].run_count == 0
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_late_summary_response_still_records_usage_after_timeout(tmp_path: Path) -> None:
    """A provider ignoring cancellation must not lose the returned counters."""
    config, paths, storage, _scope, _context = _forced_compaction_context(tmp_path, session=_session("session"))
    assert isinstance(storage, SqliteDb)
    recorded = asyncio.Event()

    class _LateModel(RecordingModel):
        async def aresponse(self, *_args: object, **_kwargs: object) -> ModelResponse:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return ModelResponse(
                    content="Late summary.",
                    response_usage=MessageMetrics(
                        input_tokens=20,
                        output_tokens=2,
                        total_tokens=22,
                    ),
                )
            raise AssertionError

    model = _LateModel(id="summary-model", provider="test-provider")

    async def record(response: ModelResponse) -> None:
        await record_summary_usage(model, response, storage=storage, session_id="session", requester_id=None)
        recorded.set()

    try:
        with pytest.raises(_CompactionSummaryTimeoutError):
            await generate_compaction_summary(
                model=model,
                summary_input="Input.",
                summary_prompt="Summarize.",
                timeout_seconds=0.01,
                on_response=record,
            )
        await asyncio.wait_for(recorded.wait(), timeout=2)
        report = collect_admin_usage(config=config, runtime_paths=paths)
        assert report.totals.total_tokens == 22
        assert report.model_breakdown[0].run_count == 0
    finally:
        storage.close()
