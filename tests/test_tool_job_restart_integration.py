"""Restart coverage for retained background results and native approvals."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, cast

import pytest
from agno.agent import Agent
from agno.db.base import BaseDb, SessionType
from agno.db.sqlite import SqliteDb
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.tools.function import Function

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_storage import create_session_storage
from mindroom.approval_execution import _continue_persisted_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalDecision,
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
)
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import (
    CompletedAttempt,
    TurnRunState,
    TurnSinks,
    apply_local_approval_decisions,
    run_blocking_response_turn,
)
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.disabled import approval_is_parked, event_is_parked
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, serialize_tool_execution_identity
from tests.response_runner_helpers import _bot
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_response_turn import _AdapterLog, _blocking_adapter, _continuation, _ctx
from tests.test_tool_job_turn_integration import _provider_tool_content, _wait_until_ready

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.response_turn import DynamicContinuationRunState


class _ResultObservedBarrierModel(DelegationModel):
    """Pause provider continuation after the retained result entered its messages."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(id="test", responses=responses)
        self.result_observed = asyncio.Event()

    async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
        response = await super().ainvoke(*args, **kwargs)
        if response.content == "This response must be interrupted.":
            self.result_observed.set()
            await asyncio.Event().wait()
        return response


@pytest.mark.asyncio
async def test_interrupted_unconfirmed_result_is_retrieved_after_runtime_reconstruction(  # noqa: C901, PLR0915 - Real crash/restart lifecycle.
    tmp_path: Path,
) -> None:
    """A crash before receipt readback retains the real result without replaying its side effect."""
    started, release = asyncio.Event(), asyncio.Event()
    executions = 0

    async def slow_tool() -> str:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return "retained after restart"

    config = Config(background_tool_jobs=True, agents={"leader": AgentConfig(display_name="Leader")})
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    storage_file = str(tmp_path / "restart.db")

    def storage_factory() -> SqliteDb:
        return SqliteDb(db_file=storage_file)

    original_model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("slow_tool", "original-call", wait_timeout=None)]),
            ModelResponse(content="Original response released."),
        ],
    )
    install_tool_job_execution(original_model)
    original_storage = storage_factory()
    original_actor = Agent(id="leader", model=original_model, tools=[slow_tool], db=original_storage, telemetry=False)

    @owned_tool_execution
    async def start_original() -> RunOutput:
        set_consumption_storage(storage_factory)
        return await original_actor.arun(
            "Start durable work",
            session_id=context.session_id,
            user_id=owner.requester_id,
        )

    interrupted_storage = storage_factory()
    original_task = None
    interrupted_task = None
    restored = None
    final_storage = None
    try:
        with tool_runtime_context(context), human_message_signal_context(signal):
            original_task = asyncio.create_task(start_original())
            await asyncio.wait_for(started.wait(), 2)
            signal.notify()
            original = await asyncio.wait_for(original_task, 2)
        assert original.tools is not None
        job_id = json.loads(cast("str", original.tools[0].result))["job_id"]
        release.set()
        assert (await _wait_until_ready(runtime, job_id, owner=owner)).status == "completed"

        interrupted_model = _ResultObservedBarrierModel(
            [
                ModelResponse(
                    tool_calls=[_call("job", "interrupted-wait", action="wait", job_id=job_id, wait_timeout=0)],
                ),
                ModelResponse(content="This response must be interrupted."),
            ],
        )
        install_tool_job_execution(interrupted_model)
        interrupted_actor = Agent(
            id="leader",
            model=interrupted_model,
            tools=[JobTools(paths, owner)],
            db=interrupted_storage,
            telemetry=False,
        )

        def unavailable_storage() -> SqliteDb:
            msg = "simulated crash before canonical receipt readback"
            raise RuntimeError(msg)

        crash_scope = ScopeSessionContext(
            HistoryScope(kind="agent", scope_id="leader"),
            interrupted_storage,
            None,
            session_id=context.session_id,
            storage_factory=unavailable_storage,
        )

        async def interrupted_attempt(
            _run: TurnRunState,
            state: DynamicContinuationRunState,
        ) -> CompletedAttempt:
            response = await interrupted_actor.arun(
                state.active_prompt,
                session_id=context.session_id,
                user_id=owner.requester_id,
            )
            return CompletedAttempt(
                response_text=str(response.content or ""),
                replayable_text=str(response.content or ""),
                has_visible_content=bool(response.content),
                session_id=response.session_id,
                attempt_run_id=response.run_id,
            )

        with tool_runtime_context(context):
            interrupted_task = asyncio.create_task(
                run_blocking_response_turn(
                    _ctx(
                        entity_label="leader",
                        session_id=context.session_id,
                        room_id=owner.room_id,
                        thread_id=owner.resolved_thread_id,
                        requester_id=owner.requester_id,
                        background_tool_jobs=True,
                    ),
                    _blocking_adapter(
                        _AdapterLog(),
                        interrupted_attempt,
                        open_scope=lambda: nullcontext(crash_scope),
                    ),
                    TurnSinks(),
                    continuation=_continuation("Retrieve retained work"),
                ),
            )
            await asyncio.wait_for(interrupted_model.result_observed.wait(), 2)
            assert "retained after restart" in str(interrupted_model.seen_messages[-1].content)
            interrupted_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await interrupted_task

        before_restart = await runtime.lookup(job_id, owner=owner, depth=0)
        assert not before_restart.wait_acknowledged
        assert len(await runtime.pending_outcomes()) == 1
        await runtime.shutdown()
        register_background_runtime(paths, None)

        restored = ToolJobRuntime(paths.storage_root)
        await restored.recover()
        register_background_runtime(paths, restored)
        recovered = await restored.lookup(job_id, owner=owner, depth=0)
        assert recovered.result == "retained after restart"
        assert not recovered.wait_acknowledged

        final_model = DelegationModel(
            id="test",
            responses=[
                ModelResponse(
                    tool_calls=[_call("job", "recovered-wait", action="wait", job_id=job_id, wait_timeout=0)],
                ),
                ModelResponse(content="Recovered result delivered."),
            ],
        )
        install_tool_job_execution(final_model)
        final_storage = storage_factory()
        final_actor = Agent(
            id="leader",
            model=final_model,
            tools=[JobTools(paths, owner)],
            db=final_storage,
            telemetry=False,
        )

        @owned_tool_execution
        async def retrieve_after_restart() -> RunOutput:
            set_consumption_storage(storage_factory)
            return await final_actor.arun(
                "Retrieve after restart",
                session_id=context.session_id,
                user_id=owner.requester_id,
            )

        with tool_runtime_context(context):
            final = await asyncio.wait_for(retrieve_after_restart(), 2)
        assert final.tools is not None
        retrieved = next(tool for tool in final.tools if tool.tool_call_id == "recovered-wait")
        assert retrieved.result == "retained after restart"
        assert _provider_tool_content(final_model, "recovered-wait") == "retained after restart"
        assert executions == 1
        assert (await restored.lookup(job_id, owner=owner, depth=0)).wait_acknowledged
        assert await restored.pending_outcomes() == []
    finally:
        release.set()
        if original_task is not None and not original_task.done():
            original_task.cancel()
        if original_task is not None:
            await asyncio.gather(original_task, return_exceptions=True)
        if interrupted_task is not None and not interrupted_task.done():
            interrupted_task.cancel()
            await asyncio.gather(interrupted_task, return_exceptions=True)
        original_storage.close()
        interrupted_storage.close()
        if final_storage is not None:
            final_storage.close()
        register_background_runtime(paths, None)
        if restored is not None:
            await restored.shutdown()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False], ids=["approve", "deny"])
