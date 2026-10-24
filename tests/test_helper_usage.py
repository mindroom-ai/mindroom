"""Paid helper calls retain content-free usage in their caller's conversation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from openai.types import CompletionUsage

from mindroom import agents
from mindroom.agent_cli.approval import CliApprovalCall
from mindroom.agents import create_agent
from mindroom.ai import ai_response
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.models import ModelConfig, ToolConfigEntry
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.custom_tools.dynamic_workflow import _aexecute_participant, _arun_agent, _WorkflowRun
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.helper_usage import HelperUsageOwner, get_helper_usage_owner, helper_usage_context, record_helper_usage
from mindroom.history.session_context import (
    open_bound_scope_session_context,
    open_resolved_scope_session_context,
)
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.hooks import HookRegistry
from mindroom.llm import usage as llm_usage
from mindroom.llm.errors import ProviderError
from mindroom.llm.provider import UsageReported
from mindroom.llm.runs import RunRecord
from mindroom.llm.usage import TokenCounts
from mindroom.memory._mem0_usage import counted_mem0_usage, install_mem0_usage_capture, mem0_usage_run
from mindroom.memory.auto_flush import _extract_memory_summary
from mindroom.response_turn import CompletedApprovalRun, PausedAnswer, PausedAttempt, ResponsePausedForApproval
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import session_state_snapshot
from mindroom.teams.envelope import team_response
from mindroom.teams.resume import continue_paused_team_run
from mindroom.teams.spec import TeamMode
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    ToolDispatchContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from mindroom.usage.stats import collect_admin_usage, collect_self_usage
from mindroom.usage.storage import quote_identifier
from tests.ai_user_id_helpers import team_orchestrator
from tests.approval_continuation_helpers import approval_continuation
from tests.conftest import make_turn_context, unwrap_extracted_collaborator
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.fakes.scripted_model import usage as owned_usage
from tests.history_helpers import (
    _completed_run,
    _forced_compaction_context,
    _hook_runtime_context,
    _make_config,
    _session,
)
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.session_storage.store import ConversationStore


def _usage_rows(storage: ConversationStore) -> list[tuple[str, str]]:
    """Return every stored usage row as ``(session_id, usage_data)``."""
    with storage.transaction(write=False) as connection:
        return connection.execute(
            f"SELECT session_id, usage_data FROM {quote_identifier(storage.names.usage)}",  # noqa: S608
        ).fetchall()


def _resumed_turn(outcome: str) -> list[ScriptStep]:
    """Return the scripted turn a resumed run requests after its approved call: an answer, or a failed request."""
    if outcome != "error":
        return answer("Finished.")

    async def fail() -> None:
        msg = "Resumed provider failed"
        raise ProviderError(msg, provider="Synthetic")

    return [Await(fail)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    [
        "agent",
        "private_agent",
        "team",
        "cli_agent",
        "private_cli_agent",
    ],
)
@pytest.mark.parametrize("outcome", ["completed", "error", "cancel"])
async def test_approval_resumed_helpers_keep_caller_usage_and_reset_context(  # noqa: C901, PLR0915 - shared native/CLI ownership outcomes
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    outcome: str,
) -> None:
    """Approval resume must retain paid helper output in its real caller store even if the resumed run fails."""
    config, paths = _make_config(tmp_path)
    config.agents["general"] = config.agents.pop("test_agent")
    is_cli = caller in {"cli_agent", "private_cli_agent"}
    config.agents["general"].tools = [ToolConfigEntry(name="calculator")]
    if is_cli:
        config.agents["general"].tools.append(ToolConfigEntry(name="shell"))
        config.administrators = ["@user:localhost"]
    config.memory.backend = "none"
    config.models["helper"] = ModelConfig(provider="openai", id="helper-model")
    config.agents["helper"] = AgentConfig(display_name="Helper", model="helper")
    config.tool_approval = ToolApprovalConfig.model_validate(
        {"default": "auto_approve", "rules": [{"match": "add", "action": "require_approval"}]},
    )
    config.agents["general"].private = (
        AgentPrivateConfig(per="user", root="mind_data") if caller in {"private_agent", "private_cli_agent"} else None
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
    models = {
        "default": ScriptedModel([calls(tool_call("add", "approved", a=2, b=3)), _resumed_turn(outcome)]),
        "helper": ScriptedModel([answer("Sample helper output.", owned_usage(20, 2))]),
    }
    leader = ScriptedModel(
        [
            calls(tool_call("delegate_task_to_member", "delegate", member_id="general", task="Add 2 and 3")),
            _resumed_turn(outcome),
        ],
    )
    monkeypatch.setattr(agents, "_build_agent_model_provider", lambda _config, _paths, name, _identity: models[name])
    monkeypatch.setattr("mindroom.teams.members.build_model_provider", lambda *_args, **_kwargs: leader)
    original_add = CalculatorTools.add
    helper_results: list[object] = []

    async def add(self: CalculatorTools, a: float, b: float) -> str:
        helper = create_agent("helper", config, paths, identity)
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
    )
    turn = make_turn_context(
        "reviewers" if caller == "team" else "general",
        session_id="approval-session",
        room_id=context.room_id,
        thread_id=context.thread_id,
        requester_id=context.requester_id,
    )
    orchestrator = team_orchestrator(config, paths)
    orchestrator.agent_bots = {name: SimpleNamespace(running=True) for name in ("general", "second")}

    async def pause() -> None:
        with tool_runtime_context(context):
            if caller == "team":
                await team_response(
                    agent_names=["general", "second"],
                    mode=TeamMode.COORDINATE,
                    message="Add 2 and 3",
                    orchestrator=orchestrator,
                    execution_identity=identity,
                    ctx=turn,
                    user_id=context.requester_id,
                    configured_team_name="reviewers",
                    turn_recorder=TurnRecorder(user_message="Add 2 and 3"),
                )
            else:
                await ai_response(
                    turn,
                    prompt="Add 2 and 3",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=identity,
                    supports_native_tool_approval=True,
                )

    with pytest.raises(ResponsePausedForApproval) as raised:
        await pause()
    paused = raised.value.paused
    if is_cli:
        # Reconstruct an interrupted minimal response using its exact saved approval.
        with open_scope() as scope_context:
            assert scope_context is not None
            stored = scope_context.storage.get_run(paused.run_id)
            assert isinstance(stored, RunRecord)
            stored.metadata = {**(stored.metadata or {}), "agent_mode": "minimal"}
            assert scope_context.session is not None
            scope_context.storage.save_runs(scope_context.session, [stored])
    calls_ = (
        ApprovalCall(
            "approved",
            "add",
            "general",
            2**62,
            toolkit_name="calculator",
            arguments_digest=approval_arguments_digest({"a": 2, "b": 3}),
        ),
    )
    cli_call = None
    if is_cli:
        cli_call = CliApprovalCall(
            toolkit="calculator",
            function="add",
            call_id="approved",
            parent_bash_call_id="interrupted-bash",
            arguments={"a": 2, "b": 3},
            requirements=tuple(paused.requirements),
            delegation_depth=0,
        ).to_dict()

        async def recovered_response(*_args: object, **_kwargs: object) -> CompletedApprovalRun:
            # The approved helper has already run; the old Bash is never replayed.
            if outcome == "error":
                msg = "Resumed provider failed"
                raise RuntimeError(msg)
            return CompletedApprovalRun(response_text="Finished.", metadata_content={})

        monkeypatch.setattr("mindroom.approval.execution._stream_continuation_turn", recovered_response)

    async def resume() -> CompletedApprovalRun | PausedAttempt:
        if caller == "team":
            with tool_runtime_context(context):
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
                    approval_calls=calls_,
                    history_scope=scope,
                    prior_presentation_state=paused.response_presentation_state,
                    prior_tool_trace=paused.tool_trace,
                    progress=None,
                )
        runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
        execution = replace(runner._approval_execution, config=lambda: config, runtime_paths=paths)
        return await execution.continue_run(
            approval_continuation(
                approval_id="sample-approval",
                run_id=paused.run_id,
                session_id="approval-session",
                room_id=context.room_id,
                thread_id=context.thread_id,
                requester_id=context.requester_id,
                state="claimed",
                calls=calls_,
                request_body="Add 2 and 3",
                cli_call=cli_call,
            ),
            paused_answer=PausedAnswer(),
            execution_identity=identity,
            tool_dispatch=(
                LiveToolDispatchContext.from_runtime_context(context)
                if is_cli
                else ToolDispatchContext(execution_identity=identity)
            ),
            decisions={"approved": True},
            denial_reasons={"approved": None},
            tool_trace_collector=[],
            typing_log_context={},
            progress=None,
        )

    assert helper_results == []
    assert get_helper_usage_owner() is None
    if outcome == "completed":
        assert isinstance(await resume(), CompletedApprovalRun)
    else:
        # A native resume raises the provider's failure; the CLI follow-up turn raises its reply's error.
        failure = RuntimeError if is_cli else ProviderError
        with pytest.raises(asyncio.CancelledError if outcome == "cancel" else failure):
            await resume()
    assert get_helper_usage_owner() is None
    assert helper_results == ["Sample helper output."]
    with open_scope() as scope_context:
        assert scope_context is not None
        rows = _usage_rows(scope_context.storage)
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
async def test_mem0_extraction_calls_are_complete_request_rows(tmp_path: Path) -> None:
    """Each Mem0 extraction call is one request row, so the conversation's request detail stays complete."""
    run = _completed_run("ordinary")
    run.user_id = "@user:localhost"
    run.model = "reply-model"
    run.model_provider = "test-provider"
    run.metrics = llm_usage.RunMetrics(
        tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10),
        details={
            "model": [
                llm_usage.ModelMetrics(
                    tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10),
                    id="reply-model",
                    provider="test-provider",
                ),
            ],
        },
    )
    for message in run.messages or []:
        if message.role == "assistant":
            message.metrics = llm_usage.MessageMetrics(
                tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10),
            )
            message.created_at = 1_790_000_000
            message.provider_data = {"mindroom_model": {"id": "reply-model", "provider": "test-provider"}}
    session = _session("session", runs=[run])
    config, paths, _storage, _scope, _context = _forced_compaction_context(tmp_path, session=session)
    usage = CompletionUsage(prompt_tokens=120, completion_tokens=30, total_tokens=150)
    create = MagicMock(return_value=SimpleNamespace(usage=usage))
    llm = SimpleNamespace(
        config=SimpleNamespace(model="gpt-6-luna"),
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
    )
    install_mem0_usage_capture(llm, "openai")
    with counted_mem0_usage() as calls:
        llm.client.chat.completions.create()
        llm.client.chat.completions.create()
    extraction = mem0_usage_run(calls)
    assert extraction is not None
    await record_helper_usage(
        extraction,
        owner=HelperUsageOwner(
            storage_factory=partial(create_session_storage, "test_agent", config, paths, execution_identity=None),
            session_id="session",
        ),
        invocation_id=extraction.run_id,
        kind="memory_extraction",
        requester_id="@user:localhost",
    )

    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 310
    assert report.request_breakdown is not None
    assert [(row.kind, row.model_provider, row.model, row.totals.total_tokens) for row in report.request_breakdown] == [
        ("run", "test-provider", "reply-model", 10),
        ("memory_extraction", "OpenAI", "gpt-6-luna", 150),
        ("memory_extraction", "OpenAI", "gpt-6-luna", 150),
    ]
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 0


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
    run.metrics = llm_usage.RunMetrics(tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10))
    session = _session("session", runs=[run])
    config, paths, storage, _scope, context = _forced_compaction_context(tmp_path, session=session)
    identity = build_execution_identity_from_runtime_context(context)
    model = ScriptedModel(
        [answer(content, owned_usage(100, 10)), answer("Remember another preference.", owned_usage(50, 5))],
    )
    monkeypatch.setattr("mindroom.memory.auto_flush.build_model_provider", lambda *_args: model)
    for _ in range(2):
        await _extract_memory_summary(
            config=config,
            runtime_paths=paths,
            agent_name="test_agent",
            session_id="session",
            lines=["Synthetic conversation excerpt."],
            existing_context="",
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
    helpers = [
        json.loads(value)
        for _session_id, value in _usage_rows(storage)
        if json.loads(value).get("kind") == "memory_auto_flush"
    ]
    assert len(helpers) == 2
    assert len({row["run_id"] for row in helpers}) == 2
    assert "Synthetic conversation" not in json.dumps(helpers)
    assert "Remember" not in json.dumps(helpers)
    storage.save_session_state(session, session_state_snapshot(session.session_state))
    assert collect_admin_usage(config=config, runtime_paths=paths).totals == report.totals


@pytest.mark.asyncio
@pytest.mark.parametrize("caller", ["agent", "private_agent", "configured_team", "ad_hoc_team"])
async def test_workflow_first_turn_uses_actual_caller_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
) -> None:
    """Synthetic room-agent participant IDs must not replace private, configured or ad hoc caller ownership."""
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
            member_names=["test_agent", "second"],
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
    model = ScriptedModel([answer("Sample output.", owned_usage(20, 2))])
    monkeypatch.setattr("mindroom.agents._build_agent_model_provider", lambda *_args: model)
    with open_scope() as scope_context, helper_usage_context(scope_context):
        assert scope_context is not None
        storage = scope_context.storage
        result = await _aexecute_participant(
            context,
            {"id": "writer", "kind": "room_agent", "agent": "test_agent"},
            "Sample task.",
            _WorkflowRun("sample-workflow"),
        )
        assert result.content == "Sample output."
        loaded = storage.load_session(context.session_id, "team" if caller.endswith("team") else "agent")
        assert loaded is not None, "First-turn helper usage needs its actual caller session row"
        assert not loaded.runs
        rows = _usage_rows(storage)
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
@pytest.mark.parametrize("ending", ["completed", "error", "cancelled"])
async def test_workflow_participant_usage_counts_however_its_run_ends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    """A room-agent participant's paid requests count once, also when its run fails or is stopped mid-request."""
    config, paths, _storage, scope, context = _forced_compaction_context(tmp_path, session=_session("session"))
    requested = asyncio.Event()

    async def interrupt() -> None:
        requested.set()
        if ending == "error":
            msg = "provider auth failed"
            raise ProviderError(msg, provider="Synthetic")
        await asyncio.Event().wait()

    turn = (
        answer("Sample output.", owned_usage(30, 3))
        if ending == "completed"
        else [UsageReported(owned_usage(30, 3), final=ending == "error"), Await(interrupt)]
    )
    monkeypatch.setattr("mindroom.agents._build_agent_model_provider", lambda *_args: ScriptedModel([turn]))
    open_scope = partial(
        open_resolved_scope_session_context,
        agent_name="test_agent",
        scope=scope,
        session_id="session",
        config=config,
        runtime_paths=paths,
        execution_identity=build_execution_identity_from_runtime_context(context),
    )
    with open_scope() as scope_context, helper_usage_context(scope_context):
        participant = asyncio.create_task(
            _aexecute_participant(
                context,
                {"id": "writer", "kind": "room_agent", "agent": "test_agent"},
                "Sample task.",
                _WorkflowRun("sample-workflow"),
            ),
        )
        if ending == "completed":
            assert (await participant).content == "Sample output."
        elif ending == "error":
            with pytest.raises(ProviderError, match="provider auth failed"):
                await participant
        else:
            await requested.wait()
            participant.cancel()
            with pytest.raises(asyncio.CancelledError):
                await participant
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 33
    assert sum(row.run_count for row in report.model_breakdown) == 0
    assert report.user_breakdown[0].user_id == "@user:localhost"
    assert report.request_breakdown is not None
    assert [row.totals.total_tokens for row in report.request_breakdown] == [33]


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
        context.storage.update_metadata(_session("session"), lambda _metadata: {"marker": "concurrent session"})
        await record_helper_usage(
            RunRecord(
                run_id="paid",
                metrics=llm_usage.RunMetrics(tokens=TokenCounts(input_tokens=10, output_tokens=1, total_tokens=11)),
            ),
            owner=owner,
            invocation_id="paid",
            kind="dynamic_workflow",
            requester_id=None,
        )
        loaded = context.storage.load_session("session", "agent")
        assert loaded is not None
        assert loaded.metadata == {"marker": "concurrent session"}
        raise asyncio.CancelledError
    assert get_helper_usage_owner() is None
    assert collect_admin_usage(config=config, runtime_paths=paths).totals.total_tokens == 11
    with open_scope() as context:
        assert context is not None
        sessions = quote_identifier(context.storage.names.sessions)
        with context.storage.transaction(write=True) as connection:
            connection.execute(f"DELETE FROM {sessions} WHERE session_id = ?", ("session",))  # noqa: S608
        assert _usage_rows(context.storage) == []


@pytest.mark.asyncio
async def test_returned_helper_usage_write_finishes_after_cancellation(tmp_path: Path) -> None:
    """Once counters are returned, cancellation must wait for the accepted SQLite write."""
    config, paths, storage, _scope, _context = _forced_compaction_context(tmp_path, session=_session("session"))
    started = threading.Event()

    def create_storage() -> ConversationStore:
        started.set()
        return create_session_storage("test_agent", config, paths, execution_identity=None)

    with closing(sqlite3.connect(storage.path, isolation_level=None)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        task = asyncio.create_task(
            record_helper_usage(
                RunRecord(
                    run_id="paid-attempt",
                    metrics=llm_usage.RunMetrics(tokens=TokenCounts(input_tokens=20, output_tokens=2, total_tokens=22)),
                ),
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
    assert collect_admin_usage(config=config, runtime_paths=paths).totals.total_tokens == 22
