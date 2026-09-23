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
from agno.tools.calculator import CalculatorTools

from mindroom.agent_storage import create_session_storage, get_agent_session, get_team_session
from mindroom.agents import create_agent
from mindroom.approval_tools import toolkit_owners_for_agents
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.models import ToolConfigEntry
from mindroom.custom_tools.dynamic_workflow import _aexecute_participant, _arun_agent
from mindroom.dynamic_workflows.runner import DynamicWorkflowExecutionError
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.helper_usage import HelperUsageOwner, get_helper_usage_owner, helper_usage_context, record_helper_usage
from mindroom.history.session_context import (
    close_agent_runtime_state_dbs,
    close_team_runtime_state_dbs,
    open_bound_scope_session_context,
    open_resolved_scope_session_context,
)
from mindroom.history.types import HistoryScope
from mindroom.hooks import HookRegistry
from mindroom.memory.auto_flush import _extract_memory_summary
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt, paused_attempt_from_response
from mindroom.teams import (
    TeamMode,
    _attach_team_pause_presentation,
    build_materialized_team_instance,
    continue_paused_team_run,
    materialize_exact_team_members,
)
from mindroom.tool_system.runtime_context import ToolDispatchContext, build_execution_identity_from_runtime_context
from mindroom.usage_stats import collect_admin_usage, collect_self_usage
from mindroom.usage_storage import quote_identifier
from tests.conftest import unwrap_extracted_collaborator
from tests.history_helpers import (
    RecordingModel,
    _completed_run,
    _forced_compaction_context,
    _hook_runtime_context,
    _make_config,
    _session,
)
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot
from tests.test_approval_dynamic_continuation import _call, _ScriptedModel

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
@pytest.mark.parametrize("caller", ["agent", "private_agent", "team"])
@pytest.mark.parametrize("outcome", ["completed", "error", "cancel"])
async def test_approval_resumed_helpers_keep_caller_usage_and_reset_context(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    outcome: str,
) -> None:
    """Approval resume must retain paid helper output in its real caller store even if the resumed run fails."""
    config, paths = _make_config(tmp_path)
    config.agents["general"] = config.agents.pop("test_agent")
    config.agents["general"].tools = [ToolConfigEntry(name="calculator")]
    config.defaults.learning = False
    config.memory.backend = "none"
    config.models["default"].provider = "synthetic"
    config.models["default"].id = "synthetic"
    config.tool_approval = ToolApprovalConfig.model_validate(
        {"default": "auto_approve", "rules": [{"match": "add", "action": "require_approval"}]},
    )
    config.agents["general"].private = (
        AgentPrivateConfig(per="user", root="mind_data") if caller == "private_agent" else None
    )
    if caller == "team":
        config.agents["second"] = AgentConfig(display_name="Second")
        config.teams["reviewers"] = TeamConfig(display_name="Reviewers", role="Review", agents=["general", "second"])
    persist_entity_accounts(config, paths)
    context = replace(
        _hook_runtime_context(
            config=config,
            runtime_paths=paths,
            registry=HookRegistry.empty(),
            session_id="approval-session",
        ),
        agent_name="general",
    )
    identity = build_execution_identity_from_runtime_context(context)
    helper_context = replace(context, target=replace(context.target, session_id="ephemeral-participant-session"))
    responses: list[ModelResponse | RuntimeError] = [
        *(
            [_call("delegate_task_to_member", "delegate", member_id="general", task="Add 2 and 3")]
            if caller == "team"
            else []
        ),
        _call("add", "approved", a=2, b=3),
        *(
            [RuntimeError("Resumed provider failed")] * 2
            if outcome == "error"
            else [ModelResponse(content="Finished.")] * 2
        ),
    ]
    monkeypatch.setattr(
        "mindroom.model_loading.get_model_instance",
        lambda *_args, **_kwargs: _ScriptedModel(id="synthetic", responses=responses),
    )
    original_add = CalculatorTools.add
    helper_results: list[object] = []

    async def add(self: CalculatorTools, a: float, b: float) -> str:
        helper = Agent(
            model=_HelperModel(
                id="helper-model",
                provider="test-provider",
                responses=[
                    ModelResponse(
                        content="Sample helper output.",
                        response_usage=MessageMetrics(input_tokens=20, output_tokens=2, total_tokens=22),
                    ),
                ],
            ),
            telemetry=False,
        )
        helper_results.append(await _arun_agent(helper_context, helper, "Sample task."))
        if outcome == "cancel":
            raise asyncio.CancelledError
        return original_add(self, a, b)

    monkeypatch.setattr(CalculatorTools, "add", add)
    scope = HistoryScope(
        kind="team" if caller == "team" else "agent",
        scope_id="reviewers" if caller == "team" else "general",
    )
    open_scope = partial(
        open_resolved_scope_session_context,
        agent_name="general",
        scope=scope,
        session_id="approval-session",
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
        create_session_if_missing=True,
    )
    with open_scope() as scope_context:
        assert scope_context is not None
        if caller == "team":
            members = materialize_exact_team_members(
                ["general", "second"],
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
                session_id="approval-session",
                supports_native_tool_approval=True,
            )
            actor = build_materialized_team_instance(
                requested_agent_names=["general", "second"],
                agents=members.agents,
                mode=TeamMode.COORDINATE,
                config=config,
                runtime_paths=paths,
                scope_context=scope_context,
                model_name="default",
                configured_team_name="reviewers",
                execution_identity=identity,
            )
            owners = toolkit_owners_for_agents(members.agents)
        else:
            actor = create_agent(
                "general",
                config,
                paths,
                identity,
                session_id="approval-session",
                history_storage=scope_context.storage,
                supports_native_tool_approval=True,
            )
            owners = toolkit_owners_for_agents([actor])
        try:
            paused = await actor.arun("Add 2 and 3", session_id="approval-session", user_id=context.requester_id)
            assert paused.status == RunStatus.paused
            captured = paused_attempt_from_response(
                paused,
                fallback_session_id="approval-session",
                fallback_run_id=paused.run_id,
                toolkit_owners=owners,
            )
            assert captured is not None
            if caller == "team":
                captured = _attach_team_pause_presentation(
                    captured,
                    response=paused,
                    config_names=["general", "second"],
                    display_names=["General", "Second"],
                    show_tool_calls=True,
                )
        finally:
            if caller == "team":
                close_team_runtime_state_dbs(
                    agents=members.agents,
                    team_db=actor.db,
                    shared_scope_storage=scope_context.storage,
                )
            else:
                close_agent_runtime_state_dbs(actor, shared_scope_storage=scope_context.storage)
    calls = (ApprovalCall("approved", "add", "general", 2**62, toolkit_name="calculator"),)

    async def resume() -> CompletedApprovalRun | PausedAttempt:
        if caller == "team":
            return await continue_paused_team_run(
                member_names=("general", "second"),
                mode=TeamMode.COORDINATE,
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
                session_id="approval-session",
                run_id=paused.run_id,
                user_id=context.requester_id,
                configured_team_name="reviewers",
                model_name="default",
                decisions={"approved": True},
                denial_reasons={"approved": None},
                refresh_scheduler=None,
                approval_calls=calls,
                history_scope=scope,
                prior_presentation_state=captured.response_presentation_state,
                prior_response_text=captured.response_text,
                prior_tool_trace=captured.tool_trace,
                progress=None,
            )
        runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
        execution = replace(runner._approval_execution, config=lambda: config, runtime_paths=paths)
        return await execution.continue_run(
            ApprovalContinuation(
                approval_id="sample-approval",
                run_id=paused.run_id,
                session_id="approval-session",
                entity_kind="agent",
                entity_name="general",
                room_id=context.room_id,
                thread_id=context.thread_id,
                requester_id=context.requester_id,
                response_event_id="$waiting",
                sources=ResponseSources(("$source",), ("$source",)),
                state="claimed",
                calls=calls,
                request_body="Add 2 and 3",
            ),
            execution_identity=identity,
            tool_dispatch=ToolDispatchContext(execution_identity=identity),
            decisions={"approved": True},
            denial_reasons={"approved": None},
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
        )

    assert get_helper_usage_owner() is None
    if outcome == "completed":
        assert isinstance(await resume(), CompletedApprovalRun)
    else:
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
            await resume()
    assert get_helper_usage_owner() is None
    assert helper_results == ["Sample helper output."]
    with open_scope() as scope_context:
        assert scope_context is not None
        storage = scope_context.storage
        assert isinstance(storage, SqliteDb)
        with storage.db_engine.connect() as connection:
            rows = connection.exec_driver_sql(
                f"SELECT session_id, usage_data FROM {quote_identifier(storage.session_table_name + '_usage')}",  # noqa: S608
            ).all()
    helpers = [
        (session_id, json.loads(value))
        for session_id, value in rows
        if json.loads(value).get("kind") == "dynamic_workflow"
    ]
    assert len(helpers) == 1
    assert helpers[0][0] == "approval-session"
    assert helpers[0][1]["user_id"] == "@user:localhost"
    assert helpers[0][1]["metrics"]["total_tokens"] == 22
    assert "Sample helper output" not in json.dumps(helpers)
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 22
    assert sum(row.run_count for row in report.model_breakdown) == 0


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