async def test_native_approval_is_parked_disabled_then_resumed_once(  # noqa: C901, PLR0915 - Real pause and two startups.
    tmp_path: Path,
    approved: bool,
) -> None:
    """Disabled startup preserves a real pause; enabled reconstruction settles the exact call once."""
    bot = _bot(tmp_path)
    config = bot.config
    config.background_tool_jobs = True
    paths = bot.runtime_paths
    owner = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@requester:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="!room:localhost_$thread",
    )
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name="general",
        transport_agent_name=None,
        membership_turn_id="$approval-source",
    )
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    executions = 0
    approval_started, approval_release = asyncio.Event(), asyncio.Event()

    async def write_report() -> str:
        nonlocal executions
        executions += 1
        approval_started.set()
        await approval_release.wait()
        return "approved report"

    function = Function.from_callable(write_report)
    function.requires_confirmation = True
    paused_model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("write_report", "approval-call", wait_timeout=None)]),
        ],
    )
    install_tool_job_execution(paused_model)

    def storage_factory() -> BaseDb:
        return create_session_storage("general", config, paths, owner)

    storage = storage_factory()
    actor = Agent(
        id="general",
        model=paused_model,
        tools=[function, JobTools(paths, owner)],
        db=storage,
        telemetry=False,
    )
    disabled = None
    enabled = None
    restarted_runtime = None
    restart_storage = None
    resume_task = None
    try:
        with tool_runtime_context(context):
            paused = await actor.arun(
                "Write the report",
                session_id=owner.session_id,
                user_id=owner.requester_id,
            )
        assert paused.status is RunStatus.paused
        assert paused.requirements
        assert executions == 0
        assert paused.user_id == owner.requester_id
        assert storage.delete_session(owner.session_id, user_id=owner.requester_id)
        storage.upsert_session(
            AgentSession(
                session_id=owner.session_id,
                agent_id="general",
                user_id="@history-owner:localhost",
            ),
        )
        storage.upsert_run(paused, session_id=owner.session_id, user_id=owner.requester_id)
        storage.close()

        store = bot._journal_store.principal(bot._journal_principal_id)
        await store.admit(
            InboundEvent(
                "$approval-source",
                owner.room_id,
                owner.resolved_thread_id,
                EventKind.MESSAGE,
                EventClass.ACTIONABLE,
                owner.requester_id,
                1,
                {"event_id": "$approval-source", "content": {"body": "write report"}},
            ),
            ProjectedEvent(
                "$approval-source",
                owner.room_id,
                owner.resolved_thread_id,
                owner.requester_id,
                1,
                {"body": "write report"},
                None,
                None,
            ),
        )
        continuation = ApprovalContinuation(
            approval_id="approval",
            run_id=paused.run_id,
            session_id=owner.session_id,
            entity_kind="agent",
            entity_name="general",
            room_id=owner.room_id,
            thread_id=owner.resolved_thread_id,
            requester_id=owner.requester_id,
            response_event_id="$approval-response",
            sources=ResponseSources(("$approval-source",), ("$approval-source",)),
            calls=(
                ApprovalCall(
                    tool_call_id="approval-call",
                    tool_name="write_report",
                    invoking_agent="general",
                    expires_at_ns=2**62,
                    decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.DENIED,
                ),
            ),
            state="ready",
            execution_identity=serialize_tool_execution_identity(owner),
            history_scope=HistoryScope(kind="agent", scope_id="general"),
        )
        assert await store.create_approval_continuation(continuation) is not None
        event = await store.load_event("$approval-source")
        assert event is not None

        register_background_runtime(paths, None)
        await runtime.shutdown()
        config.background_tool_jobs = False
        disabled = ToolJobRuntimeCoordinator(
            paths,
            lambda: config,
            lambda _: None,
            AgentReplyMembershipIndex(),
        )
        await disabled.initialize(bot._journal_store)
        assert approval_is_parked(paths, "approval")
        assert event_is_parked(config, paths, "general", event)
        assert await store.approval_continuation("approval") == continuation
        assert await store.is_pending("$approval-source")
        assert executions == 0
        await disabled.stop()
        disabled = None

        config.background_tool_jobs = True
        enabled = ToolJobRuntimeCoordinator(
            paths,
            lambda: config,
            lambda _: None,
            AgentReplyMembershipIndex(),
        )
        await enabled.initialize(bot._journal_store)
        assert not approval_is_parked(paths, "approval")
        assert not event_is_parked(config, paths, "general", event)
        claimed = await store.claim_approval_continuation("approval", runtime_generation="enabled-restart")
        assert claimed is not None

        restarted_runtime = ToolJobRuntime(paths.storage_root)
        await restarted_runtime.recover()
        register_background_runtime(paths, restarted_runtime)
        restart_storage = storage_factory()
        persisted = restart_storage.get_run(paused.run_id)
        assert isinstance(persisted, RunOutput)
        session = restart_storage.get_session(
            owner.session_id,
            session_type=SessionType.AGENT,
            user_id="@history-owner:localhost",
        )
        assert isinstance(session, AgentSession)
        assert session.user_id == "@history-owner:localhost"
        assert persisted.user_id == owner.requester_id

        resumed_function = Function.from_callable(write_report)
        resumed_function.requires_confirmation = True
        resumed_model = DelegationModel(id="test", responses=[ModelResponse(content="Approval settled.")])
        install_tool_job_execution(resumed_model)
        resumed_actor = Agent(
            id="general",
            model=resumed_model,
            tools=[resumed_function, JobTools(paths, owner)],
            db=restart_storage,
            telemetry=False,
        )
        decisions = {"approval-call": approved}
        denial_reasons = {"approval-call": None if approved else "Denied for this test."}
        requirements = apply_local_approval_decisions(
            persisted,
            decisions=decisions,
            denial_reasons=denial_reasons,
        )

        @owned_tool_execution
        async def resume() -> RunOutput:
            set_consumption_storage(storage_factory)
            response, _presentation = await _continue_persisted_agent(
                resumed_actor,
                claimed,
                persisted,
                requirements,
                config=config,
                runtime_paths=paths,
                execution_identity=owner,
                refresh_scheduler=None,
                decisions=decisions,
                denial_reasons=denial_reasons,
            )
            return response

        with tool_runtime_context(context):
            resume_task = asyncio.create_task(resume())
            if approved:
                await asyncio.wait_for(approval_started.wait(), 2)
                approval_release.set()
            response = await asyncio.wait_for(resume_task, 3)
        assert response.status is RunStatus.completed
        assert executions == int(approved)
        assert response.tools is not None
        settled_tool = next(tool for tool in response.tools if tool.tool_call_id == "approval-call")
        assert settled_tool.confirmed is approved
        if approved:
            assert settled_tool.result == "approved report"
            assert _provider_tool_content(resumed_model, "approval-call") == "approved report"
        else:
            assert settled_tool.result is None
            assert settled_tool.confirmation_note == "Denied for this test."
            assert "Denied for this test." in _provider_tool_content(resumed_model, "approval-call")
        job_owner = replace(owner, transport_agent_name="general")
        jobs = await restarted_runtime.list_jobs(owner=job_owner, depth=0)
        assert len(jobs) == int(approved)
        if approved:
            assert jobs[0].status == "completed"
            assert jobs[0].result == "approved report"
            assert jobs[0].wait_acknowledged
        assert await restarted_runtime.pending_outcomes() == []
    finally:
        approval_release.set()
        if resume_task is not None and not resume_task.done():
            resume_task.cancel()
        if resume_task is not None:
            await asyncio.gather(resume_task, return_exceptions=True)
        register_background_runtime(paths, None)
        if restart_storage is not None:
            restart_storage.close()
        if restarted_runtime is not None:
            await restarted_runtime.shutdown()
        if enabled is not None:
            await enabled.stop()
        if disabled is not None:
            await disabled.stop()
        await runtime.shutdown()
        storage.close()
