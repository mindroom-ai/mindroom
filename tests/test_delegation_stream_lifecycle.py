"""Delegation stream owners finish continuations before releasing the run."""

from __future__ import annotations

import asyncio
import gc
from typing import TYPE_CHECKING, cast

import pytest

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.agents import create_agent
from mindroom.approval.resume import ResumeRunner
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.delegation import execution
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState
from mindroom.history.storage import new_scope_session
from mindroom.history.types import HistoryScope
from mindroom.llm.events import ContentDelta, RunFinished
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.store import persist_to
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.agent_loop.helpers import collect
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.test_delegation_direct_audit import _identity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.approval.resume import ResumeDecisions, ResumeEvents
    from mindroom.llm.events import RunEvent

_PAUSED = RunRecord(
    run_id="parent-run",
    session_id="session-1",
    status=RunStatus.paused,
    metadata={DELEGATION_STATE_KEY: DelegationState().to_dict()},
)


async def _paused_run() -> AsyncIterator[RunEvent]:
    yield RunFinished(_PAUSED)


async def _unused_continue(paused: RunRecord, _decisions: ResumeDecisions, _on_event: ResumeEvents | None) -> RunRecord:
    return paused


def _stream(**options: object) -> AsyncIterator[RunEvent]:
    return execution.drive_delegation_stream(
        _paused_run(),
        store=None,
        session=new_scope_session(session_id="session-1", scope_id="leader", is_team=False),
        continue_parent=_unused_continue,
        agent_name="leader",
        run_child=cast("Callable", None),
        config=cast("Callable", None),  # type: ignore[arg-type]
        runtime_paths=cast("Callable", None),  # type: ignore[arg-type]
        execution_identity=None,
        **options,  # type: ignore[arg-type]
    )


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
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()

    async def drive(_response: RunRecord, *, on_event: Callable[[object], None], **_kwargs: object) -> RunRecord:
        try:
            on_event(ContentDelta("partial output"))
            if not already_failed:
                await asyncio.Future()
        finally:
            message = "Driver failed"
            raise RuntimeError(message)

    async def consume() -> None:
        stream = _stream()
        try:
            assert await anext(stream) == ContentDelta("partial output")
            if consumer_failure is not None:
                message = "Consumer stopped"
                raise consumer_failure(message)
        finally:
            await stream.aclose()  # type: ignore[attr-defined]

    monkeypatch.setattr(execution, "_drive_delegations", drive)
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

    async def drive(_response: RunRecord, *, on_event: Callable[[object], None], **_kwargs: object) -> RunRecord:
        on_event(ContentDelta("partial output"))
        message = "Driver failed"
        raise RuntimeError(message)

    monkeypatch.setattr(execution, "_drive_delegations", drive)
    stream = _stream()
    assert await anext(stream) == ContentDelta("partial output")
    with pytest.raises(RuntimeError, match="Driver failed"):
        await anext(stream)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_cancellation", [False, True])
@pytest.mark.parametrize("close_only", [False, True])
async def test_cancelled_stream_drains_its_continuation_task(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    repeat_cancellation: bool,
    close_only: bool,
) -> None:
    """Closing the outer stream must keep ownership through child-task cleanup."""
    tasks: list[asyncio.Task[RunRecord]] = []
    chunk_seen = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    async def drive(response: RunRecord, *, on_event: Callable[[object], None], **_kwargs: object) -> RunRecord:
        tasks.append(cast("asyncio.Task[RunRecord]", asyncio.current_task()))
        try:
            on_event(ContentDelta("continued chunk"))
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleanup_finished.set()
        return response

    monkeypatch.setattr(execution, "_drive_delegations", drive)
    stream = _stream()

    async def consume() -> None:
        try:
            assert await anext(stream) == ContentDelta("continued chunk")
            chunk_seen.set()
            if not close_only:
                await asyncio.Future()
        finally:
            await stream.aclose()  # type: ignore[attr-defined]

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
async def test_a_consumer_failure_during_the_continuation_stores_the_cancelled_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    """A delivery failure while the parent continues closes the resumed run, which stores its cancelled record.

    The continuation's events reach the consumer as they happen; when the consumer fails on one, the resume is
    closed where it stands and its terminal record lands before the failure propagates.
    """
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["helper"]),
            "helper": AgentConfig(display_name="Helper"),
        },
        defaults={"tools": []},
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    model = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate", agent_name="missing", task="Do work")),
            answer("Parent continued"),
        ],
    )
    ScriptedAgents({"leader": model}).install(monkeypatch)
    store = create_session_storage("leader", config, paths, identity)
    build = create_agent("leader", config, paths, identity, history_storage=store, supports_native_tool_approval=True)
    session = new_scope_session(session_id=identity.session_id or "", scope_id="leader", is_team=False)
    ctx = RunContext(run_id="parent-run", session_id=session.session_id, user_id=identity.requester_id)
    request = [prompt_for_turn(build), Message(role="user", content="Delegate")]

    async def unused_child(**_kwargs: object) -> str:
        pytest.fail("Rejected delegation must not execute a child")

    def deliver(event: object) -> None:
        if isinstance(event, ContentDelta) and event.text:
            message = "Consumer stopped"
            raise failure(message)

    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        _events, paused = await collect(run_agent(build.spec, request, ctx=ctx, persist=persist_to(store, session)))
        assert paused.status is RunStatus.paused
        runner = ResumeRunner(
            build=build,
            config=config,
            runtime_paths=paths,
            agent_name="leader",
            model_name="test-model",
            store=store,
            session=session,
            scope=HistoryScope(kind="agent", scope_id="leader"),
            user_id=identity.requester_id,
        )
        with pytest.raises(failure, match="Consumer stopped"):
            await execution._drive_delegations(
                paused,
                store=store,
                session=session,
                continue_parent=runner.continue_run,
                agent_name="leader",
                run_child=unused_child,
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
                on_event=deliver,
            )

    persisted = store.load_session(session.session_id, "agent")
    assert persisted is not None
    assert [(run.run_id, run.status) for run in persisted.runs] == [("parent-run", RunStatus.cancelled)]
