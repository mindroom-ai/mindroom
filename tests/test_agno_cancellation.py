"""Cancellation ownership must drain only the current registered agent run."""

from __future__ import annotations

import asyncio
import threading
from contextlib import nullcontext
from typing import TYPE_CHECKING, cast

import pytest
from agno.agent import Agent
from agno.agent import _run as agent_run
from agno.agent import _session as agent_session
from agno.db.sqlite import SqliteDb
from agno.run.base import RunStatus
from agno.run.concurrency import mark_worker_managed, unmark_worker_managed
from agno.session.agent import AgentSession

from mindroom import agno_session_persistence_patch as persistence_patch
from mindroom.agent_storage import get_agent_session
from mindroom.cancellation import request_task_cancel
from tests.test_agno_session_persistence_patch import _owner_and_session, _storage

if TYPE_CHECKING:
    from pathlib import Path

    from agno.run.agent import RunOutput


@pytest.mark.asyncio
@pytest.mark.parametrize(("cancel_waiter", "process_shutdown"), [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("eager_tasks", [False, True])
async def test_agent_cancellation_scope_drains_accepted_write(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_waiter: bool,
    process_shutdown: bool,
    eager_tasks: bool,
) -> None:
    """A detached run write must finish before ownership ends, even after repeated cancellation."""
    storage = _storage(tmp_path, "cancel-owner")
    owner, session = _owner_and_session("agent", storage, "cancel-owner")
    assert isinstance(owner, Agent)
    assert isinstance(session, AgentSession)
    assert session.runs is not None
    run = session.runs[0]
    assert run.run_id is not None
    run.status = RunStatus.cancelled
    run.content = "Partial answer"
    write_started = threading.Event()
    release_write = threading.Event()
    body_finished = asyncio.Event()
    background: list[asyncio.Task[None]] = []
    original_upsert = storage.upsert_run

    def blocked_upsert(
        run: RunOutput,
        session_id: str,
        user_id: str | None = None,
        run_index: int | None = None,
    ) -> None:
        write_started.set()
        assert release_write.wait(timeout=5)
        original_upsert(run, session_id, user_id, run_index)

    async def persist(
        agent: Agent,
        run_response: RunOutput,
        session: AgentSession,
        **_kwargs: object,
    ) -> None:
        background.append(cast("asyncio.Task[None]", asyncio.current_task()))
        await agent_session.asave_session(agent, session)
        await agent_session.asave_run(agent, run_response, session.session_id)

    monkeypatch.setattr(storage, "upsert_run", blocked_upsert)
    monkeypatch.setattr(agent_run, "acleanup_and_store", persist)

    async def finish_scope() -> None:
        async with persistence_patch.drain_agent_cancellation(owner, run.run_id) as bind:
            with bind():
                agent_run._persist_cancelled_run_in_background(owner, run, session)
            assert await asyncio.to_thread(write_started.wait, 5)
            body_finished.set()

    loop = asyncio.get_running_loop()
    original_factory = loop.get_task_factory()
    if eager_tasks:
        loop.set_task_factory(asyncio.eager_task_factory)
    waiter = asyncio.create_task(finish_scope())
    try:
        async with asyncio.timeout(5):
            await body_finished.wait()
        await asyncio.sleep(0)
        assert not waiter.done(), "Run ownership ended before its accepted cancellation write"
        if cancel_waiter:
            for _ in range(2):
                request_task_cancel(waiter, process_shutdown=process_shutdown)
                await asyncio.sleep(0)
                assert not waiter.done(), "Repeated cancellation detached the accepted write"
        release_write.set()
        if cancel_waiter:
            with pytest.raises(asyncio.CancelledError):
                await waiter
        else:
            await waiter
        assert background
        assert all(task.done() for task in background)
        persisted = get_agent_session(storage, session.session_id)
        assert persisted is not None
        assert persisted.runs is not None
        assert persisted.runs[0].status == RunStatus.cancelled
        assert persisted.runs[0].content == "Partial answer"
    finally:
        release_write.set()
        await asyncio.gather(waiter, *background, return_exceptions=True)
        loop.set_task_factory(original_factory)
        storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "excluded",
    ["unregistered", "team", "workflow", "worker", "other_agent", "other_run", "no_scope"],
)
async def test_agent_cancellation_scope_preserves_unowned_background_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    excluded: str,
) -> None:
    """Nested, external, and differently owned saves keep upstream detached behavior."""
    storage = (
        SqliteDb(db_file=str(tmp_path / "unregistered.db"))
        if excluded == "unregistered"
        else _storage(tmp_path, "cancel-excluded")
    )
    persistence_patch.install_patch()
    owner, session = _owner_and_session("agent", storage, "cancel-excluded")
    assert isinstance(owner, Agent)
    assert session.runs is not None
    run = session.runs[0]
    assert run.run_id is not None
    run.status = RunStatus.cancelled
    if excluded == "team":
        owner.team_id = "parent-team"
    elif excluded == "workflow":
        owner.workflow_id = "parent-workflow"
    actual_owner = Agent(db=storage, telemetry=False) if excluded == "other_agent" else owner
    scope_run_id = "different-run" if excluded == "other_run" else run.run_id
    entered = asyncio.Event()
    release = asyncio.Event()
    background: list[asyncio.Task[None]] = []

    async def persist(*_args: object, **_kwargs: object) -> None:
        background.append(cast("asyncio.Task[None]", asyncio.current_task()))
        entered.set()
        await release.wait()

    monkeypatch.setattr(agent_run, "acleanup_and_store", persist)
    if excluded == "worker":
        mark_worker_managed(run.run_id)

    async def finish_scope() -> None:
        scope = (
            nullcontext(nullcontext)
            if excluded == "no_scope"
            else persistence_patch.drain_agent_cancellation(owner, scope_run_id)
        )
        async with scope as bind:
            with bind():
                agent_run._persist_cancelled_run_in_background(actual_owner, run, session)
            if excluded != "worker":
                await entered.wait()

    waiter = asyncio.create_task(finish_scope())
    try:
        async with asyncio.timeout(5):
            await asyncio.shield(waiter)
        if excluded == "worker":
            assert not entered.is_set()
            assert background == []
        else:
            assert entered.is_set()
            assert background
            assert not background[0].done()
    finally:
        release.set()
        await asyncio.gather(waiter, *background, return_exceptions=True)
        unmark_worker_managed(run.run_id)
        storage.close()
