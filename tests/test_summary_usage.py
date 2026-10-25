"""Incurred summary usage survives compaction, including rejected output."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from contextlib import closing
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.history.compaction import _summarize_chunk, compact_scope_history, scope_usage_recorder
from mindroom.history.storage import record_summary_usage
from mindroom.history.summary_call import _CompactionSummaryTimeoutError, generate_compaction_summary
from mindroom.history.types import HistoryScopeState
from mindroom.llm.messages import Message
from mindroom.llm.usage import MessageMetrics, ModelMetrics, RunMetrics, TokenCounts
from mindroom.session_storage.store import ConversationStore
from mindroom.session_storage.types import session_state_snapshot
from mindroom.tool_system.runtime_context import (
    DetachedRequesterContext,
    detached_requester_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from mindroom.usage.stats import collect_admin_usage, collect_self_usage
from tests.conftest import install_model_providers, prepare_history_for_run_for_test
from tests.fakes.scripted_model import Await
from tests.fakes.summary_model import Reply, ScriptedSummaryModel, summary_model, summary_steps, summary_turn
from tests.history_helpers import (
    _completed_run,
    _forced_compaction_context,
    _reported,
    _session,
    _summarizer,
)

if TYPE_CHECKING:
    from pathlib import Path


def _summary_model(*replies: Reply) -> ScriptedSummaryModel:
    """Return a summary model whose stored usage rows name ``test-provider``."""
    return summary_model(*replies, provider="test-provider")


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
    run.metrics = RunMetrics(tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10))
    run.metrics.details = {
        "model": [
            ModelMetrics(
                tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10),
                id="answer-model",
                provider="test-provider",
            ),
        ],
    }
    session = _session("session", runs=[run])
    config, paths, storage, scope, context = _forced_compaction_context(tmp_path, session=session)
    model = _summary_model(
        *[
            summary_turn(
                content,
                usage=MessageMetrics(
                    tokens=TokenCounts(
                        input_tokens=100,
                        output_tokens=10,
                        total_tokens=110,
                        cache_read_tokens=40,
                        reasoning_tokens=2,
                    ),
                ),
            ),
        ],
    )
    with tool_runtime_context(context if has_requester else None):
        operation = compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=HistoryScopeState(force_compact_before_next_run=True),
            summarizer=_summarizer(
                model,
                10_000,
                name="summary",
                timeout_seconds=10,
                scope=scope,
                prompt="Summarize.",
                on_usage=scope_usage_recorder(storage, session.session_id),
            ),
            available_history_budget=None,
            replay_window_tokens=None,
            threshold_tokens=None,
            before_tokens=0,
            progress=None,
            complete=_reported,
            replay_model=None,
        )
        if content:
            assert await operation is not None
        else:
            with pytest.raises(RuntimeError, match="no result"):
                await operation

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

    with storage.transaction(write=False) as connection:
        snapshots = [value for (value,) in connection.execute("SELECT usage_data FROM test_agent_sessions_usage")]
    summary = next(json.loads(value) for value in snapshots if json.loads(value).get("kind") == "compaction_summary")
    assert "A complete summary" not in json.dumps(summary)
    assert "question" not in json.dumps(summary)
    assert summary["requests"][0]["metrics"]["input_tokens"] == 100
    # Repeated exports and session writes cannot add the same summary twice.
    storage.save_session_state(session, session_state_snapshot(session.session_state))
    assert collect_admin_usage(config=config, runtime_paths=paths).totals == report.totals


@pytest.mark.asyncio
async def test_summary_cost_counts_toward_api_requester(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An OpenAI-compatible request has no Matrix conversation, so its summary is charged to the API requester."""
    session = _session("session", runs=[_completed_run("original")])
    config, paths, storage, _scope, _context = _forced_compaction_context(tmp_path, session=session)
    model = _summary_model(
        *[
            summary_turn(
                "A complete summary.",
                usage=MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=10, total_tokens=110)),
            ),
        ],
    )
    authority = DetachedRequesterContext(
        requester_id="@api:localhost",
        config=config,
        runtime_paths=paths,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        config_provider=lambda: config,
        budget_monitor=None,
    )
    # Through the production preparation path, which wires the summary's usage recorder.
    install_model_providers(monkeypatch, {"default": model})
    with detached_requester_context(authority):
        prepared = await prepare_history_for_run_for_test(
            agent_name="test_agent",
            full_prompt="Current prompt",
            session_id="session",
            runtime_paths=paths,
            config=config,
            execution_identity=None,
            storage=storage,
            session=session,
        )
    assert prepared.compaction_reply_outcome == "success"

    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.request_breakdown is not None
    assert [(request.kind, request.user_id) for request in report.request_breakdown] == [
        ("compaction_summary", "@api:localhost"),
    ]


@pytest.mark.asyncio
async def test_retry_keeps_cost_of_rejected_attempt(tmp_path: Path) -> None:
    """A successful retry adds its cost without replacing the earlier paid response."""
    # Large enough that the retry after the empty result can send a smaller input.
    run = _completed_run("original", messages=[Message(role="user", content="Long input. " * 30_000)])
    session = _session("session", runs=[run])
    config, paths, storage, scope, _context = _forced_compaction_context(tmp_path, session=session)
    assert isinstance(storage, ConversationStore)
    model = _summary_model(
        *[
            summary_turn(
                "",
                usage=MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=10, total_tokens=110)),
            ),
            summary_turn(
                "Complete.",
                usage=MessageMetrics(tokens=TokenCounts(input_tokens=50, output_tokens=5, total_tokens=55)),
            ),
        ],
    )
    result = await _summarize_chunk(
        _summarizer(
            model,
            100_000,
            name="summary",
            timeout_seconds=10,
            session_id="session",
            scope=scope,
            on_usage=partial(record_summary_usage, storage=storage, session_id="session", requester_id=None),
            prompt="Summarize.",
        ),
        None,
        [run],
    )
    assert result.summary == "Complete."
    report = collect_admin_usage(config=config, runtime_paths=paths, include_daily=True)
    assert report.totals.total_tokens == 165
    assert report.model_breakdown[0].totals.total_tokens == 165
    assert report.model_breakdown[0].run_count == 0
    assert report.daily_breakdown[0].run_count == 0


