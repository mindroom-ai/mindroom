"""Native approved tools retain the response owner through automatic job joining."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
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

from mindroom import response_turn as response_turn_module
from mindroom.ai import _AgentRunContext, _PreparedAgentRun, ai_response
from mindroom.approval_execution import _continue_persisted_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope, PreparedHistoryState
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, ResponsePausedForApproval, ResponseTurnContext
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
from mindroom.tool_jobs.completion import ReplyBoundaryReport, _JobJoin, reply_boundary_report
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.held_replies import _WAITING_NOTICE
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.events import StructuredStreamChunk, ToolTraceEntry
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from tests.conftest import make_turn_context
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.identity_helpers import entity_ids
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    assembled_function,
    lookup,
    pending_outcomes,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path


async def _wait_for_progress(pending: asyncio.Task[str], progress: asyncio.Event) -> None:
    """Wait for real progress, surfacing a premature response failure immediately."""
    waiter = asyncio.create_task(progress.wait())
    try:
        await asyncio.wait({pending, waiter}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if pending.done():
            pending.result()
        assert progress.is_set(), "response did not reach expected progress"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("wait_timeout", [0, 0.001])
async def test_native_approval_leaves_running_work_for_its_message_to_hold(  # noqa: PLR0915
    tmp_path: Path,
    team: bool,
    wait_timeout: float,
) -> None:
    """A resumed approval finishes while its approved work runs on, and its message holds that work."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader", tools=["calculator"])},
    )
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    started, release = asyncio.Event(), asyncio.Event()
    report = ReplyBoundaryReport()
    executions = 0

    async def slow_tool() -> str:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return "retained actual result"

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("add", "approved-call", wait_timeout=wait_timeout)]),
            ModelResponse(content="Independent work done."),
        ],
    )
    install_tool_job_execution(model)
    function = Function.from_callable(slow_tool)
    function.name = "add"
    function.owning_toolkit = "calculator"
    function.requires_confirmation = True
    db_file = str(tmp_path / "approval.db")
    storage = SqliteDb(db_file=db_file)
    tools = [function, JobTools(paths, owner)]
    member_model = model
    member = Agent(id="leader", name="Leader", model=member_model, tools=tools, db=storage, telemetry=False)
    if team:
        member_model.responses[-1] = ModelResponse(content="Member waiting for its result.")
        model = DelegationModel(
            id="coordinator",
            responses=[
                ModelResponse(
                    tool_calls=[_call("delegate_task_to_member", "delegate", member_id="leader", task="Add")],
                ),
                ModelResponse(content="Independent work done."),
                ModelResponse(
                    tool_calls=[
                        _call("delegate_task_to_member", "retrieve-member", member_id="leader", task="Retrieve result"),
                    ],
                ),
                ModelResponse(content="Final result received."),
            ],
        )
        install_tool_job_execution(model)
        actor = Team(id="leader", model=model, members=[member], db=storage, telemetry=False)
    else:
        actor = member
    approval_calls = (ApprovalCall("approved-call", "add", "leader", 2**62, toolkit_name="calculator"),)
    pending = None
    try:
        with tool_runtime_context(context), reply_boundary_report(report):
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
                calls=approval_calls,
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
                        progress=None,
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
                        return_value=ResolvedExactTeamMembers(["leader"], [member], ["Leader"], {"leader"}, []),
                    ),
                    patch("mindroom.teams.build_materialized_team_instance", return_value=actor),
                ):
                    response = await continue_paused_team_run(
                        member_names=("leader",),
                        approval_calls=approval_calls,
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
                        prior_presentation_state=_TeamStreamPresentation.new(
                            ["leader"],
                            ["Leader"],
                            show_tool_calls=True,
                        ).to_state(),
                        progress=None,
                    )
                return response.response_text

            pending = asyncio.create_task(resume_team() if team else resume_agent())
            await _wait_for_progress(pending, started)
            text = await asyncio.wait_for(pending, 30)
            assert "Independent work done." in text
            jobs = await runtime.list_jobs(owner=owner, depth=0)
            assert len(jobs) == 1
            assert (await lookup(runtime, jobs[0].job_id, owner=owner, depth=0)).status == "running"
            assert report.boundary is not None
            assert report.boundary.notice == _WAITING_NOTICE
            assert report.boundary.key.participants == ("leader",)
            release.set()
            ready = await runtime.wait(jobs[0].job_id, owner=owner, depth=0)
            await runtime.release_wait(jobs[0].job_id, ready.claim)
            # Nothing retrieved the outcome; it waits for the turn continuing the held message.
            assert [job.job_id for job in pending_outcomes(runtime)] == [jobs[0].job_id]
            assert executions == 1
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("show_tool_calls", [False, True])
async def test_blocking_agent_join_preserves_prior_text_when_approval_pauses(  # noqa: PLR0915
    tmp_path: Path,
    show_tool_calls: bool,
) -> None:
    """A native approval after a job join retains the prose the reply published before the join."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    executions = 0

    async def slow_tool() -> str:
        return "actual result"

    async def approved_tool() -> str:
        nonlocal executions
        executions += 1
        return "approved action"

    original_join = response_turn_module.join_conversation_jobs

    async def join_once_ready(attempted: set[str], **kwargs: object) -> _JobJoin:
        jobs = await runtime.list_jobs(owner=owner, depth=0)
        if not attempted:
            # The join continues only with work that is ready, so let the job finish first.
            await wait_for_status(runtime, jobs[0].job_id, "completed")
            model.responses.extend(
                [
                    ModelResponse(tool_calls=[_call("job", "retrieve", action="wait", job_id=jobs[0].job_id)]),
                    ModelResponse(
                        content="Need approval to continue.",
                        tool_calls=[_call("approved_tool", "approval")],
                    ),
                ],
            )
        return await original_join(attempted, **kwargs)

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("slow_tool", "ordinary-call", wait_timeout=0)]),
            ModelResponse(content="Independent answer already shown."),
        ],
    )
    install_tool_job_execution(model)
    function = assembled_function(approved_tool)
    function.requires_confirmation = True
    db_file = str(tmp_path / "agent.db")
    storage = SqliteDb(db_file=db_file)
    actor = Agent(
        id="leader",
        model=model,
        tools=[assembled_function(slow_tool), function, JobTools(paths, owner)],
        db=storage,
        telemetry=False,
    )
    scope = ScopeSessionContext(
        HistoryScope(kind="agent", scope_id="leader"),
        storage,
        None,
        session_id=context.session_id,
        storage_factory=lambda: SqliteDb(db_file=db_file),
    )
    ctx = make_turn_context(
        entity_label="leader",
        session_id=context.session_id,
        room_id=owner.room_id,
        thread_id=owner.resolved_thread_id,
        requester_id=owner.requester_id,
    )

    async def prepare(turn: ResponseTurnContext, **kwargs: object) -> _AgentRunContext:
        prompt = str(kwargs["prompt"])
        prepared = _PreparedAgentRun(
            agent=actor,
            messages=(Message(role="user", content=prompt),),
            unseen_event_ids=[],
            prepared_history=PreparedHistoryState(),
            runtime_model_name="default",
        )
        return _AgentRunContext(
            turn=turn,
            session_id=context.session_id,
            prompt=prompt,
            model_prompt=None,
            prepared_run=prepared,
            run_input=prepared.run_input,
            metadata=turn.matrix_run_metadata,
        )

    @owned_tool_execution
    async def run() -> str:
        set_consumption_storage(lambda: SqliteDb(db_file=db_file))
        return await ai_response(
            ctx,
            prompt="Start",
            runtime_paths=paths,
            config=config,
            execution_identity=owner,
            show_tool_calls=show_tool_calls,
            supports_native_tool_approval=True,
        )

    pending = None
    try:
        with (
            tool_runtime_context(context),
            patch("mindroom.ai.open_resolved_scope_session_context", return_value=nullcontext(scope)),
            patch("mindroom.ai._prepare_agent_run_context", new=prepare),
            patch.object(response_turn_module, "join_conversation_jobs", new=join_once_ready),
        ):
            pending = asyncio.create_task(run())
            with pytest.raises(ResponsePausedForApproval) as raised:
                await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
            paused = raised.value.paused
            assert paused.response_text.count("Independent answer already shown.") == 1
            assert "Need approval to continue." in paused.response_text
            assert "Waiting for background work" not in paused.response_text
            assert [tool.tool_call_id for tool in paused.tools] == ["approval"]
            assert [entry.tool_name for entry in paused.tool_trace] == ["slow_tool", "job", "approved_tool"]
            assert paused.tool_trace[-1].type == "tool_call_started"
            assert executions == 0
            if show_tool_calls:
                assert "`approved_tool` [3]" in paused.response_text
            else:
                assert "🔧" not in paused.response_text
    finally:
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("repeat_join", [False, True])
async def test_ordinary_team_autojoin_persists_exact_result_receipt(  # noqa: C901, PLR0915 - One native job lifecycle across delivery modes.
    tmp_path: Path,
    streaming: bool,
    repeat_join: bool,
) -> None:
    """Both ordinary team entry points acknowledge only their saved native result receipt."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    identities = entity_ids(config, paths)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    releases = [asyncio.Event(), asyncio.Event()]
    calls = 0
    stage = 0

    async def slow_tool() -> str:
        nonlocal calls
        calls += 1
        await releases[calls - 1].wait()
        return "actual result"

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
        tools=[assembled_function(slow_tool), JobTools(paths, owner)],
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

    async def prepare(*_args: object, **kwargs: object) -> _PreparedMaterializedTeamExecution:
        return _PreparedMaterializedTeamExecution(
            messages=(Message(role="user", content=str(kwargs["message"])),),
            run_metadata={},
            unseen_event_ids=[],
            prepared_history=PreparedHistoryState(),
            runtime_model_name="default",
        )

    recorder = TurnRecorder(user_message="Start")
    final_trace: list[ToolTraceEntry] = []
    original_join = response_turn_module.join_conversation_jobs

    async def join_once_ready(attempted: set[str], **kwargs: object) -> _JobJoin:
        """Let the running job finish before the boundary, which continues only with ready work."""
        nonlocal stage
        running = [job for job in await runtime.list_jobs(owner=owner, depth=0) if job.status == "running"]
        if running:
            job_id = running[0].job_id
            if stage == 0:
                model.responses.extend(
                    [
                        ModelResponse(
                            tool_calls=[_call("job", "retrieve", action="wait", job_id=job_id, wait_timeout=0)],
                        ),
                        *(
                            [
                                ModelResponse(tool_calls=[_call("slow_tool", "second-call", wait_timeout=0)]),
                                ModelResponse(content="Second independent stage done."),
                            ]
                            if repeat_join
                            else [ModelResponse(content="Final result received.")]
                        ),
                    ],
                )
            else:
                model.responses.extend(
                    [
                        ModelResponse(tool_calls=[_call("job", "retrieve-second", action="wait", job_id=job_id)]),
                        ModelResponse(content="Final result received."),
                    ],
                )
            releases[stage].set()
            stage += 1
            await wait_for_status(runtime, job_id, "completed")
        return await original_join(attempted, **kwargs)

    async def run() -> str:
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
        rendered = ""
        async for chunk in team_response_stream(
            agent_ids=[identities["leader"]],
            message="Start",
            orchestrator=orchestrator,
            execution_identity=owner,
            ctx=ctx,
            user_id=owner.requester_id,
            turn_recorder=recorder,
        ):
            if isinstance(chunk, StructuredStreamChunk):
                rendered = chunk.content
                final_trace[:] = chunk.tool_trace or []
            else:
                rendered = str(chunk)
        return rendered

    pending = None
    try:
        with (
            tool_runtime_context(context),
            patch("mindroom.teams._materialize_team_members", return_value=members),
            patch("mindroom.teams.build_materialized_team_instance", return_value=team),
            patch("mindroom.teams.open_bound_scope_session_context", return_value=nullcontext(scope)),
            patch("mindroom.teams.prepare_materialized_team_execution", new=prepare),
            patch.object(response_turn_module, "join_conversation_jobs", new=join_once_ready),
        ):
            pending = asyncio.create_task(run())
            answer = await asyncio.wait_for(pending, JOB_TEST_TIMEOUT)
            assert "Final result received." in answer
            assert answer.count("**Team Response**") == 1
            assert answer.count("Independent work done.") == 1
            assert recorder.assistant_text.count("Independent work done.") == 1
            assert "Final result received." in recorder.assistant_text
            expected_tools = ["slow_tool", "job"] * (2 if repeat_join else 1)
            assert [entry.tool_name for entry in recorder.completed_tools] == expected_tools
            if repeat_join:
                assert answer.count("Second independent stage done.") == 1
                assert recorder.assistant_text.count("Second independent stage done.") == 1
            if streaming:
                assert [entry.tool_name for entry in final_trace] == expected_tools
            assert calls == (2 if repeat_join else 1)
            assert pending_outcomes(runtime) == []
    finally:
        for release in releases:
            release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await runtime.shutdown()
