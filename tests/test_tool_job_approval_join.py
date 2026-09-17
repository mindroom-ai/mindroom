"""Native approved tools retain the response owner through automatic job joining."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.team import Team
from agno.tools.function import Function

from mindroom.ai import _AgentRunContext, _PreparedAgentRun
from mindroom.approval_execution import _continue_persisted_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import ApprovalContinuation
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope, PreparedHistoryState
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, ResponseTurnContext
from mindroom.streaming import StreamingPresentation
from mindroom.team_exact_members import ResolvedExactTeamMembers
from mindroom.teams import (
    TeamMode,
    _PreparedMaterializedTeamExecution,
    _TeamStreamPresentation,
    continue_paused_team_run,
    team_response,
    team_response_stream,
)
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.completion import background_wait_notice
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.events import BackgroundWaitChunk, ToolTraceEntry
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from tests.conftest import make_turn_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("wait_timeout", [0, 0.001])
@pytest.mark.parametrize("human_release", [False, True])
async def test_native_approval_joins_before_final_response(  # noqa: PLR0915
    tmp_path: Path,
    team: bool,
    wait_timeout: float,
    human_release: bool,
) -> None:
    """Approved work stays owned and visibly waiting until a result or human release."""
    config = Config(background_tool_jobs=True, agents={"leader": AgentConfig(display_name="Leader")})
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    started, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    signal = HumanMessageSignal()
    notices: list[str] = []
    executions = 0

    async def slow_tool() -> str:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return "retained actual result"

    async def notice(presentation: StreamingPresentation) -> None:
        notices.append(presentation.response_text)
        waiting.set()

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("slow_tool", "approved-call", wait_timeout=wait_timeout)]),
            ModelResponse(content="Independent work done."),
        ],
    )
    install_tool_job_execution(model)
    function = Function.from_callable(slow_tool)
    function.requires_confirmation = True
    db_file = str(tmp_path / "approval.db")
    storage = SqliteDb(db_file=db_file)
    tools = [function, JobTools(paths, owner)]
    actor = (
        Team(id="leader", model=model, members=[], tools=tools, db=storage, telemetry=False)
        if team
        else Agent(id="leader", model=model, tools=tools, db=storage, telemetry=False)
    )
    pending = None
    try:
        with tool_runtime_context(context), human_message_signal_context(signal), background_wait_notice(notice):
            paused = await actor.arun(
                "Start the approved work",
                session_id=context.session_id,
                user_id=owner.requester_id,
            )
            assert paused.status is RunStatus.paused
            assert executions == 0
            assert paused.requirements
            if not team:
                paused.requirements[0].confirm()
            continuation = ApprovalContinuation(
                approval_id="approval",
                run_id=paused.run_id,
                session_id=context.session_id,
                entity_kind="team" if team else "agent",
                entity_name="leader",
                room_id=owner.room_id,
                thread_id=owner.resolved_thread_id,
                requester_id=owner.requester_id,
                response_event_id="$response",
                sources=ResponseSources(("$source",), ("$source",)),
                calls=(),
                state="claimed",
            )

            @owned_tool_execution
            async def resume_agent() -> str:
                set_consumption_storage(lambda: SqliteDb(db_file=db_file))
                assert isinstance(actor, Agent)
                scope = ScopeSessionContext(
                    HistoryScope(kind="agent", scope_id="leader"),
                    storage,
                    storage.get_session(context.session_id, session_type=SessionType.AGENT),
                    session_id=context.session_id,
                    storage_factory=lambda: SqliteDb(db_file=db_file),
                )

                async def prepare(ctx: ResponseTurnContext, **kwargs: object) -> _AgentRunContext:
                    prompt = str(kwargs["prompt"])
                    prepared = _PreparedAgentRun(
                        agent=actor,
                        messages=(Message(role="user", content=prompt),),
                        unseen_event_ids=[],
                        prepared_history=PreparedHistoryState(),
                        runtime_model_name="default",
                    )
                    return _AgentRunContext(
                        turn=ctx,
                        session_id=context.session_id,
                        prompt=prompt,
                        model_prompt=None,
                        prepared_run=prepared,
                        run_input=prepared.run_input,
                        metadata=ctx.matrix_run_metadata,
                    )

                with (
                    patch("mindroom.ai.open_resolved_scope_session_context", return_value=nullcontext(scope)),
                    patch("mindroom.ai._prepare_agent_run_context", new=prepare),
                ):
                    response = await _continue_persisted_agent(
                        actor,
                        continuation,
                        paused,
                        paused.requirements,
                        config=config,
                        runtime_paths=paths,
                        execution_identity=owner,
                        refresh_scheduler=None,
                        decisions={},
                        denial_reasons={},
                        knowledge=None,
                        tool_trace_collector=[],
                        run_id_callback=None,
                        tool_dispatch=LiveToolDispatchContext.from_runtime_context(context),
                    )
                assert isinstance(response, CompletedApprovalRun)
                return response.response_text

            async def resume_team() -> str:
                session = storage.get_session(context.session_id, session_type=SessionType.TEAM)
                scope = ScopeSessionContext(
                    HistoryScope(kind="team", scope_id="leader"),
                    storage,
                    session,
                    session_id=context.session_id,
                    storage_factory=lambda: SqliteDb(db_file=db_file),
                )
                with (
                    patch("mindroom.teams.open_resolved_scope_session_context", return_value=nullcontext(scope)),
                    patch(
                        "mindroom.teams.materialize_exact_team_members",
                        return_value=ResolvedExactTeamMembers([], [], [], set(), []),
                    ),
                    patch("mindroom.teams.build_materialized_team_instance", return_value=actor),
                    patch("mindroom.teams.validate_approval_tool_owners"),
                ):
                    response = await continue_paused_team_run(
                        member_names=(),
                        mode=TeamMode.COORDINATE,
                        config=config,
                        runtime_paths=paths,
                        execution_identity=owner,
                        session_id=context.session_id,
                        run_id=paused.run_id,
                        user_id=owner.requester_id,
                        configured_team_name="leader",
                        model_name="default",
                        decisions={"approved-call": True},
                        denial_reasons={"approved-call": None},
                        refresh_scheduler=None,
                        history_scope=scope.scope,
                        prior_presentation_state=_TeamStreamPresentation.new([], [], show_tool_calls=True).to_state(),
                    )
                return response.response_text

            pending = asyncio.create_task(resume_team() if team else resume_agent())
            start_waiter = asyncio.create_task(started.wait())
            try:
                await asyncio.wait({pending, start_waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
                if pending.done():
                    pending.result()
                await asyncio.wait_for(start_waiter, 2)
            finally:
                start_waiter.cancel()
                await asyncio.gather(start_waiter, return_exceptions=True)
            notice_waiter = asyncio.create_task(waiting.wait())
            try:
                await asyncio.wait({pending, notice_waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            finally:
                notice_waiter.cancel()
                await asyncio.gather(notice_waiter, return_exceptions=True)
            assert not pending.done(), "approval returned a final response while accepted work was still running"
            assert waiting.is_set(), "approval join did not publish visible wait progress"
            assert "Independent work done." in notices[-1]
            assert "Waiting for background work" in notices[-1]
            jobs = await runtime.list_jobs(owner=owner, depth=0)
            assert len(jobs) == 1
            job_id = jobs[0].job_id
            if human_release:
                signal.notify()
                text = await asyncio.wait_for(pending, 2)
                assert "Independent work done." in text
                assert (await runtime.lookup(job_id, owner=owner, depth=0)).status == "running"
                release.set()
            else:
                model.responses.extend(
                    [
                        ModelResponse(
                            tool_calls=[_call("job", "retrieve", action="wait", job_id=job_id, wait_timeout=0)],
                        ),
                        ModelResponse(content="Final result received."),
                    ],
                )
                release.set()
                text = await asyncio.wait_for(pending, 2)
                assert "Final result received." in text
                assert await runtime.pending_outcomes() == []
            assert executions == 1
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(("streaming", "recovered"), [(False, False), (True, False), (False, True)])
async def test_ordinary_team_autojoin_persists_exact_result_receipt(  # noqa: C901, PLR0915 - One native job lifecycle across delivery modes.
    tmp_path: Path,
    streaming: bool,
    recovered: bool,
) -> None:
    """Both ordinary team entry points acknowledge only their saved native result receipt."""
    config = Config(background_tool_jobs=True, agents={"leader": AgentConfig(display_name="Leader")})
    paths = _runtime_paths(tmp_path)
    identities = entity_ids(config, paths)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    release, waiting = asyncio.Event(), asyncio.Event()
    notices: list[StreamingPresentation] = []
    calls = 0

    async def slow_tool() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "actual result"

    async def report_wait(presentation: StreamingPresentation) -> None:
        notices.append(presentation)
        waiting.set()

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("slow_tool", "ordinary-call", wait_timeout=0)]),
            ModelResponse(content="Independent work done."),
        ],
    )
    install_tool_job_execution(model)
    storage_file = str(tmp_path / "team.db")
    storage = SqliteDb(db_file=storage_file)
    member = Agent(id="leader", name="Leader", model=model, telemetry=False)
    team = Team(
        id="leader",
        model=model,
        members=[member],
        tools=[slow_tool, JobTools(paths, owner)],
        db=storage,
        telemetry=False,
    )
    members = ResolvedExactTeamMembers(["leader"], [member], ["Leader"], {"leader"}, [])
    scope = ScopeSessionContext(
        HistoryScope(kind="team", scope_id="leader"),
        storage,
        None,
        session_id=context.session_id,
        storage_factory=lambda: SqliteDb(db_file=storage_file),
    )
    orchestrator = MagicMock(config=config, runtime_paths=paths)
    ctx = make_turn_context(
        entity_label="leader",
        session_id=context.session_id,
        room_id=owner.room_id,
        thread_id=owner.resolved_thread_id,
        requester_id=owner.requester_id,
    )
    prefix = StreamingPresentation(
        "Earlier team answer.\n\n🔧 `original_tool` [1]",
        tool_trace=(ToolTraceEntry("tool_call_completed", "original_tool", result_preview="earlier result"),),
    )
    if recovered:
        ctx = replace(ctx, initial_presentation=prefix)

    async def prepare(*_args: object, **kwargs: object) -> _PreparedMaterializedTeamExecution:
        return _PreparedMaterializedTeamExecution(
            messages=(Message(role="user", content=str(kwargs["message"])),),
            run_metadata={},
            unseen_event_ids=[],
            prepared_history=PreparedHistoryState(),
            runtime_model_name="default",
        )

    async def run() -> str:
        recorder = TurnRecorder(user_message="Start")
        if not streaming:
            return await team_response(
                agent_names=["leader"],
                mode=TeamMode.COORDINATE,
                message="Start",
                orchestrator=orchestrator,
                execution_identity=owner,
                ctx=ctx,
                user_id=owner.requester_id,
                turn_recorder=recorder,
            )
        chunks = []
        async for chunk in team_response_stream(
            agent_ids=[identities["leader"]],
            message="Start",
            orchestrator=orchestrator,
            execution_identity=owner,
            ctx=ctx,
            user_id=owner.requester_id,
            turn_recorder=recorder,
        ):
            if isinstance(chunk, BackgroundWaitChunk):
                waiting.set()
            else:
                chunks.append(str(chunk))
        return "".join(chunks)

    pending = None
    try:
        with (
            tool_runtime_context(context),
            background_wait_notice(report_wait),
            patch("mindroom.teams._materialize_team_members", return_value=members),
            patch("mindroom.teams.build_materialized_team_instance", return_value=team),
            patch("mindroom.teams.open_bound_scope_session_context", return_value=nullcontext(scope)),
            patch("mindroom.teams.prepare_materialized_team_execution", new=prepare),
        ):
            pending = asyncio.create_task(run())
            notice_waiter = asyncio.create_task(waiting.wait())
            try:
                await asyncio.wait({pending, notice_waiter}, timeout=2, return_when=asyncio.FIRST_COMPLETED)
                if pending.done():
                    pending.result()
                assert waiting.is_set()
                if recovered:
                    assert notices[-1].response_text.startswith(prefix.response_text + "\n\n")
                    assert notices[-1].response_text.count(prefix.response_text) == 1
                    assert notices[-1].tool_trace == prefix.tool_trace
            finally:
                notice_waiter.cancel()
                await asyncio.gather(notice_waiter, return_exceptions=True)
            jobs = await runtime.list_jobs(owner=owner, depth=0)
            assert len(jobs) == 1
            model.responses.extend(
                [
                    ModelResponse(
                        tool_calls=[_call("job", "retrieve", action="wait", job_id=jobs[0].job_id, wait_timeout=0)],
                    ),
                    ModelResponse(content="Final result received."),
                ],
            )
            release.set()
            answer = await asyncio.wait_for(pending, 2)
            assert "Final result received." in answer
            if recovered:
                assert answer.startswith(prefix.response_text + "\n\n")
                assert answer.count(prefix.response_text) == 1
            assert calls == 1
            assert await runtime.pending_outcomes() == []
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        register_background_runtime(paths, None)
        await runtime.shutdown()
