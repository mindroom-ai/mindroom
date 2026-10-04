"""HTTP admission follows real native work and cleanup after cancellation."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.tools import Toolkit
from starlette.applications import Starlette
from starlette.routing import Route
from structlog.testing import capture_logs

from mindroom import agents
from mindroom.credentials import CredentialsManager
from mindroom.custom_tools import google_scholar
from mindroom.custom_tools.google_drive import GoogleDriveTools
from mindroom.custom_tools.google_scholar import GoogleScholarTools
from mindroom.mcp_gateway import server
from mindroom.mcp_gateway import toolkits as gateway_toolkits
from mindroom.mcp_gateway import tools as gateway
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.runtime_context import get_tool_runtime_context, get_worker_runtime_context
from mindroom.tool_system.worker_routing import get_tool_execution_identity
from tests.test_mcp_gateway_server import _HEADERS, _authenticate, _call, _cancel, _client
from tests.test_mcp_gateway_tools import context  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.requests import Request

    from mindroom.api.connection_agents import AgentToolContext

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
    context: AgentToolContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    interruption: str,
) -> None:
    """A cancelled call keeps its slot, identity, and close owner until native work exits on the gateway pool."""
    started, release = threading.Event(), threading.Event()
    close_started, close_release, closed = threading.Event(), threading.Event(), threading.Event()
    bodies: list[str] = []
    on_gateway_pool: dict[str, bool] = {}

    def block(stage: str) -> None:
        on_gateway_pool[stage] = threading.current_thread().name.startswith("mindroom-mcp-gateway-tool")
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
            assert on_gateway_pool == dict.fromkeys(("body", "build", "close", "connect"), True)
        finally:
            release.set()
            close_release.set()
            await asyncio.gather(first, return_exceptions=True)
            await gateway_toolkits.drain_gateway_tool_cleanup()


async def test_blocking_tool_bodies_leave_the_default_executor_free(
    context: AgentToolContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow synchronous gateway tools cannot hold the threads that other offloaded work needs."""
    release = threading.Event()
    bodies: list[str] = []

    def work() -> str:
        bodies.append(threading.current_thread().name)
        assert release.wait(5)
        return "done"

    monkeypatch.setattr(
        agents,
        "build_agent_toolkit",
        lambda *_args, **_kwargs: Toolkit(name="calculator", tools=[work]),
    )
    loop = asyncio.get_running_loop()
    default_executor, replacement_executor = ThreadPoolExecutor(max_workers=2), ThreadPoolExecutor()
    loop.set_default_executor(default_executor)
    calls = [
        asyncio.create_task(gateway.invoke_tool(context, toolkit="calculator", function="work", arguments={}))
        for _ in range(2)
    ]
    try:
        async with asyncio.timeout(5):
            while len(bodies) < len(calls):  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 2) == "free"
    finally:
        release.set()
        results = await asyncio.gather(*calls, return_exceptions=True)
        loop.set_default_executor(replacement_executor)
        default_executor.shutdown(wait=True)
        replacement_executor.shutdown(wait=True)
    assert results == [{"result": "done"}, {"result": "done"}]


