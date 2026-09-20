"""Paid helper calls retain content-free usage in their caller's conversation."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.metrics import MessageMetrics, RunMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput, RunStatus

from mindroom.agent_storage import create_session_storage, get_agent_session, get_team_session
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.custom_tools.dynamic_workflow import _aexecute_participant, _arun_agent
from mindroom.dynamic_workflows.runner import DynamicWorkflowExecutionError
from mindroom.helper_usage import HelperUsageOwner, get_helper_usage_owner, helper_usage_context, record_helper_usage
from mindroom.history.session_context import open_bound_scope_session_context, open_resolved_scope_session_context
from mindroom.history.types import HistoryScope
from mindroom.hooks import HookRegistry
from mindroom.memory.auto_flush import _extract_memory_summary
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.usage_stats import collect_admin_usage, collect_self_usage
from mindroom.usage_storage import quote_identifier
from tests.history_helpers import (
    RecordingModel,
    _completed_run,
    _forced_compaction_context,
    _hook_runtime_context,
    _make_config,
    _session,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _HelperModel(RecordingModel):
    responses: list[ModelResponse] = field(default_factory=list)

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return self.responses.pop(0)

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        yield self.responses.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["Remember the sample preference.", "NO_REPLY"])
@pytest.mark.parametrize("has_requester", [True, False])
async def test_memory_usage_survives_rejected_output_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    has_requester: bool,
) -> None:
    """Discarding a returned extractor's metrics loses paid retries and rejected output."""
    run = _completed_run("ordinary")
    run.user_id = "@previous:localhost"
    run.metrics = RunMetrics(input_tokens=7, output_tokens=3, total_tokens=10)
    session = _session("session", runs=[run])
    session.user_id = "@previous:localhost"
    session.session_data = {"session_metrics": run.metrics.to_dict()}
    config, paths, storage, _scope, context = _forced_compaction_context(tmp_path, session=session)
    assert isinstance(storage, SqliteDb)
    config.memory.auto_flush.extractor.include_memory_context.memory_snippets = 0
    identity = build_execution_identity_from_runtime_context(context)
    model = _HelperModel(
        id="helper-model",
        provider="test-provider",
        responses=[
            ModelResponse(
                content=content,
                response_usage=MessageMetrics(input_tokens=100, output_tokens=10, total_tokens=110),
            ),
            ModelResponse(
                content="Remember another preference.",
                response_usage=MessageMetrics(input_tokens=50, output_tokens=5, total_tokens=55),
            ),
        ],
    )
    monkeypatch.setattr("mindroom.memory.auto_flush.model_loading.get_model_instance", lambda *_args: model)
    try:
        for _ in range(2):
            await _extract_memory_summary(
                config=config,
                runtime_paths=paths,
                storage_path=paths.storage_root,
                agent_name="test_agent",
                session_id="session",
                lines=["Synthetic conversation excerpt."],
                execution_identity=identity if has_requester else None,
            )
        report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
        assert report.totals.total_tokens == 175
        assert sum(row.run_count for row in report.model_breakdown) == 1
        assert sum(row.totals.total_tokens for row in report.cumulative_model_breakdown) == 175
        assert report.request_breakdown is not None
        assert [row.totals.total_tokens for row in report.request_breakdown] == [110, 55]
        assert {row.kind for row in report.request_breakdown} == {"memory_auto_flush"}
        expected_requester = "@user:localhost" if has_requester else None
        assert {row.user_id for row in report.request_breakdown} == {expected_requester}
        own = collect_self_usage(
            agent_name="test_agent",
            requester_id="@user:localhost",
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
        )
        assert own.totals.total_tokens == (165 if has_requester else 0)
        with storage.db_engine.connect() as connection:
            snapshots = connection.exec_driver_sql("SELECT usage_data FROM test_agent_sessions_usage").scalars().all()
        helpers = [json.loads(value) for value in snapshots if json.loads(value).get("kind") == "memory_auto_flush"]
        assert len(helpers) == 2
        assert len({row["run_id"] for row in helpers}) == 2
        assert "Synthetic conversation" not in json.dumps(helpers)
        assert "Remember" not in json.dumps(helpers)
        storage.upsert_session(session)
        assert collect_admin_usage(config=config, runtime_paths=paths).totals == report.totals
    finally:
        storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["agent", "private_agent", "configured_team", "ad_hoc_team"])
