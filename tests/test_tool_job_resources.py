"""Shared SDK connection acquisitions and retiring transport generations."""

from __future__ import annotations

import asyncio

import anyio
import pytest
from agno.agent import Agent
from agno.agent import _init as agent_init
from agno.team import Team
from agno.team import _init as team_init
from agno.tools import Toolkit

from mindroom.tool_jobs.agno_compat_resources import install_execution_resource_bindings
from mindroom.tool_jobs.resources import (
    connect_async_execution_resource,
    disconnect_async_execution_resource,
    execution_resources,
)


@pytest.mark.asyncio
async def test_async_admission_waits_for_retiring_connection() -> None:
    """A new response cannot use a transport whose final close already started."""
    resource = object()
    closing_started, allow_close = asyncio.Event(), asyncio.Event()
    attempted, admitted, release_second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    alive = False
    connects = closes = 0

    async def connect() -> None:
        nonlocal alive, connects
        connects += 1
        alive = True

    async def close() -> None:
        nonlocal alive, closes
        closes += 1
        if closes == 1:
            closing_started.set()
            await allow_close.wait()
        alive = False

    async def first_owner() -> None:
        async with execution_resources():
            await connect_async_execution_resource(resource, connect, close)
            await disconnect_async_execution_resource(resource)

    async def second_owner() -> None:
        async with execution_resources():
            attempted.set()
            await connect_async_execution_resource(resource, connect, close)
            admitted.set()
            await release_second.wait()
            assert alive
            await disconnect_async_execution_resource(resource)

    first = asyncio.create_task(first_owner())
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(closing_started.wait(), 2)
        second = asyncio.create_task(second_owner())
        await asyncio.wait_for(attempted.wait(), 2)
        assert not admitted.is_set()
        allow_close.set()
        await asyncio.wait_for(first, 2)
        await asyncio.wait_for(admitted.wait(), 2)
        assert alive
        assert connects == 2
        release_second.set()
        await asyncio.wait_for(second, 2)
        assert not alive
        assert closes == 2
    finally:
        allow_close.set()
        release_second.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)


class _SharedTools(Toolkit):
    _requires_connect = True

    def __init__(self) -> None:
        self.open = False
        self.connects = 0
        self.closes = 0
        super().__init__(name="shared")

    def connect(self) -> None:
        self.connects += 1
        self.open = True

    def close(self) -> None:
        assert self.open
        self.closes += 1
        self.open = False


@pytest.mark.asyncio
@pytest.mark.parametrize("detached_child", [False, True])
@pytest.mark.parametrize("teams", [False, True])
async def test_sdk_actors_share_balanced_connection_references(*, detached_child: bool, teams: bool) -> None:
    """Each SDK actor retains its own connection even within one response owner."""
    install_execution_resource_bindings()
    toolkit = _SharedTools()
    first = Team(id="first", tools=[toolkit], members=[]) if teams else Agent(id="first", tools=[toolkit])
    second = Team(id="second", tools=[toolkit], members=[]) if teams else Agent(id="second", tools=[toolkit])

    def connect(actor: Agent | Team) -> None:
        if isinstance(actor, Team):
            team_init._connect_connectable_tools(actor)
        else:
            agent_init.connect_connectable_tools(actor)

    def disconnect(actor: Agent | Team) -> None:
        if isinstance(actor, Team):
            team_init._disconnect_connectable_tools(actor)
        else:
            agent_init.disconnect_connectable_tools(actor)

    async with execution_resources() as resources:
        child = resources.acquire() if detached_child else None
        connect(first)
        connect(second)
        connect(first)  # Repeated SDK initialization must not add an unbalanced reference.
        assert toolkit.connects == 1
        disconnect(first)
        assert toolkit.open
        disconnect(second)
        assert toolkit.open is detached_child
        assert toolkit.closes == (0 if detached_child else 1)
    if child is not None:
        assert toolkit.open
        await child.release()
        await child.release()
    assert not toolkit.open
    assert toolkit.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("detached_child", [False, True])
async def test_async_acquisitions_balance_within_one_owner(*, detached_child: bool) -> None:
    """Shared async acquisitions survive the first teardown and close once."""
    resource = object()
    scope = anyio.CancelScope()
    connected_task: asyncio.Task[None] | None = None
    alive = False
    connects = closes = 0

    async def connect() -> None:
        nonlocal connected_task, alive, connects
        scope.__enter__()
        connected_task = asyncio.current_task()
        connects += 1
        alive = True

    async def close() -> None:
        nonlocal alive, closes
        assert asyncio.current_task() is connected_task
        scope.__exit__(None, None, None)
        closes += 1
        alive = False

    async with execution_resources() as resources:
        child = resources.acquire() if detached_child else None
        await connect_async_execution_resource(resource, connect, close)
        await connect_async_execution_resource(resource, connect, close)
        assert connects == 1
        await disconnect_async_execution_resource(resource)
        assert alive
        await disconnect_async_execution_resource(resource)
        assert alive is detached_child
    if child is not None:
        assert alive
        await child.release()
        await child.release()
    assert not alive
    assert closes == 1
