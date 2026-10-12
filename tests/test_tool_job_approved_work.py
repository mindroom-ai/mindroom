"""A natively approved call keeps running as a background job after its approval resumes the reply."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from typing import TYPE_CHECKING
from unittest.mock import patch

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
from mindroom.approval.execution import _continue_persisted_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import ApprovalCall, ApprovalContinuation, approval_arguments_digest
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope, PreparedHistoryState
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import (
    CompletedApprovalRun,
    PausedAnswer,
    ResponseTurnContext,
)
from mindroom.team_exact_members import ResolvedExactTeamMembers
from mindroom.teams import (
    TeamMode,
    _TeamStreamPresentation,
    continue_paused_team_run,
)
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.runtime_context import (
    LiveToolDispatchContext,
    build_execution_identity_from_runtime_context,
    tool_runtime_context,
)
from tests.conftest import message_origin
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.tool_job_helpers import (
    lookup,
    pending_outcomes,
    tool_job_runtime,
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
@pytest.mark.parametrize("background_after", [0, 0.001])
async def test_native_approval_leaves_running_work_to_its_follow_up(  # noqa: PLR0915
    tmp_path: Path,
    team: bool,
    background_after: float,
) -> None:
    """A resumed approval finishes while its approved work runs on, leaving that work to its follow-up."""
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
            ModelResponse(tool_calls=[_call("add", "approved-call", background_after=background_after)]),
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
    approval_calls = (
        ApprovalCall(
            "approved-call",
            "add",
            "leader",
            2**62,
            toolkit_name="calculator",
            arguments_digest=approval_arguments_digest({"background_after": background_after}),
        ),
    )
    pending = None
    try:
        with tool_runtime_context(context):
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
                origin=message_origin(sender_id=owner.requester_id),
                response_event_id="$response",
                sources=ResponseSources(("$source",), ("$source",)),
                calls=approval_calls,
                state="claimed",
            )

            @owned_tool_execution()
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
                        paused_answer=PausedAnswer(),
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
            release.set()
            ready = await runtime.wait(jobs[0].job_id, owner=owner, depth=0)
            await runtime.release_wait(jobs[0].job_id, ready.claim)
            # Nothing retrieved the outcome; its follow-up reports it.
            assert [job.job_id for job in pending_outcomes(runtime)] == [jobs[0].job_id]
            assert executions == 1
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await runtime.shutdown()
