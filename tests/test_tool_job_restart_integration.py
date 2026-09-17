"""Restart coverage for retained background results and native approvals."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.tools.function import Function

from mindroom import response_runner
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_storage import create_session_storage
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import (
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
)
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_turn import (
    CompletedAttempt,
    TurnRunState,
    TurnSinks,
    paused_attempt_from_response,
    run_blocking_response_turn,
)
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.disabled import approval_is_parked, event_is_parked
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.events import format_tool_started_event
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_response_turn import _AdapterLog, _blocking_adapter, _continuation, _ctx
from tests.test_tool_job_turn_integration import _provider_tool_content, _wait_until_ready

if TYPE_CHECKING:
    from pathlib import Path

    from agno.run.agent import RunOutput

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
async def test_native_approval_writer_marker_parks_after_storage_change(  # noqa: PLR0915 - Real SDK pause and disabled startup.
    tmp_path: Path,
) -> None:
    """Disabled startup trusts the real writer marker after session storage moves."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    config = bot.config
    config.background_tool_jobs = True
    paths = bot.runtime_paths
    owner = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="!room:localhost:$thread",
        transport_agent_name="general",
    )
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name="general",
        transport_agent_name="general",
        membership_turn_id="$approval-source",
    )
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    side_effects: list[str] = []

    async def write_report() -> str:
        side_effects.append("executed")
        return "approved report"

    function = Function.from_callable(write_report)
    function.requires_confirmation = True
    function.owning_toolkit = "test_toolkit"
    paused_model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("write_report", "approval-call", wait_timeout=None)]),
        ],
    )
    install_tool_job_execution(paused_model)
    storage = create_session_storage("general", config, paths, owner)
    actor = Agent(
        id="general",
        model=paused_model,
        tools=[function, JobTools(paths, owner)],
        db=storage,
        telemetry=False,
    )
    disabled = None
    try:
        with tool_runtime_context(context):
            response = await actor.arun(
                "Write the report",
                session_id=owner.session_id,
                user_id=owner.requester_id,
            )
        assert response.status is RunStatus.paused
        paused = paused_attempt_from_response(
            response,
            fallback_session_id=owner.session_id,
            fallback_run_id=response.run_id,
            toolkit_owners={("general", "write_report"): "test_toolkit"},
        )
        assert paused is not None
        marker, trace = format_tool_started_event(paused.tools[0], tool_index=1)
        assert trace is not None
        paused = replace(
            paused,
            response_text=marker.strip(),
            tool_trace=(trace,),
        )

        store = runner.deps.approval_store
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
        request = _plain_request(
            _target(thread_id="$thread", reply_to_event_id="$approval-source"),
            source_event_id="$approval-source",
        )
        with (
            patch("mindroom.delivery_gateway.DeliveryGateway.edit_text", new=AsyncMock(return_value=True)),
            patch(
                "mindroom.approval_response.resolve_tool_approval_approver",
                return_value=owner.requester_id,
            ),
            patch(
                "mindroom.approval_response.evaluate_tool_approval",
                new=AsyncMock(return_value=(True, 60.0)),
            ),
            patch.object(runner._approval_responses, "publish_generation", new=AsyncMock()),
        ):
            await runner._suspend_for_approval(
                paused,
                request=request,
                target=request.response_envelope.target,
                progress=response_runner._DeliveryProgress(tracked_event_id="$approval-response"),
                execution_identity=owner,
                entity_kind="agent",
                history_scope=HistoryScope(kind="agent", scope_id="general"),
                show_tool_calls=True,
            )

        saved = await store.approval_continuation_for_source("$approval-source")
        assert saved is not None
        assert saved.requires_background_tool_jobs is True

        storage.close()
        config.agents["general"].private = AgentPrivateConfig(per="user")
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
        source_event_id = "$approval-source"
        event = await store.load_event(source_event_id)
        assert event is not None
        assert approval_is_parked(paths, saved.approval_id)
        assert event_is_parked(config, paths, "general", event)
        assert await store.is_pending(source_event_id)
        assert side_effects == []
    finally:
        register_background_runtime(paths, None)
        if disabled is not None:
            await disabled.stop()
        await runtime.shutdown()
        storage.close()