@pytest.mark.parametrize("phase", ["metadata", "entry", "plugins"])
async def test_cancelled_discovery_offload_keeps_capacity_until_thread_exits(
    context: AgentToolContext,  # noqa: F811
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


@pytest.mark.parametrize("async_body", [True, False], ids=["async-body", "sync-body"])
async def test_cancelled_worker_proxy_call_keeps_capacity_until_proxy_thread_exits(
    context: AgentToolContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    async_body: bool,
) -> None:
    """A worker-routed body, async or sync, keeps its slot on the gateway pool until its proxy request returns."""
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    threads: list[str] = []

    def proxy(**_kwargs: object) -> str:
        threads.append(threading.current_thread().name)
        started.set()
        try:
            assert release.wait(5)
            return "done"
        finally:
            finished.set()

    async def async_work() -> str:
        return "local"

    def sync_work() -> str:
        return "local"

    work = async_work if async_body else sync_work
    work.__name__ = "work"

    def build(*_args: object, **_kwargs: object) -> Toolkit:
        toolkit = Toolkit(name="calculator", tools=[work])
        # Like maybe_wrap_toolkit_for_sandbox_proxy, every worker-routed function gets the async proxy.
        toolkit.async_functions = {
            name: sandbox_proxy._wrap_async_proxy(
                function,
                "calculator",
                name,
                runtime_paths=context.runtime_paths,
                credentials_manager=None,
            )
            for name, function in {**toolkit.functions, **toolkit.async_functions}.items()
        }
        return toolkit

    monkeypatch.setattr(sandbox_proxy, "_call_proxy_sync", proxy)
    monkeypatch.setattr(agents, "build_agent_toolkit", build)

    async def dispatch(_request: Request, _name: str, arguments: dict[str, object]) -> dict[str, object]:
        if arguments.get("query") == "probe":
            return {"ok": True}
        return await gateway.invoke_tool(context, toolkit="calculator", function="work", arguments={})

    async with _client(dispatch, max_active_calls=1) as client:
        first = asyncio.create_task(client.post("/mcp", json=_call(1)))
        try:
            await _wait(started)
            await client.post("/mcp", json=_cancel(1))
            assert _code(await asyncio.wait_for(first, 2)) == "cancelled"
            assert _code(await client.post("/mcp", json=_call(2, arguments={"query": "probe"}))) == "busy"
            assert [name.startswith("mindroom-mcp-gateway-tool") for name in threads] == [True]
            release.set()
            await _wait(finished)
            async with asyncio.timeout(2):
                while _code(response := await client.post("/mcp", json=_call(1))) == "duplicate_request":  # noqa: ASYNC110
                    await asyncio.sleep(0)
            assert response.json()["result"]["structuredContent"] == {"result": "done"}
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await gateway_toolkits.drain_gateway_tool_cleanup()


async def test_cancelled_google_scholar_search_keeps_capacity_until_the_scrape_returns(
    context: AgentToolContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocking Google Scholar scrape runs on the gateway pool and keeps its slot after the call is cancelled."""
    started, release = threading.Event(), threading.Event()
    threads: list[str] = []

    def search(_query: str, _limit: int) -> list[dict[str, object]]:
        threads.append(threading.current_thread().name)
        started.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(google_scholar, "_search_publications", search)
    monkeypatch.setattr(agents, "build_agent_toolkit", lambda *_args, **_kwargs: GoogleScholarTools())

    async def dispatch(_request: Request, _name: str, arguments: dict[str, object]) -> dict[str, object]:
        if arguments.get("query") == "probe":
            return {"ok": True}
        return await gateway.invoke_tool(
            context,
            toolkit="calculator",
            function="search_google_scholar",
            arguments={"query": "attention"},
        )

    async with _client(dispatch, max_active_calls=1) as client:
        first = asyncio.create_task(client.post("/mcp", json=_call(1)))
        try:
            await _wait(started)
            await client.post("/mcp", json=_cancel(1))
            assert _code(await asyncio.wait_for(first, 2)) == "cancelled"
            assert _code(await client.post("/mcp", json=_call(2, arguments={"query": "probe"}))) == "busy"
            assert [name.startswith("mindroom-mcp-gateway-tool") for name in threads] == [True]
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await gateway_toolkits.drain_gateway_tool_cleanup()


async def test_cancelled_google_drive_call_keeps_capacity_until_its_body_returns(
    context: AgentToolContext,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A blocking Google Drive call runs on the gateway pool and keeps its slot after the call is cancelled."""
    started, release = threading.Event(), threading.Event()
    threads: list[str] = []

    def blocking_scope_check() -> str:
        threads.append(threading.current_thread().name)
        started.set()
        assert release.wait(5)
        return '{"error": "unused"}'

    tool = GoogleDriveTools(
        runtime_paths=context.runtime_paths,
        credentials_manager=CredentialsManager(tmp_path / "credentials"),
        worker_target=None,
    )
    monkeypatch.setattr(tool, "_write_scope_upgrade_result", blocking_scope_check)
    monkeypatch.setattr(agents, "build_agent_toolkit", lambda *_args, **_kwargs: tool)

    async def dispatch(_request: Request, _name: str, arguments: dict[str, object]) -> dict[str, object]:
        if arguments.get("query") == "probe":
            return {"ok": True}
        return await gateway.invoke_tool(
            context,
            toolkit="calculator",
            function="google_drive_create_folder",
            arguments={"name": "Plans"},
        )

    async with _client(dispatch, max_active_calls=1) as client:
        first = asyncio.create_task(client.post("/mcp", json=_call(1)))
        try:
            await _wait(started)
            await client.post("/mcp", json=_cancel(1))
            assert _code(await asyncio.wait_for(first, 2)) == "cancelled"
            assert _code(await client.post("/mcp", json=_call(2, arguments={"query": "probe"}))) == "busy"
            assert [name.startswith("mindroom-mcp-gateway-tool") for name in threads] == [True]
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            await gateway_toolkits.drain_gateway_tool_cleanup()


async def test_repeated_cancel_preserves_async_cleanup_and_other_server_capacity(
    context: AgentToolContext,  # noqa: F811
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
    context: AgentToolContext,  # noqa: F811
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
    context: AgentToolContext,  # noqa: F811
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
