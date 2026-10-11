"""Delegation stream owners finish continuations before releasing the run."""

from __future__ import annotations

import asyncio
import gc
from typing import TYPE_CHECKING, cast

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunContentEvent, RunOutput
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage, get_agent_session
from mindroom.agents import apply_tool_approval_capability
from mindroom.agno_compat_session_persistence import drain_agent_cancellation
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation import execution
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("already_failed", [False, True])
@pytest.mark.parametrize("consumer_failure", [None, RuntimeError, asyncio.CancelledError])
async def test_closed_stream_observes_driver_failure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    already_failed: bool,
    consumer_failure: type[BaseException] | None,
) -> None:
    """Early close retrieves driver errors without replacing the consumer's outcome."""
    response = RunOutput(
        run_id="parent-run",
        status=RunStatus.paused,
        metadata={DELEGATION_STATE_KEY: DelegationState().to_dict()},
    )
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()

    async def drive(
        _entity: Agent,
        _response: RunOutput,
        *,
        on_event: Callable[[object], None],
        **_kwargs: object,
    ) -> RunOutput:
        try:
            on_event("partial output")
            if not already_failed:
                await asyncio.Future()
        finally:
            message = "Driver failed"
            raise RuntimeError(message)

    async def paused_run() -> AsyncIterator[RunOutput]:
        yield response

    async def consume() -> None:
        stream = execution.drive_delegation_stream(Agent(telemetry=False), paused_run())
        try:
            assert await anext(stream) == "partial output"
            if consumer_failure is not None:
                message = "Consumer stopped"
                raise consumer_failure(message)
        finally:
            await stream.aclose()

    monkeypatch.setattr(execution, "drive_delegations", drive)
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        if consumer_failure is None:
            await consume()
        else:
            with pytest.raises(consumer_failure, match="Consumer stopped"):
                await consume()
        gc.collect()
        assert unhandled == []
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_exhausted_stream_propagates_driver_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrieving a task during cleanup must not hide failures from active consumers."""
    response = RunOutput(
        run_id="parent-run",
        status=RunStatus.paused,
        metadata={DELEGATION_STATE_KEY: DelegationState().to_dict()},
    )

    async def drive(
        _entity: Agent,
        _response: RunOutput,
        *,
        on_event: Callable[[object], None],
        **_kwargs: object,
    ) -> RunOutput:
        on_event("partial output")
        message = "Driver failed"
        raise RuntimeError(message)

    async def paused_run() -> AsyncIterator[RunOutput]:
        yield response

    monkeypatch.setattr(execution, "drive_delegations", drive)
    stream = execution.drive_delegation_stream(Agent(telemetry=False), paused_run())
    assert await anext(stream) == "partial output"
    with pytest.raises(RuntimeError, match="Driver failed"):
        await anext(stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_cancellation", [False, True])
@pytest.mark.parametrize("close_only", [False, True])
async def test_cancelled_stream_drains_its_continuation_task(  # noqa: C901, PLR0915
    monkeypatch: pytest.MonkeyPatch,
    repeat_cancellation: bool,
    close_only: bool,
) -> None:
    """Closing the outer stream must keep ownership through child-task cleanup."""
    tasks: list[asyncio.Task[RunOutput]] = []
    chunk_seen = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    response = RunOutput(
        run_id="parent-run",
        status=RunStatus.paused,
        metadata={DELEGATION_STATE_KEY: DelegationState().to_dict()},
    )

    async def drive(
        _entity: Agent,
        response: RunOutput,
        *,
        on_event: Callable[[object], None],
        **_kwargs: object,
    ) -> RunOutput:
        tasks.append(cast("asyncio.Task[RunOutput]", asyncio.current_task()))
        try:
            on_event("continued chunk")
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_finished.set()
        return response

    async def paused_run() -> AsyncIterator[RunOutput]:
        yield response

    monkeypatch.setattr(execution, "drive_delegations", drive)
    stream = execution.drive_delegation_stream(Agent(telemetry=False), paused_run())

    async def consume() -> None:
        try:
            assert await anext(stream) == "continued chunk"
            chunk_seen.set()
            if not close_only:
                await asyncio.Future()
        finally:
            await stream.aclose()

    consumer = asyncio.create_task(consume())
    cleanup_waiter = asyncio.create_task(cleanup_started.wait())
    try:
        async with asyncio.timeout(5):
            await chunk_seen.wait()
            if not close_only:
                consumer.cancel()
            await asyncio.wait((consumer, cleanup_waiter), return_when=asyncio.FIRST_COMPLETED)
            assert not consumer.done(), "Outer stream released its unfinished continuation"
            assert cleanup_started.is_set()
            if repeat_cancellation:
                for _ in range(2):
                    consumer.cancel()
                    await asyncio.sleep(0)
                    assert not consumer.done(), "Repeated cancellation detached continuation cleanup"
                    assert not tasks[0].done()
            release_cleanup.set()
            if not close_only or repeat_cancellation:
                with pytest.raises(asyncio.CancelledError):
                    await consumer
            else:
                await consumer
        assert cleanup_finished.is_set()
        assert len(tasks) == 1
        assert tasks[0].done()
    finally:
        release_cleanup.set()
        cleanup_waiter.cancel()
        consumer.cancel()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(consumer, cleanup_waiter, *tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_continuation_closes_before_run_owner_exits(
    tmp_path: Path,
    failure: type[BaseException],
) -> None:
    """A delivery failure closes real Agno continuation and drains its cancellation write."""
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", delegate_to=[])},
        defaults={"tools": []},
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    identity = _identity()
    toolkit = DelegateTools("leader", [], paths, config, execution_identity=identity)
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    storage = create_session_storage("leader", config, paths, identity)
    parent = Agent(
        name="leader",
        db=storage,
        tools=[toolkit],
        telemetry=False,
        model=DelegationModel(
            id="test-parent",
            responses=[
                ModelResponse(tool_calls=[_call("run_subagent", "delegate", agent_name="missing", task="Do work")]),
                ModelResponse(content="Parent continued"),
            ],
        ),
    )

    async def unused_child(**_kwargs: object) -> str:
        pytest.fail("Rejected delegation must not execute a child")

    def deliver(event: object) -> None:
        if isinstance(event, RunContentEvent) and event.content:
            message = "Consumer stopped"
            raise failure(message)

    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
            assert response.status == RunStatus.paused
            assert response.run_id is not None
            async with drain_agent_cancellation(parent, response.run_id) as bind_owner:
                with bind_owner(), pytest.raises(failure, match="Consumer stopped"):
                    await execution.drive_delegations(
                        parent,
                        response,
                        agent_name="leader",
                        run_child=unused_child,
                        config=config,
                        runtime_paths=paths,
                        execution_identity=identity,
                        on_event=deliver,
                    )
            persisted = get_agent_session(storage, identity.session_id)
            assert persisted is not None
            assert persisted.runs is not None
            assert len(persisted.runs) == 1
            assert persisted.runs[0].run_id == response.run_id
            assert persisted.runs[0].status == RunStatus.cancelled
            assert persisted.runs[0].content == "Parent continued"
    finally:
        storage.close()
