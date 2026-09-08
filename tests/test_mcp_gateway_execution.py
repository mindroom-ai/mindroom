"""HTTP admission follows real native work and cleanup after cancellation."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.tools import Toolkit
from starlette.applications import Starlette
from starlette.routing import Route
from structlog.testing import capture_logs

from mindroom import agents
from mindroom.mcp_gateway import server
from mindroom.mcp_gateway import toolkits as gateway_toolkits
from mindroom.mcp_gateway import tools as gateway
from mindroom.tool_system.runtime_context import get_tool_runtime_context, get_worker_runtime_context
from mindroom.tool_system.worker_routing import get_tool_execution_identity
from tests.test_mcp_gateway_server import _HEADERS, _authenticate, _call, _cancel, _client
from tests.test_mcp_gateway_tools import context  # noqa: F401

if TYPE_CHECKING:
    from starlette.requests import Request

    from mindroom.api.personal_agent import PersonalAgentContext

pytestmark = pytest.mark.asyncio


def _code(response: httpx.Response) -> str | None:
    return response.json()["result"]["structuredContent"].get("error", {}).get("code")


async def _wait(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5)


@pytest.mark.parametrize(
    ("phase", "interruption"),
    [
        ("build", "cancelled"),
        ("connect", "cancelled"),
        ("body", "cancelled"),
        ("close", "cancelled"),
        ("body", "timeout"),
    ],
)
async def test_native_capacity_survives_response_until_cleanup_finishes(
    context: PersonalAgentContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    interruption: str,
) -> None:
    """A cancelled call keeps its slot, identity, and close owner until native work exits."""
    started, release = threading.Event(), threading.Event()
    close_started, close_release, closed = threading.Event(), threading.Event(), threading.Event()
    bodies: list[str] = []

    def block(stage: str) -> None:
        assert get_tool_runtime_context() is None
        assert get_tool_execution_identity() == context.execution_identity
        worker = get_worker_runtime_context()
        assert worker is not None
        assert worker.config is context.config
        if phase == stage:
            started.set()
            assert release.wait(5)

    class BlockingToolkit(Toolkit):
        def __init__(self) -> None:
            block("build")
            super().__init__(name="calculator", tools=[self.work])
            self._requires_connect = True

        def connect(self) -> None:
            block("connect")

        def work(self) -> str:
            bodies.append("work")
            block("body")
            return "done"

        def close(self) -> None:
            close_started.set()
            block("close")
            assert close_release.wait(5)
            closed.set()

    monkeypatch.setattr(agents, "build_agent_toolkit", lambda *_args, **_kwargs: BlockingToolkit())

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        if _arguments.get("query") == "probe":
            return {"ok": True}
        return await gateway.invoke_tool(context, toolkit="calculator", function="work", arguments={})

    async with _client(
        dispatch,
        max_active_calls=1,
        deadline_seconds=0.5 if interruption == "timeout" else 60,
    ) as client:
        first = asyncio.create_task(client.post("/mcp", json=_call(12)))
        try:
            await _wait(started)
            if interruption == "cancelled":
                await client.post("/mcp", json=_cancel(12), headers={"Authorization": "Bearer bob"})
                await client.post("/mcp", json=_cancel("12"))
                assert not first.done()
                await client.post("/mcp", json=_cancel(12))
            assert _code(await asyncio.wait_for(first, 2)) == interruption
            assert _code(await client.post("/mcp", json=_call(13, arguments={"query": "probe"}))) == "busy"
            assert _code(await client.post("/mcp", json=_call(12))) == "duplicate_request"
            release.set()
            await _wait(close_started)
            await client.post("/mcp", json=_cancel(12))
            await client.post("/mcp", json=_cancel(12))
            assert not closed.is_set()
            assert _code(await client.post("/mcp", json=_call(13, arguments={"query": "probe"}))) == "busy"
            assert bodies == (["work"] if phase in {"body", "close"} else [])
            close_release.set()
            await gateway_toolkits.drain_gateway_tool_cleanup()
            assert closed.is_set()
            # The original typed request ID becomes reusable only after its owner exits.
            response = await client.post("/mcp", json=_call(12))
            assert response.json()["result"]["structuredContent"] == {"result": "done"}
        finally:
            release.set()
            close_release.set()
            await asyncio.gather(first, return_exceptions=True)
            await gateway_toolkits.drain_gateway_tool_cleanup()


@pytest.mark.parametrize("phase", ["metadata", "entry", "plugins"])
async def test_cancelled_discovery_offload_keeps_capacity_until_thread_exits(
    context: PersonalAgentContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Cancelling offloaded resolution cannot admit more work or start a later body."""
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    bodies: list[str] = []
    target = {"metadata": "_entries", "entry": "_require_entry", "plugins": "load_plugins"}[phase]
    original = getattr(gateway, target)

    def block(*args: object, **kwargs: object) -> object:
        started.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    def work() -> str:
        bodies.append("work")
        return "done"

    monkeypatch.setattr(gateway, target, block)
    monkeypatch.setattr(
        agents,
        "build_agent_toolkit",
        lambda *_args, **_kwargs: Toolkit(name="calculator", tools=[work]),
    )

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        if _arguments.get("query") == "probe":
            return {"ok": True}
        if phase == "metadata":
            return await gateway.search_tools(context)
        return await gateway.invoke_tool(context, toolkit="calculator", function="work", arguments={})

    async with _client(dispatch, max_active_calls=1) as client:
        first = asyncio.create_task(client.post("/mcp", json=_call(1)))
        try:
            await _wait(started)
            await client.post("/mcp", json=_cancel(1))
            assert _code(await asyncio.wait_for(first, 2)) == "cancelled"
            assert _code(await client.post("/mcp", json=_call(2, arguments={"query": "probe"}))) == "busy"
            release.set()
            await _wait(finished)
            assert bodies == []
            async with asyncio.timeout(2):
                while _code(response := await client.post("/mcp", json=_call(1))) == "duplicate_request":  # noqa: ASYNC110
                    await asyncio.sleep(0)
            assert _code(response) is None
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await _wait(finished)
            await gateway_toolkits.drain_gateway_tool_cleanup()