@pytest.mark.parametrize("participant_kind", ["ephemeral_agent", "room_agent"])
async def test_workflow_first_turn_uses_actual_caller_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    participant_kind: str,
) -> None:
    """Synthetic participant IDs must not replace private, configured or ad hoc caller ownership."""
    config, paths = _make_config(tmp_path)
    config.memory.backend = "none"
    if caller == "private_agent":
        config.agents["test_agent"].private = AgentPrivateConfig(per="user", root="mind_data")
    config.agents["second"] = AgentConfig(display_name="Second")
    config.teams["reviewers"] = TeamConfig(display_name="Reviewers", role="Review", agents=["test_agent", "second"])
    persist_entity_accounts(config, paths)
    context = _hook_runtime_context(
        config=config,
        runtime_paths=paths,
        registry=HookRegistry.empty(),
        session_id="first-turn",
    )
    identity = build_execution_identity_from_runtime_context(context)
    if caller.endswith("team"):
        open_scope = partial(
            open_bound_scope_session_context,
            agents=[Agent(id="test_agent"), Agent(id="second")],
            team_name="reviewers" if caller == "configured_team" else None,
            session_id=context.session_id,
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
            create_session_if_missing=True,
        )
    else:
        open_scope = partial(
            open_resolved_scope_session_context,
            agent_name="test_agent",
            scope=HistoryScope(kind="agent", scope_id="test_agent"),
            session_id=context.session_id,
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
            create_session_if_missing=True,
        )
    model = _HelperModel(
        id="helper-model",
        provider="test-provider",
        responses=[
            ModelResponse(
                content="Sample output.",
                response_usage=MessageMetrics(input_tokens=20, output_tokens=2, total_tokens=22),
            ),
        ],
    )
    monkeypatch.setattr("mindroom.model_loading.get_model_instance", lambda *_args, **_kwargs: model)
    with open_scope() as scope_context, helper_usage_context(scope_context):
        assert scope_context is not None
        storage = scope_context.storage
        assert isinstance(storage, SqliteDb)
        result = await _aexecute_participant(
            context,
            {"id": "writer", "kind": participant_kind, "agent": "test_agent"},
            "Sample task.",
            run_scope="sample-workflow",
        )
        assert result == "Sample output."
        loaded = (get_team_session if caller.endswith("team") else get_agent_session)(storage, context.session_id)
        assert loaded is not None, "First-turn helper usage needs its actual caller session row"
        assert not loaded.runs
        with storage.db_engine.connect() as connection:
            rows = connection.exec_driver_sql(
                f"SELECT session_id, usage_data FROM {quote_identifier(storage.session_table_name + '_usage')}",  # noqa: S608
            ).all()
        assert len(rows) == 1
        assert rows[0][0] == "first-turn"
        snapshot = json.loads(rows[0][1])
        assert snapshot["kind"] == "dynamic_workflow"
        assert snapshot["user_id"] == "@user:localhost"
        assert snapshot["metrics"]["total_tokens"] == 22
        assert "Sample" not in rows[0][1]
    if caller == "ad_hoc_team":
        # Existing reporting policy excludes unconfigured teams, even though their store is retained.
        return
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 22
    assert sum(row.run_count for row in report.model_breakdown) == 0
    assert report.request_breakdown is not None
    assert len(report.request_breakdown) == 1
    assert report.request_breakdown[0].totals.total_tokens == 22
    if caller in {"agent", "private_agent"}:
        own = collect_self_usage(
            agent_name="test_agent",
            requester_id=context.requester_id,
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
        )
        other = collect_self_usage(
            agent_name="test_agent",
            requester_id="@other:localhost",
            config=config,
            runtime_paths=paths,
            execution_identity=replace(identity, requester_id="@other:localhost"),
        )
        assert own.totals.total_tokens == 22
        assert other.totals.total_tokens == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("details", ["complete", "partial", "absent"])
