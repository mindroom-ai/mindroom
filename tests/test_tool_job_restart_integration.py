"""Restart coverage for retained background results and native approvals."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from typing import TYPE_CHECKING, cast

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.response import ModelResponse

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
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.control import pending_human_messages_context
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.test_response_turn import _AdapterLog, _blocking_adapter, _continuation, _ctx
from tests.test_tool_job_turn_integration import _provider_tool_content, _wait_until_ready
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    assembled_function,
    lookup,
    pending_outcomes,
    tool_job_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agno.run.agent import RunOutput

    from mindroom.response_turn import TurnContinuationRunState


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
    storage_file = str(tmp_path / "restart.db")

    def storage_factory() -> SqliteDb:
        return SqliteDb(db_file=storage_file)

    original_model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("slow_tool", "original-call", background_after=None)]),
            ModelResponse(content="Original response released."),
        ],
    )
    install_tool_job_execution(original_model)
    original_storage = storage_factory()
    original_actor = Agent(
        id="leader",
        model=original_model,
        tools=[assembled_function(slow_tool)],
        db=original_storage,
        telemetry=False,
    )

    @owned_tool_execution()
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
        with tool_runtime_context(context), pending_human_messages_context(signal):
            original_task = asyncio.create_task(start_original())
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            signal.set()
            original = await original_task
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
            state: TurnContinuationRunState,
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
            await asyncio.wait_for(interrupted_model.result_observed.wait(), JOB_TEST_TIMEOUT)
            assert "retained after restart" in str(interrupted_model.seen_messages[-1].content)
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

        @owned_tool_execution()
        async def retrieve_after_restart() -> RunOutput:
            set_consumption_storage(storage_factory)
            return await final_actor.arun(
                "Retrieve after restart",
                session_id=context.session_id,
                user_id=owner.requester_id,
            )

        with tool_runtime_context(context):
            final = await asyncio.wait_for(retrieve_after_restart(), JOB_TEST_TIMEOUT)
        assert final.tools is not None
        retrieved = next(tool for tool in final.tools if tool.tool_call_id == "recovered-wait")
        assert retrieved.result == "retained after restart"
        assert _provider_tool_content(final_model, "recovered-wait") == "retained after restart"
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
        original_storage.close()
        interrupted_storage.close()
        if final_storage is not None:
            final_storage.close()
        if restored is not None:
            await restored.shutdown()
        await runtime.shutdown()