async def test_repeated_cancel_preserves_async_cleanup_and_other_server_capacity(
    context: PersonalAgentContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async bodies still cancel promptly, while close and admission remain server-owned."""
    started, close_started, release, closed = (asyncio.Event() for _ in range(4))
    cleanup_cancelled = False

    class AsyncToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.work])
            self._requires_connect = True

        async def connect(self) -> None:  # ty: ignore[invalid-method-override]
            pass

        async def work(self) -> str:
            started.set()
            await asyncio.Event().wait()
            return "never"

        async def close(self) -> None:  # ty: ignore[invalid-method-override]
            nonlocal cleanup_cancelled
            close_started.set()
            try:
                await release.wait()
                closed.set()
            except asyncio.CancelledError:
                cleanup_cancelled = True
                raise

    monkeypatch.setattr(agents, "build_agent_toolkit", lambda *_args, **_kwargs: AsyncToolkit())

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return await gateway.invoke_tool(context, toolkit="calculator", function="work", arguments={})

    async def probe(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    async with _client(dispatch, max_active_calls=1) as client:
        first = asyncio.create_task(client.post("/mcp", json=_call(1)))
        try:
            await asyncio.wait_for(started.wait(), 2)
            await client.post("/mcp", json=_cancel(1))
            assert _code(await asyncio.wait_for(first, 2)) == "cancelled"
            await asyncio.wait_for(close_started.wait(), 2)
            await client.post("/mcp", json=_cancel(1))
            await client.post("/mcp", json=_cancel(1))
            assert _code(await client.post("/mcp", json=_call(2))) == "busy"
            assert not cleanup_cancelled
            async with _client(probe, max_active_calls=1) as other:
                assert _code(await other.post("/mcp", json=_call(1))) is None
            release.set()
            await gateway_toolkits.drain_gateway_tool_cleanup()
            assert closed.is_set()
            assert not cleanup_cancelled
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await gateway_toolkits.drain_gateway_tool_cleanup()


async def test_cancelled_native_cleanup_failure_is_safely_logged_and_releases_capacity(
    context: PersonalAgentContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed retained cleanup reports only its type and still releases its server slot."""
    started, close_started, close_release = (asyncio.Event() for _ in range(3))
    provider_detail = "private cleanup provider detail"

    class FailingCloseToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.work])
            self._requires_connect = True

        async def connect(self) -> None:  # ty: ignore[invalid-method-override]
            pass

        async def work(self) -> str:
            started.set()
            await asyncio.Event().wait()
            return "never"

        async def close(self) -> None:  # ty: ignore[invalid-method-override]
            close_started.set()
            await close_release.wait()
            raise RuntimeError(provider_detail)

    monkeypatch.setattr(agents, "build_agent_toolkit", lambda *_args, **_kwargs: FailingCloseToolkit())

    async def dispatch(_request: Request, _name: str, arguments: dict[str, object]) -> dict[str, object]:
        if arguments.get("query") == "probe":
            return {"ok": True}
        return await gateway.invoke_tool(context, toolkit="calculator", function="work", arguments={})

    with capture_logs() as logs:
        async with _client(dispatch, max_active_calls=1) as client:
            first = asyncio.create_task(client.post("/mcp", json=_call(1)))
            try:
                await asyncio.wait_for(started.wait(), 2)
                await client.post("/mcp", json=_cancel(1))
                assert _code(await asyncio.wait_for(first, 2)) == "cancelled"
                await asyncio.wait_for(close_started.wait(), 2)
                assert _code(await client.post("/mcp", json=_call(2, arguments={"query": "probe"}))) == "busy"
                close_release.set()
                await gateway_toolkits.drain_gateway_tool_cleanup()
                response = await client.post("/mcp", json=_call(2, arguments={"query": "probe"}))
                assert _code(response) is None
            finally:
                close_release.set()
                await asyncio.gather(first, return_exceptions=True)
                await gateway_toolkits.drain_gateway_tool_cleanup()

    failures = [entry for entry in logs if entry.get("event") == "mcp_gateway_tool_cleanup_failed"]
    assert failures == [
        {
            "error_type": "RuntimeError",
            "event": "mcp_gateway_tool_cleanup_failed",
            "log_level": "warning",
        },
    ]
    assert provider_detail not in str(logs)


