"""Shared SDK connection acquisitions and retiring transport generations."""

from __future__ import annotations

import asyncio
import threading

import anyio
import pytest
from agno.agent import Agent
from agno.agent import _init as agent_init
from agno.team import Team
from agno.team import _init as team_init
from agno.tools import Toolkit

from mindroom.tool_jobs import agno_compat_resources
from mindroom.tool_jobs.agno_compat_resources import install_execution_resource_bindings
from mindroom.tool_jobs.resources import (
    ExecutionResources,
    bind_execution_resources,
    connect_async_execution_resource,
    disconnect_async_execution_resource,
    execution_resources,
)


@pytest.mark.asyncio
async def test_concurrent_compatibility_installation_wraps_sdk_bindings_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent actor construction cannot capture and wrap a partially installed binding."""
    first_entered = threading.Event()
    concurrent_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def bindings(connect: object, disconnect: object) -> tuple[object, object]:
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(2)
        elif call_number == 2:
            concurrent_entered.set()
        return connect, disconnect

    def install(*, started: threading.Event | None = None) -> None:
        if started is not None:
            started.set()
        agno_compat_resources.install_execution_resource_bindings()

    monkeypatch.setattr(agno_compat_resources, "_INSTALLED", False)
    monkeypatch.setattr(agno_compat_resources, "_sync_bindings", bindings)
    monkeypatch.setattr(agno_compat_resources, "_async_bindings", bindings)
    first = asyncio.create_task(asyncio.to_thread(install))
    second: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(first_entered.wait, 2)
        second = asyncio.create_task(asyncio.to_thread(install, started=second_started))
        assert await asyncio.to_thread(second_started.wait, 2)
        assert not await asyncio.to_thread(concurrent_entered.wait, 0.1)
    finally:
        release_first.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)

    assert calls == 4


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


class MCPTools(Toolkit):
    """Test transport following the SDK's MRO admission contract, without MCP I/O."""

    def __init__(self, name: str) -> None:
        self.initialized = False
        self.connects = 0
        self.closes = 0
        self.connect_started = asyncio.Event()
        self.allow_connect = asyncio.Event()
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.allow_connect.set()
        self.allow_close.set()
        super().__init__(name=name)

    async def connect(self) -> None:  # ty: ignore[invalid-method-override] - SDK MCPTools uses async Toolkit overrides.
        """Expose a deterministic barrier after acquisition but before readiness."""
        self.connect_started.set()
        await self.allow_connect.wait()
        self.connects += 1
        self.initialized = True

    async def close(self) -> None:  # ty: ignore[invalid-method-override] - SDK MCPTools uses async Toolkit overrides.
        """Expose a deterministic retirement barrier and count physical closes."""
        assert self.initialized
        self.close_started.set()
        await self.allow_close.wait()
        self.closes += 1
        self.initialized = False


async def _connect_mcp_actor(actor: Agent | Team) -> None:
    if isinstance(actor, Team):
        await team_init._connect_mcp_tools(actor)
    else:
        await agent_init.connect_mcp_tools(actor)


async def _disconnect_mcp_actor(actor: Agent | Team) -> None:
    if isinstance(actor, Team):
        await team_init._disconnect_mcp_tools(actor)
    else:
        await agent_init.disconnect_mcp_tools(actor)