@pytest.mark.parametrize("status", [RunStatus.completed, RunStatus.error])
@pytest.mark.parametrize("response_id", ["paid-attempt", None])
async def test_workflow_retains_paid_status_without_inventing_request_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    details: str,
    status: RunStatus,
    response_id: str | None,
) -> None:
    """Repeated final outputs upsert once; incomplete request details never become a combined request."""
    config, paths, storage, scope, context = _forced_compaction_context(tmp_path, session=_session("session"))
    messages = [
        Message(
            role="assistant",
            content="First sample.",
            metrics=MessageMetrics(input_tokens=10, output_tokens=1, total_tokens=11),
        ),
        Message(
            role="assistant",
            content="Second sample.",
            metrics=MessageMetrics(input_tokens=20, output_tokens=2, total_tokens=22),
        ),
    ]
    response = RunOutput(
        run_id=response_id,
        session_id="synthetic-session",
        user_id="@untrusted:localhost",
        parent_run_id="synthetic-parent",
        model="helper-model",
        model_provider="test-provider",
        content="Sample output.",
        status=status,
        metrics=RunMetrics(input_tokens=30, output_tokens=3, total_tokens=33),
        messages=messages if details == "complete" else messages[:1] if details == "partial" else None,
    )
    actor = Agent()

    async def outputs(*_args: object, **_kwargs: object) -> AsyncIterator[RunOutput]:
        yield response
        yield response

    monkeypatch.setattr(actor, "arun", outputs)
    open_scope = partial(
        open_resolved_scope_session_context,
        agent_name="test_agent",
        scope=scope,
        session_id="session",
        config=config,
        runtime_paths=paths,
        execution_identity=build_execution_identity_from_runtime_context(context),
    )
    try:
        with open_scope() as scope_context, helper_usage_context(scope_context):
            if status == RunStatus.error:
                with pytest.raises(DynamicWorkflowExecutionError, match="Sample output"):
                    await _arun_agent(context, actor, "Sample task.")
            else:
                assert await _arun_agent(context, actor, "Sample task.") == "Sample output."
        report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
        assert report.totals.total_tokens == 33
        assert sum(row.run_count for row in report.model_breakdown) == 0
        assert report.user_breakdown[0].user_id == "@user:localhost"
        assert report.request_breakdown is not None
        assert [row.totals.total_tokens for row in report.request_breakdown] == (
            [11, 22] if details == "complete" else []
        )
        assert report.request_coverage is not None
        assert report.request_coverage.unavailable_sources == (0 if details == "complete" else 1)
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_scope_reset_and_first_turn_write_preserve_concurrent_session(tmp_path: Path) -> None:
    """A helper owner must reset on cancellation, and first-turn initialization must not overwrite live state."""
    config, paths = _make_config(tmp_path)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    open_scope = partial(
        open_resolved_scope_session_context,
        agent_name="test_agent",
        scope=scope,
        session_id="session",
        config=config,
        runtime_paths=paths,
        execution_identity=None,
        create_session_if_missing=True,
    )
    assert get_helper_usage_owner() is None
    with (  # noqa: PT012 - cancellation intentionally exits the bound scope
        pytest.raises(asyncio.CancelledError),
        open_scope() as context,
        helper_usage_context(context),
    ):
        assert context is not None
        owner = get_helper_usage_owner()
        assert owner is not None
        assert owner.initial_session is not None
        session = _session("session")
        session.metadata = {"marker": "concurrent session"}
        context.storage.upsert_session(session)
        await record_helper_usage(
            RunOutput(run_id="paid", metrics=RunMetrics(input_tokens=10, output_tokens=1, total_tokens=11)),
            owner=owner,
            invocation_id="paid",
            kind="dynamic_workflow",
            requester_id=None,
        )
        loaded = get_agent_session(context.storage, "session")
        assert loaded is not None
        assert loaded.metadata == {"marker": "concurrent session"}
        raise asyncio.CancelledError
    assert get_helper_usage_owner() is None
    assert collect_admin_usage(config=config, runtime_paths=paths).totals.total_tokens == 11
    with open_scope() as context:
        assert context is not None
        assert isinstance(context.storage, SqliteDb)
        context.storage.delete_session("session")
        with context.storage.db_engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM test_agent_sessions_usage").scalar_one() == 0


@pytest.mark.asyncio
async def test_persisted_participant_usage_is_not_added_again(tmp_path: Path) -> None:
    """A participant with its own DB already contributes an ordinary run snapshot."""
    config, paths, storage, scope, context = _forced_compaction_context(tmp_path, session=_session("session"))
    model = _HelperModel(
        id="helper-model",
        provider="test-provider",
        responses=[
            ModelResponse(
                content="Sample output.",
                response_usage=MessageMetrics(input_tokens=20, output_tokens=2, total_tokens=22),
            ),
        ],
    )
    actor = Agent(id="test_agent", db=storage, model=model, telemetry=False)
    open_scope = partial(
        open_resolved_scope_session_context,
        agent_name="test_agent",
        scope=scope,
        session_id="session",
        config=config,
        runtime_paths=paths,
        execution_identity=build_execution_identity_from_runtime_context(context),
    )
    try:
        with open_scope() as scope_context, helper_usage_context(scope_context):
            assert await _arun_agent(context, actor, "Sample task.") == "Sample output."
        report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
        assert report.totals.total_tokens == 22
        assert sum(row.run_count for row in report.model_breakdown) == 1
        assert report.request_breakdown is not None
        assert {row.kind for row in report.request_breakdown} == {"run"}
    finally:
        storage.close()


@pytest.mark.asyncio
async def test_returned_helper_usage_write_finishes_after_cancellation(tmp_path: Path) -> None:
    """Once counters are returned, cancellation must wait for the accepted SQLite write."""
    config, paths, storage, _scope, _context = _forced_compaction_context(tmp_path, session=_session("session"))
    assert isinstance(storage, SqliteDb)
    started = threading.Event()

    def create_storage() -> SqliteDb:
        started.set()
        opened = create_session_storage("test_agent", config, paths, execution_identity=None)
        assert isinstance(opened, SqliteDb)
        return opened

    with storage.db_engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        task = asyncio.create_task(
            record_helper_usage(
                RunOutput(metrics=RunMetrics(input_tokens=20, output_tokens=2, total_tokens=22)),
                owner=HelperUsageOwner(create_storage, "session"),
                invocation_id="paid-attempt",
                kind="memory_auto_flush",
                requester_id=None,
            ),
        )
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=0.02)
            assert not done
        finally:
            connection.rollback()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            storage.close()
    assert collect_admin_usage(config=config, runtime_paths=paths).totals.total_tokens == 22