async def test_server_shutdown_drains_cancelled_metadata_work(
    context: PersonalAgentContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown waits for cancelled discovery threads that own no toolkit cleanup."""
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    running, stopping = asyncio.Event(), asyncio.Event()
    original = gateway._entries

    def block(*args: object, **kwargs: object) -> object:
        started.set()
        assert release.wait(5)
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(gateway, "_entries", block)

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        if _arguments.get("query") == "probe":
            return {"ok": True}
        return await gateway.search_tools(context)

    transport = server.GatewayServer(
        authenticate=_authenticate,
        dispatch=dispatch,
        public_url="https://portal.example.org",
    )
    app = Starlette(routes=[Route("/mcp", endpoint=transport, methods=["POST"])])

    async def lifespan() -> None:
        async with transport.run():
            running.set()
            await stopping.wait()

    owner = asyncio.create_task(lifespan())
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://portal.example.org",
            headers=_HEADERS,
        ) as client:
            await asyncio.wait_for(running.wait(), 2)
            first = asyncio.create_task(client.post("/mcp", json=_call(1)))
            await _wait(started)
            await client.post("/mcp", json=_cancel(1))
            assert _code(await first) == "cancelled"
            stopping.set()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(owner), 0.05)
            assert _code(await client.post("/mcp", json=_call(2, arguments={"query": "probe"}))) == "busy"
            release.set()
            await asyncio.wait_for(owner, 2)
            assert finished.is_set()
    finally:
        stopping.set()
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