@pytest.mark.asyncio
@pytest.mark.parametrize("teams", [False, True])
@pytest.mark.parametrize("detached_child", [False, True])
async def test_cancelled_teardown_drains_every_sdk_connection(*, teams: bool, detached_child: bool) -> None:
    """Repeated cancellation cannot abandon later immediate or deferred closes."""
    install_execution_resource_bindings()
    toolkits = [MCPTools("first"), MCPTools("second")]
    for toolkit in toolkits:
        toolkit.allow_close.clear()
    actor = Team(id="actor", members=[], tools=toolkits) if teams else Agent(id="actor", tools=toolkits)
    resources = ExecutionResources()
    child = resources.acquire() if detached_child else None

    with bind_execution_resources(resources):
        await _connect_mcp_actor(actor)
        if child is not None:
            await _disconnect_mcp_actor(actor)
            await resources.release_parent()
            closing = asyncio.create_task(child.release())
        else:
            closing = asyncio.create_task(_disconnect_mcp_actor(actor))
        try:
            for toolkit in toolkits:
                await asyncio.wait_for(toolkit.close_started.wait(), 2)
                assert not closing.done()
                closing.cancel()
                toolkit.allow_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 2)
            assert actor._mcp_tools_initialized_on_run == []
            assert all(not toolkit.initialized for toolkit in toolkits)
            assert all(toolkit.connects == toolkit.closes == 1 for toolkit in toolkits)
            if child is not None:
                await child.release()
        finally:
            for toolkit in toolkits:
                toolkit.allow_close.set()
            await asyncio.gather(closing, return_exceptions=True)
            for toolkit in toolkits:
                if toolkit.initialized:
                    await disconnect_async_execution_resource(toolkit)
            if child is None:
                await resources.release_parent()


@pytest.mark.asyncio
@pytest.mark.parametrize("teams", [False, True])
async def test_cancelled_retirement_admission_keeps_sdk_cleanup_balanced(*, teams: bool) -> None:
    """Cancellation cannot record a phantom acquisition or break later admission."""
    install_execution_resource_bindings()
    toolkit, independent = MCPTools("retiring"), MCPTools("independent")
    toolkit.allow_close.clear()
    attempted = asyncio.Event()
    first = Team(id="first", members=[], tools=[toolkit]) if teams else Agent(id="first", tools=[toolkit])
    second_tools = [independent]
    second = Team(id="second", members=[], tools=second_tools) if teams else Agent(id="second", tools=second_tools)

    async def retire() -> None:
        async with execution_resources():
            await _connect_mcp_actor(first)
            await _disconnect_mcp_actor(first)

    async def cancelled_actor() -> None:
        async with execution_resources():
            try:
                await _connect_mcp_actor(second)
                second.tools = [independent, toolkit]
                attempted.set()
                await _connect_mcp_actor(second)
            finally:
                await _disconnect_mcp_actor(second)

    first_task = asyncio.create_task(retire())
    second_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(toolkit.close_started.wait(), 2)
        second_task = asyncio.create_task(cancelled_actor())
        await asyncio.wait_for(attempted.wait(), 2)
        second_task.cancel()
        toolkit.allow_close.set()
        await asyncio.wait_for(first_task, 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(second_task, 2)
        assert second._mcp_tools_initialized_on_run == []
        assert not independent.initialized
        assert independent.connects == independent.closes == 1
        assert not toolkit.initialized
        assert toolkit.closes == toolkit.connects
        completed_generations = toolkit.connects
        async with execution_resources():
            await _connect_mcp_actor(first)
            assert toolkit.initialized
            assert toolkit.connects == completed_generations + 1
            await _disconnect_mcp_actor(first)
        assert not toolkit.initialized
        assert toolkit.closes == toolkit.connects
    finally:
        toolkit.allow_close.set()
        await asyncio.gather(first_task, *([second_task] if second_task is not None else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("teams", [False, True])
async def test_cancelled_acquired_connection_still_releases_sdk_reference(*, teams: bool) -> None:
    """Cancellation during connect completion retains the acquired cleanup obligation."""
    install_execution_resource_bindings()
    toolkit = MCPTools("connecting")
    toolkit.allow_connect.clear()
    actor = Team(id="actor", members=[], tools=[toolkit]) if teams else Agent(id="actor", tools=[toolkit])

    async def run() -> None:
        async with execution_resources():
            try:
                await _connect_mcp_actor(actor)
            finally:
                await _disconnect_mcp_actor(actor)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(toolkit.connect_started.wait(), 2)
        task.cancel()
        toolkit.allow_connect.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert actor._mcp_tools_initialized_on_run == []
        assert not toolkit.initialized
        assert toolkit.connects == toolkit.closes == 1
    finally:
        toolkit.allow_connect.set()
        await asyncio.gather(task, return_exceptions=True)
