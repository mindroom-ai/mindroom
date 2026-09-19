"""Joined response-turn coverage for retained background tool results."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from typing import TYPE_CHECKING, cast

import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.models.response import ModelResponse
from agno.run.agent import RunContentEvent, RunOutput

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.response_turn import (
    AttemptResolved,
    CompletedAttempt,
    TurnRunState,
    TurnSinks,
    stream_response_turn,
)
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.events import BackgroundWaitChunk
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_response_turn import _AdapterLog, _continuation, _ctx, _streaming_adapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.response_turn import DynamicContinuationRunState
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


async def _wait_until_ready(
    runtime: ToolJobRuntime,
    job_id: str,
    *,
    owner: ToolExecutionIdentity,
) -> BackgroundJob:
    while True:
        job = await runtime.lookup(job_id, owner=owner, depth=0)
        if job.status not in {"running", "cancel_requested"}:
            return job
        runtime.changed.clear()
        job = await runtime.lookup(job_id, owner=owner, depth=0)
        if job.status not in {"running", "cancel_requested"}:
            return job
        await asyncio.wait_for(runtime.changed.wait(), 2)


def _provider_tool_content(model: DelegationModel, tool_call_id: str) -> str:
    messages = [
        message for message in model.seen_messages if message.role == "tool" and message.tool_call_id == tool_call_id
    ]
    assert len(messages) == 1
    return messages[0].get_content_string()


class _ActiveTextBarrierModel(DelegationModel):
    """Pause one provider text stream after its first visible chunk."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(id="test", responses=responses)
        self.text_started = asyncio.Event()
        self.release_text = asyncio.Event()

    async def ainvoke_stream(self, *args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        response = await self.ainvoke(*args, **kwargs)
        if response.content == "Independent work done.":
            yield ModelResponse(content="Independent work ")
            self.text_started.set()
            await self.release_text.wait()
            yield ModelResponse(content="done.")
            return
        yield response


class _ListBarrierModel(DelegationModel):
    """Hold the next provider action after the running job has been listed."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__(id="test", responses=responses)
        self.list_observed = asyncio.Event()
        self.allow_wait = asyncio.Event()

    async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
        response = await super().ainvoke(*args, **kwargs)
        if response.tool_calls and response.tool_calls[0]["id"] == "wait-call":
            self.list_observed.set()
            await self.allow_wait.wait()
        return response


@pytest.mark.asyncio
async def test_human_released_job_is_rediscovered_and_consumed_in_newer_turn(  # noqa: PLR0915 - Real two-turn lifecycle.
    tmp_path: Path,
) -> None:
    """A newer real SDK turn lists and retrieves the original side effect exactly once."""
    started, release = asyncio.Event(), asyncio.Event()
    executions = 0

    async def slow_tool() -> str:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return "durable report"

    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    signal = HumanMessageSignal()
    storage_file = str(tmp_path / "turns.db")

    def storage_factory() -> SqliteDb:
        return SqliteDb(db_file=storage_file)

    model = _ListBarrierModel(
        [
            ModelResponse(tool_calls=[_call("slow_tool", "original-call", wait_timeout=None)]),
            ModelResponse(content="The original turn was released."),
        ],
    )
    install_tool_job_execution(model)
    storage = storage_factory()
    actor = Agent(id="leader", model=model, tools=[slow_tool, JobTools(paths, owner)], db=storage, telemetry=False)

    @owned_tool_execution
    async def run_turn(prompt: str) -> RunOutput:
        set_consumption_storage(storage_factory)
        return await actor.arun(prompt, session_id=context.session_id, user_id=owner.requester_id)

    first_pending = None
    second_pending = None
    try:
        with tool_runtime_context(context), human_message_signal_context(signal):
            first_pending = asyncio.create_task(run_turn("Start the report"))
            await asyncio.wait_for(started.wait(), 2)
            signal.notify()
            first = await asyncio.wait_for(first_pending, 2)
        assert first.tools is not None
        job_id = json.loads(cast("str", first.tools[0].result))["job_id"]
        assert "released" in str(first.content)
        assert (await runtime.lookup(job_id, owner=owner, depth=0)).status == "running"

        signal.clear()
        model.responses.extend(
            [
                ModelResponse(tool_calls=[_call("job", "list-call", action="list")]),
                ModelResponse(
                    tool_calls=[
                        _call("job", "wait-call", action="wait", job_id=job_id, wait_timeout=0),
                    ],
                ),
                ModelResponse(content="The retained report was retrieved."),
            ],
        )
        with tool_runtime_context(context), human_message_signal_context(signal):
            second_pending = asyncio.create_task(run_turn("Check the earlier report"))
            await asyncio.wait_for(model.list_observed.wait(), 2)
            assert (await runtime.lookup(job_id, owner=owner, depth=0)).status == "running"
            assert job_id in str(model.seen_messages[-1].content)
            release.set()
            ready = await _wait_until_ready(runtime, job_id, owner=owner)
            assert ready.status == "completed"
            model.allow_wait.set()
            second = await asyncio.wait_for(second_pending, 2)

        assert second.tools is not None
        list_result = next(tool.result for tool in second.tools if tool.tool_call_id == "list-call")
        wait_result = next(tool.result for tool in second.tools if tool.tool_call_id == "wait-call")
        assert job_id in cast("str", list_result)
        assert wait_result == "durable report"
        assert _provider_tool_content(model, "wait-call") == "durable report"
        assert executions == 1
        assert (await runtime.lookup(job_id, owner=owner, depth=0)).wait_acknowledged
        assert await runtime.pending_outcomes() == []
    finally:
        release.set()
        model.allow_wait.set()
        for pending in (first_pending, second_pending):
            if pending is not None and not pending.done():
                pending.cancel()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
        storage.close()
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True], ids=["success", "tool-failure"])
async def test_streaming_turn_consumes_completion_only_after_active_text_boundary(  # noqa: C901, PLR0915 - Real SDK stream lifecycle.
    tmp_path: Path,
    fails: bool,
) -> None:
    """A result completed mid-stream remains pending until the real response driver joins it."""
    started, release = asyncio.Event(), asyncio.Event()
    executions = 0

    async def slow_tool() -> str:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        if fails:
            msg = "report generation failed"
            raise RuntimeError(msg)
        return "streamed report"

    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    storage_file = str(tmp_path / "stream.db")

    def storage_factory() -> SqliteDb:
        return SqliteDb(db_file=storage_file)

    model = _ActiveTextBarrierModel(
        [
            ModelResponse(tool_calls=[_call("slow_tool", "original-call", wait_timeout=0)]),
            ModelResponse(content="Independent work done."),
        ],
    )
    install_tool_job_execution(model)
    storage = storage_factory()
    actor = Agent(id="leader", model=model, tools=[slow_tool, JobTools(paths, owner)], db=storage, telemetry=False)
    scope = ScopeSessionContext(
        HistoryScope(kind="agent", scope_id="leader"),
        storage,
        None,
        session_id=context.session_id,
        storage_factory=storage_factory,
    )
    responses: list[RunOutput] = []

    async def attempt(
        _run: TurnRunState,
        state: DynamicContinuationRunState,
    ) -> AsyncIterator[str | AttemptResolved]:
        response = None
        text_parts: list[str] = []
        events = actor.arun(
            state.active_prompt,
            session_id=context.session_id,
            user_id=owner.requester_id,
            stream=True,
            stream_events=True,
            yield_run_output=True,
        )
        async for event in events:
            if isinstance(event, RunContentEvent) and event.content:
                text = str(event.content)
                text_parts.append(text)
                yield text
            elif isinstance(event, RunOutput):
                response = event
        assert response is not None
        responses.append(response)
        text = "".join(text_parts) or str(response.content or "")
        yield AttemptResolved(
            CompletedAttempt(
                response_text=text,
                replayable_text=text,
                has_visible_content=bool(text),
                session_id=response.session_id,
                attempt_run_id=response.run_id,
            ),
        )

    chunks: list[str | BackgroundWaitChunk] = []

    async def drive_stream() -> None:
        async for chunk in stream_response_turn(
            _ctx(
                entity_label="leader",
                session_id=context.session_id,
                room_id=owner.room_id,
                thread_id=owner.resolved_thread_id,
                requester_id=owner.requester_id,
                background_tool_jobs=True,
            ),
            _streaming_adapter(_AdapterLog(), attempt, open_scope=lambda: nullcontext(scope)),
            TurnSinks(),
            continuation=_continuation("Start the report"),
        ):
            chunks.append(chunk)  # noqa: PERF401 - Preserve incremental stream observation.

    pending = None
    try:
        with tool_runtime_context(context):
            pending = asyncio.create_task(drive_stream())
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(model.text_started.wait(), 2)
            jobs = await runtime.list_jobs(owner=owner, depth=0)
            assert len(jobs) == 1
            job_id = jobs[0].job_id
            model.responses.extend(
                [
                    ModelResponse(
                        tool_calls=[
                            _call("job", "retrieve-call", action="wait", job_id=job_id, wait_timeout=0),
                        ],
                    ),
                    ModelResponse(content="The completed work was observed."),
                ],
            )

            release.set()
            ready = await _wait_until_ready(runtime, job_id, owner=owner)
            assert ready.status == ("failed" if fails else "completed")
            assert not ready.wait_acknowledged
            assert len(await runtime.pending_outcomes()) == 1
            session = storage.get_session(context.session_id, session_type=SessionType.AGENT)
            assert session is None or not any(
                "mindroom_tool_job_receipts" in (run.session_state or {}) for run in session.runs or []
            )
            assert not any(isinstance(chunk, BackgroundWaitChunk) for chunk in chunks)

            model.release_text.set()
            await asyncio.wait_for(pending, 3)

        assert executions == 1
        saved = await runtime.lookup(job_id, owner=owner, depth=0)
        assert saved.wait_acknowledged
        assert await runtime.pending_outcomes() == []
        retrieval = next(
            response
            for response in responses
            if response.tools and any(tool.tool_call_id == "retrieve-call" for tool in response.tools)
        )
        receipt = (retrieval.session_state or {})["mindroom_tool_job_receipts"]
        assert receipt[f"{retrieval.run_id}:retrieve-call"]["job_id"] == job_id
        assert receipt[f"{retrieval.run_id}:retrieve-call"]["generation"] == 0
        persisted = storage.get_run(retrieval.run_id)
        assert persisted is not None
        assert (persisted.session_state or {})["mindroom_tool_job_receipts"] == receipt
        retrieval_tool = next(tool for tool in retrieval.tools or [] if tool.tool_call_id == "retrieve-call")
        if fails:
            assert retrieval_tool.tool_call_error
            assert "report generation failed" in str(retrieval_tool.result)
            assert "report generation failed" in _provider_tool_content(model, "retrieve-call")
        else:
            assert retrieval_tool.result == "streamed report"
            assert _provider_tool_content(model, "retrieve-call") == "streamed report"
        visible = "".join(chunk for chunk in chunks if isinstance(chunk, str))
        assert "Independent work done." in visible
        assert "The completed work was observed." in visible
    finally:
        release.set()
        model.release_text.set()
        if pending is not None and not pending.done():
            pending.cancel()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        storage.close()
        register_background_runtime(paths, None)
        await runtime.shutdown()
