"""Restart coverage for retained background results and native approvals."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Never

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.response_turn import (
    CompletedAttempt,
    TurnRunState,
    TurnSinks,
    run_blocking_response_turn,
)
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.control import pending_human_messages_context
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.test_response_turn import _AdapterLog, _blocking_adapter, _continuation, _ctx
from tests.test_tool_job_turn_integration import _wait_until_ready
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    job_actor,
    job_tool,
    lookup,
    pending_outcomes,
    run_job_turn,
    tool_job_runtime,
    toolkit_job_tools,
)

if TYPE_CHECKING:
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
    owner = build_execution_identity_from_runtime_context(context)
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
    controls = job_actor(*toolkit_job_tools(JobTools(paths, owner), authored_name="job").values())

    @owned_tool_execution()
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
        ) -> CompletedAttempt:
            response = await run_job_turn(
                controls,
                interrupted_model,
                state.active_prompt,
                run_id="interrupted",
                session_id=context.session_id,
                store=interrupted_storage,
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
                        load_scope=lambda: crash_scope,
                    ),
                    TurnSinks(),
                    continuation=_continuation("Retrieve retained work"),
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

        @owned_tool_execution()
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