@pytest.mark.asyncio
async def test_usage_storage_timeout_cannot_issue_another_paid_summary(tmp_path: Path) -> None:
    """A storage timeout must stay outside the model's shrink-and-retry policy."""
    run = _completed_run("original")
    _config, _paths, _storage, scope, _context = _forced_compaction_context(
        tmp_path,
        session=_session("session", runs=[run]),
    )

    async def locked_usage_storage(_model: object, _usage: MessageMetrics) -> None:
        msg = "database is locked"
        raise sqlite3.OperationalError(msg)

    model = _summary_model(
        *[
            summary_turn(
                content,
                usage=MessageMetrics(tokens=TokenCounts(input_tokens=20, output_tokens=2, total_tokens=22)),
            )
            for content in ("Complete summary.", "Unnecessary second summary.")
        ],
    )
    with pytest.raises(RuntimeError, match="Could not persist") as error:
        await _summarize_chunk(
            _summarizer(
                model,
                100_000,
                name="summary",
                timeout_seconds=10,
                session_id="session",
                scope=scope,
                on_usage=locked_usage_storage,
                prompt="Summarize.",
            ),
            None,
            [run],
        )
    assert isinstance(error.value.__cause__, sqlite3.OperationalError)
    assert len(model.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_after_response", [False, True])
async def test_summary_waits_for_locked_usage_storage_without_provider_timeout(
    tmp_path: Path,
    cancel_after_response: bool,
) -> None:
    """An on-time response stays accepted while its durable usage write waits for SQLite."""
    config, paths, storage, _scope, _context = _forced_compaction_context(tmp_path, session=_session("session"))
    assert isinstance(storage, ConversationStore)
    recording = asyncio.Event()
    recorded = asyncio.Event()
    model = _summary_model(
        *[
            summary_turn(
                "Complete summary.",
                usage=MessageMetrics(tokens=TokenCounts(input_tokens=20, output_tokens=2, total_tokens=22)),
            ),
        ],
    )

    async def record(usage: MessageMetrics) -> None:
        recording.set()
        try:
            await record_summary_usage(model.identity, usage, storage=storage, session_id="session", requester_id=None)
        finally:
            recorded.set()

    with closing(sqlite3.connect(storage.path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        task = asyncio.create_task(
            generate_compaction_summary(
                model=model,
                summary_input="Input.",
                summary_prompt="Summarize.",
                timeout_seconds=0.01,
                on_usage=record,
            ),
        )
        try:
            await asyncio.wait_for(recording.wait(), timeout=2)
            done, _pending = await asyncio.wait({task}, timeout=0.05)
            assert not done, "Usage persistence must not turn a completed response into a provider timeout"
            if cancel_after_response:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
            connection.rollback()
            if not cancel_after_response:
                summary = await asyncio.wait_for(task, timeout=2)
                assert summary == "Complete summary."
            await asyncio.wait_for(recorded.wait(), timeout=2)
            report = collect_admin_usage(config=config, runtime_paths=paths)
            assert report.totals.total_tokens == 22
            assert report.model_breakdown[0].run_count == 0
        finally:
            connection.rollback()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(recorded.wait(), timeout=2)


@pytest.mark.asyncio
async def test_late_summary_response_still_records_usage_after_timeout(tmp_path: Path) -> None:
    """A provider ignoring cancellation must not lose the returned counters."""
    config, paths, storage, _scope, _context = _forced_compaction_context(tmp_path, session=_session("session"))
    assert isinstance(storage, ConversationStore)
    recorded = asyncio.Event()

    async def ignore_cancellation() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(10)

    late_summary = summary_turn(
        "Late summary.",
        usage=MessageMetrics(tokens=TokenCounts(input_tokens=20, output_tokens=2, total_tokens=22)),
    )
    model = _summary_model([Await(ignore_cancellation), *summary_steps(late_summary)])

    async def record(usage: MessageMetrics) -> None:
        await record_summary_usage(model.identity, usage, storage=storage, session_id="session", requester_id=None)
        recorded.set()

    with pytest.raises(_CompactionSummaryTimeoutError):
        await generate_compaction_summary(
            model=model,
            summary_input="Input.",
            summary_prompt="Summarize.",
            timeout_seconds=0.01,
            on_usage=record,
        )
    await asyncio.wait_for(recorded.wait(), timeout=2)
    report = collect_admin_usage(config=config, runtime_paths=paths)
    assert report.totals.total_tokens == 22
    assert report.model_breakdown[0].run_count == 0


@pytest.mark.asyncio
async def test_summary_response_without_usage_records_nothing() -> None:
    """A provider that reports no usage leaves the usage storage untouched, so its failures cannot fail the summary."""
    model = _summary_model("A complete summary.")
    on_usage = AsyncMock()

    summary = await generate_compaction_summary(
        model=model,
        summary_input="Input.",
        summary_prompt="Summarize.",
        timeout_seconds=10,
        on_usage=on_usage,
    )

    assert summary == "A complete summary."
    on_usage.assert_not_awaited()
