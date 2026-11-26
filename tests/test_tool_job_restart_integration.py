"""Restart coverage for retained background results and native approvals."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Never

import pytest

from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import AgentSpec, RunContext
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.background import ACKNOWLEDGED_BY, delegation_outcome, job_acknowledgers
from mindroom.delegation.state import DelegationChild
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND
from mindroom.history.run_checkpoint import checkpoint_hooks
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.response_turn import AttemptResolved, CompletedAttempt, TurnRunState
from mindroom.run_storage import RunStorage
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs.consumption import (
    ConsumptionOwner,
    acknowledge_saved_run,
    consumption_context,
    set_consumption_storage,
)
from mindroom.tool_jobs.control import pending_human_messages_context
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.execution import job_definitions_hook, with_job_admission
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import AdaptedToolkit
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.scripted_agent_helpers import ProcessCrash
from tests.test_tool_job_follow_ups import _drive_jobs_turn
from tests.test_tool_job_turn_integration import _wait_until_ready
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    always_owned,
    job_actor,
    job_tool,
    lookup,
    pending_outcomes,
    run_job_turn,
    saved_jobs,
    start_job,
    tool_job_runtime,
    toolkit_job_tools,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.runs import RunRecord
    from mindroom.response_turn import TurnContinuationRunState
    from mindroom.session_storage.store import ConversationStore


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

    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = context.execution_identity
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = asyncio.Event()
    result_observed = asyncio.Event()

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", config, paths, owner)

    original_model = ScriptedModel(
        [
            calls(tool_call("slow_tool", "original-call", background_after=None)),
            answer("Original response released."),
        ],
    )
    original_storage = storage_factory()
    controls = job_actor(*toolkit_job_tools(JobTools(paths), authored_name="job").values())

    @owned_tool_execution(enabled=always_owned)
    async def start_original() -> RunRecord:
        set_consumption_storage(storage_factory)
        return await run_job_turn(
            job_actor(job_tool(slow_tool)),
            original_model,
            "Start durable work",
            run_id="original",
            session_id=context.session_id,
            store=original_storage,
        )

    async def observe_and_hang() -> Never:
        # The retained result is in the request; the crash comes before this response returns.
        result_observed.set()
        await asyncio.Event().wait()
        raise AssertionError

    interrupted_storage = storage_factory()
    original_task = None
    interrupted_task = None
    restored = None
    final_storage = None
    try:
        with tool_runtime_context(context), pending_human_messages_context(signal):
            original_task = asyncio.create_task(start_original())
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            signal.set()
            original = await original_task
        assert original.tools is not None
        job_id = json.loads(str(original.tools[0].result))["job_id"]
        release.set()
        assert (await _wait_until_ready(runtime, job_id, owner=owner)).status == "completed"

        interrupted_model = ScriptedModel(
            [
                calls(tool_call("job", "interrupted-wait", action="wait", job_id=job_id, wait_timeout=0)),
                [Await(observe_and_hang)],
            ],
        )

        def unavailable_storage() -> ConversationStore:
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
            state: TurnContinuationRunState,
        ) -> AsyncIterator[AttemptResolved]:
            response = await run_job_turn(
                controls,
                interrupted_model,
                state.active_prompt,
                run_id="interrupted",
                session_id=context.session_id,
                store=interrupted_storage,
            )
            yield AttemptResolved(
                CompletedAttempt(
                    response_text=str(response.content or ""),
                    replayable_text=str(response.content or ""),
                    has_visible_content=bool(response.content),
                    attempt_run_id=response.run_id,
                ),
            )

        with tool_runtime_context(context):
            interrupted_task = asyncio.create_task(
                _drive_jobs_turn(
                    interrupted_attempt,
                    prompt="Retrieve retained work",
                    load_scope=lambda: crash_scope,
                    entity_label="leader",
                    session_id=context.session_id,
                    room_id=owner.room_id,
                    thread_id=owner.resolved_thread_id,
                    requester_id=owner.requester_id,
                ),
            )
            await asyncio.wait_for(result_observed.wait(), JOB_TEST_TIMEOUT)
            assert "retained after restart" in str(interrupted_model.requests[-1].messages[-1].content)
            interrupted_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await interrupted_task

        before_restart = await lookup(runtime, job_id, owner=owner, depth=0)
        assert not before_restart.consumed
        assert len(pending_outcomes(runtime)) == 1
        await runtime.shutdown()

        restored = await tool_job_runtime(paths.storage_root)
        await restored.recover()
        register_background_runtime(paths, restored)
        recovered = await lookup(restored, job_id, owner=owner, depth=0)
        assert recovered.result == "retained after restart"
        assert not recovered.consumed

        final_model = ScriptedModel(
            [
                calls(tool_call("job", "recovered-wait", action="wait", job_id=job_id, wait_timeout=0)),
                answer("Recovered result delivered."),
            ],
        )
        final_storage = storage_factory()

        @owned_tool_execution(enabled=always_owned)
        async def retrieve_after_restart() -> RunRecord:
            set_consumption_storage(storage_factory)
            return await run_job_turn(
                controls,
                final_model,
                "Retrieve after restart",
                run_id="final",
                session_id=context.session_id,
                store=final_storage,
            )

        with tool_runtime_context(context):
            final = await asyncio.wait_for(retrieve_after_restart(), JOB_TEST_TIMEOUT)
        assert final.tools is not None
        retrieved = next(tool for tool in final.tools if tool.tool_call_id == "recovered-wait")
        assert retrieved.result == "retained after restart"
        [read] = [
            message
            for message in final_model.requests[-1].messages
            if message.role == "tool" and message.tool_call_id == "recovered-wait"
        ]
        assert read.get_content_string() == "retained after restart"
        assert executions == 1
        assert (await lookup(restored, job_id, owner=owner, depth=0)).consumed
        assert pending_outcomes(restored) == []
    finally:
        release.set()
        if original_task is not None and not original_task.done():
            original_task.cancel()
        if original_task is not None:
            await asyncio.gather(original_task, return_exceptions=True)
        if interrupted_task is not None and not interrupted_task.done():
            interrupted_task.cancel()
            await asyncio.gather(interrupted_task, return_exceptions=True)
        if restored is not None:
            await restored.shutdown()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_crash_after_checkpoint_preserves_mid_turn_consumption_across_restart(  # noqa: PLR0915 - End-to-end crash/restart check.
    tmp_path: Path,
) -> None:
    """A hard crash after a before_request checkpoint keeps inline and waited jobs consumed across restart."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    message_context = replace(_delegate_runtime_context(config, paths), source_kind=MESSAGE_SOURCE_KIND)
    scheduled_context = replace(message_context, source_kind=SILENT_SCHEDULE_SOURCE_KIND)
    owner = message_context.execution_identity
    assert owner is not None
    assert owner.room_id is not None
    assert owner.requester_id is not None
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    restored = None

    async def inline_tool() -> str:
        return "inline ok"

    async def crash_hard() -> Never:
        raise ProcessCrash

    async def unexpected_persist(_record: object) -> None:
        raise AssertionError

    try:
        child = DelegationChild(
            delegation_id="deleg-1",
            parent_tool_call_id="call-deleg-1",
            caller_agent_name="leader",
            child_agent_name="worker",
            task="Research",
            session_id="child-session",
            run_id="child-run",
            model_name="default",
            requested_model_name="default",
            depth=1,
            execution_identity={},
            subagent_id="a" * 32,
            agent_mode="standard",
            persona=None,
            cost_usd=0.42,
        )

        async def finish_delegation() -> object:
            return delegation_outcome("completed", "Delegated report.")

        await start_job(
            runtime,
            "deleg-1",
            tool_name="worker",
            depth=0,
            kind="delegation",
            source_kind=MESSAGE_SOURCE_KIND,
            adapter={"child": asdict(child)},
            owner=owner,
            operation=finish_delegation,
        )
        await wait_for_status(runtime, "deleg-1", "completed")

        store = create_session_storage("leader", config, paths, owner)
        session = ConversationSession(session_id=message_context.session_id, kind="agent", owner_id="leader")
        cp_hooks = checkpoint_hooks(
            RunStorage(store=store, session=session, scope=None),
            on_saved=acknowledge_saved_run,
        )
        assert cp_hooks is not None

        actor = job_actor(*toolkit_job_tools(JobTools(paths), authored_name="job").values(), job_tool(inline_tool))
        model = ScriptedModel(
            [
                calls(
                    tool_call("job", "wait-deleg", action="wait", job_id="deleg-1", wait_timeout=0),
                    tool_call("inline_tool", "inline-call"),
                ),
                [Await(crash_hard)],
            ],
        )
        spec = AgentSpec(
            id="leader",
            name="leader",
            model=model,
            system_messages=(),
            toolkits=(
                AdaptedToolkit(
                    name="tools",
                    functions=tuple(t.function for t in actor.tools.values()),
                    instructions=None,
                ),
            ),
        )
        hooks = LoopHooks(
            before_request=(job_definitions_hook(actor), cp_hooks.before_request),
            before_dispatch=cp_hooks.before_dispatch,
            tool_middleware=with_job_admission(actor, ()),
        )
        consumption_owner = ConsumptionOwner()
        async with execution_resources(response=True, jobs_enabled=True):
            with (
                tool_runtime_context(scheduled_context),
                consumption_context(consumption_owner),
                pytest.raises(ProcessCrash),
            ):
                async for _event in run_agent(
                    spec,
                    [Message(role="user", content="Run scheduled check")],
                    ctx=RunContext(run_id="scheduled-run", session=session),
                    hooks=hooks,
                    persist=unexpected_persist,
                    request_template=ModelRequest(messages=()),
                ):
                    pass

        tool_results = {
            message.tool_call_id: message.get_content_string()
            for message in model.requests[-1].messages
            if message.role == "tool"
        }
        assert tool_results == {"wait-deleg": "Delegated report.", "inline-call": "inline ok"}

        # Neither finalize_consumption() nor terminal persist ran, yet the journal already recorded both acknowledgements.
        assert consumption_owner._claims == []
        persisted_before_shutdown = await saved_jobs(paths.storage_root)
        assert persisted_before_shutdown["deleg-1"].consumed
        await runtime.shutdown()

        restored = await tool_job_runtime(paths.storage_root)
        await restored.recover()
        register_background_runtime(paths, restored)

        deleg = await lookup(restored, "deleg-1", owner=owner, depth=0)
        assert deleg.consumed
        assert deleg.adapter[ACKNOWLEDGED_BY] == "scheduled-run"
        assert job_acknowledgers(restored.retained_jobs()) == {"deleg-1": "scheduled-run"}

        [inline_job] = [job for job in restored.retained_jobs() if job.tool_name == "inline_tool"]
        assert inline_job.consumed
        assert inline_job.returned_inline

        message_key = ConversationKey(
            recipient=owner.recipient,
            room_id=owner.room_id,
            thread_id=owner.resolved_thread_id,
            requester_id=owner.requester_id,
            silent=False,
        )
        scheduled_key = replace(message_key, silent=True)
        assert await restored.ended_jobs(message_key) == ()
        assert await restored.ended_jobs(scheduled_key) == ()
        assert restored.follow_up_candidate_revisions() == {}

        jobs_tool = JobTools(paths)
        for ctx in (message_context, scheduled_context):
            with tool_runtime_context(ctx):
                listed = json.loads(await jobs_tool.job(action="list"))
                assert inline_job.job_id not in {item["job_id"] for item in listed}
                assert await jobs_tool.job(action="collect") == "No ended background jobs."
    finally:
        if restored is not None:
            await restored.shutdown()
        await runtime.shutdown()
