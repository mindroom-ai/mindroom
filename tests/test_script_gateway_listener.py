"""Gateway-only listener that isolated workers reach instead of the primary API."""

from __future__ import annotations

import asyncio
import re
import socket
from contextlib import closing
from typing import TYPE_CHECKING, cast
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from fastapi.routing import APIRoute

from mindroom.agent_cli.session import CliAuthenticationError
from mindroom.api import main as api_main
from mindroom.api.script_gateway import serve_script_gateway_listener
from mindroom.constants import RuntimePaths
from mindroom.script_runs.models import ScriptCallRecord, ScriptCallState, ScriptToolGrant

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    from mindroom.agent_cli.protocol import AgentCliOperation
    from mindroom.agent_cli.session import TurnToolRegistry

_GATEWAY_PREFIX = "/api/script-gateway"
_AGENT_CLI_PREFIX = "/api/agent-cli"


class _ReceiptBroker:
    """Broker double that answers every authenticated receipt lookup."""

    async def accept_authenticated(self, request: object, authorization: str | None) -> ScriptCallRecord:
        raise AssertionError((request, authorization))

    async def get_authenticated(self, run_id: str, call_id: str, authorization: str | None) -> ScriptCallRecord:
        assert authorization == "Bearer capability"
        return ScriptCallRecord(
            run_id=run_id,
            call_id=call_id,
            grant=ScriptToolGrant("calculator", "add"),
            arguments_digest="digest",
            state=ScriptCallState.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            result=3,
        )


class _CliOwner:
    """Response owner double that echoes each authenticated operation."""

    async def operation(self, operation: AgentCliOperation, *, window: str | None) -> dict[str, object]:
        return {"operation": operation.operation, "window": window}


class _CliRegistry:
    """Registry double that resolves only one grant."""

    def resolve(self, authorization: str | None, *, now_ns: int) -> _CliOwner:
        assert now_ns > 0
        if authorization != "Bearer grant":
            raise CliAuthenticationError
        return _CliOwner()


def _assert_port_released(port: int) -> None:
    """Binding the port again succeeds only when no listener still holds it."""
    socket.create_server(("127.0.0.1", port)).close()


def _runtime_paths(tmp_path: Path, process_env: dict[str, str]) -> RuntimePaths:
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env=process_env,
    )


def _listener(
    tmp_path: Path,
    broker: _ReceiptBroker | None = None,
    agent_cli_registry: _CliRegistry | None = None,
) -> tuple[int, AbstractAsyncContextManager[None]]:
    """Return a free port and the gateway listener context configured to serve it."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    runtime_paths = _runtime_paths(tmp_path, {"MINDROOM_SCRIPT_GATEWAY_PORT": str(port)})
    return port, serve_script_gateway_listener(
        runtime_paths,
        host="127.0.0.1",
        broker=broker,
        log_level="INFO",
        agent_cli_registry=cast("TurnToolRegistry", agent_cli_registry),
    )


def _primary_api_requests() -> list[tuple[str, str]]:
    """Return one concrete request for every primary API route outside the worker-facing routes."""
    requests = []
    for route in api_main.app.routes:
        if not isinstance(route, APIRoute) or route.path.startswith((_GATEWAY_PREFIX, _AGENT_CLI_PREFIX)):
            continue
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        requests.extend((method, path) for method in sorted(route.methods))
    return requests


@pytest.mark.asyncio
async def test_listener_serves_only_worker_capability_routes(tmp_path: Path) -> None:
    """The listener answers gateway and Agent CLI calls with their bound owners and nothing from the primary API."""
    port, listener = _listener(tmp_path, _ReceiptBroker(), _CliRegistry())
    primary_requests = _primary_api_requests()
    assert ("GET", "/api/health") in primary_requests

    async with listener, httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        receipt = await client.get(
            f"{_GATEWAY_PREFIX}/runs/run-1/calls/call-1",
            headers={"Authorization": "Bearer capability"},
        )
        assert receipt.status_code == 200
        assert receipt.json()["result"] == 3

        cli_operation = {"operation": "tools.list"}
        listing = await client.post(
            f"{_AGENT_CLI_PREFIX}/operations",
            headers={"Authorization": "Bearer grant"},
            json=cli_operation,
        )
        assert (listing.status_code, listing.json()) == (200, {"operation": "tools.list", "window": None})
        unauthorized = await client.post(f"{_AGENT_CLI_PREFIX}/operations", json=cli_operation)
        assert unauthorized.status_code == 401
        receipt_lookup = await client.get(f"{_AGENT_CLI_PREFIX}/calls/{uuid4()}")
        assert receipt_lookup.status_code == 401

        for method, path in [
            *primary_requests,
            ("GET", "/"),
            ("GET", "/docs"),
            ("GET", "/openapi.json"),
            ("GET", "/api/script-gateway-other/calls"),
        ]:
            response = await client.request(method, path)
            assert response.status_code == 404, (method, path, response.status_code)

    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"http://127.0.0.1:{port}{_GATEWAY_PREFIX}/runs/run-1/calls/call-1")


@pytest.mark.asyncio
async def test_listener_refuses_agent_cli_calls_without_a_bound_registry(tmp_path: Path) -> None:
    """A listener started without the orchestrator's registry fails closed for every grant."""
    port, listener = _listener(tmp_path)

    async with listener, httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
        response = await client.post(
            f"{_AGENT_CLI_PREFIX}/operations",
            headers={"Authorization": "Bearer grant"},
            json={"operation": "tools.list"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_listener_closes_when_its_owner_is_cancelled_while_serving(tmp_path: Path) -> None:
    """Cancelling the owning task closes the listener and its open connections before cancellation propagates."""
    port, listener = _listener(tmp_path)
    serving = asyncio.Event()

    async def own_listener() -> None:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client, listener:
            assert (await client.get("/api/health")).status_code == 404
            serving.set()
            await asyncio.Event().wait()

    owner = asyncio.create_task(own_listener())
    await serving.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    _assert_port_released(port)


@pytest.mark.asyncio
async def test_listener_finishes_closing_when_its_owner_is_cancelled_during_shutdown(tmp_path: Path) -> None:
    """A cancellation that arrives while the listener shuts down cannot leave it serving."""
    port, listener = _listener(tmp_path)
    leaving = asyncio.Event()

    async def own_listener() -> None:
        async with listener:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                assert (await client.get("/api/health")).status_code == 404
            # The owner leaves the body without awaiting, so the cancellation below lands in listener shutdown.
            leaving.set()

    owner = asyncio.create_task(own_listener())
    await leaving.wait()
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    _assert_port_released(port)


@pytest.mark.asyncio
async def test_listener_shutdown_is_bounded_while_a_request_hangs(tmp_path: Path) -> None:
    """A client that sends part of a request and goes silent delays owner cancellation by at most the grace period."""
    port, listener = _listener(tmp_path)
    serving = asyncio.Event()

    async def own_listener() -> None:
        async with listener:
            serving.set()
            await asyncio.Event().wait()

    with patch("mindroom.api.script_gateway._LISTENER_SHUTDOWN_GRACE_SECONDS", 1):
        owner = asyncio.create_task(own_listener())
        await serving.wait()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            f"POST {_GATEWAY_PREFIX}/calls HTTP/1.1\r\nHost: gateway\r\nContent-Length: 100\r\n\r\n".encode()
            + b'{"run_id":',
        )
        await writer.drain()
        owner.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(owner), timeout=4)
            assert owner.cancelled()
            _assert_port_released(port)
            # The hung request was in flight and Uvicorn cancelled it after the grace period.
            assert (await reader.read()).startswith(b"HTTP/1.1 500")
        finally:
            writer.close()


@pytest.mark.asyncio
async def test_listener_closes_when_the_context_body_raises(tmp_path: Path) -> None:
    """A failure in the owner's body closes the listener and propagates unchanged."""
    port, listener = _listener(tmp_path)

    async def fail_inside_listener() -> None:
        async with listener:
            msg = "primary API failed"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="primary API failed"):
        await fail_inside_listener()

    _assert_port_released(port)


@pytest.mark.asyncio
async def test_listener_is_absent_without_a_configured_port(tmp_path: Path) -> None:
    """Deployments that do not opt in keep a single primary API listener."""
    runtime_paths = _runtime_paths(tmp_path, {})
    with patch("mindroom.api.script_gateway.socket.create_server") as create_server:
        async with serve_script_gateway_listener(
            runtime_paths,
            host="127.0.0.1",
            broker=None,
            log_level="INFO",
            agent_cli_registry=None,
        ):
            pass

    create_server.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_port", ["0", "65536", "-1", "gateway"])
async def test_listener_rejects_invalid_port(tmp_path: Path, raw_port: str) -> None:
    """A malformed gateway port is a startup configuration error."""
    runtime_paths = _runtime_paths(tmp_path, {"MINDROOM_SCRIPT_GATEWAY_PORT": raw_port})

    with pytest.raises(ValueError, match="MINDROOM_SCRIPT_GATEWAY_PORT"):
        async with serve_script_gateway_listener(
            runtime_paths,
            host="127.0.0.1",
            broker=None,
            log_level="INFO",
            agent_cli_registry=None,
        ):
            pass


def test_primary_api_still_serves_gateway_and_general_routes() -> None:
    """The dedicated listener is additive; the primary API route table is unchanged."""
    primary_paths = {route.path for route in api_main.app.routes if isinstance(route, APIRoute)}

    assert f"{_GATEWAY_PREFIX}/calls" in primary_paths
    assert f"{_GATEWAY_PREFIX}/runs/{{run_id}}/calls/{{call_id}}" in primary_paths
    assert "/api/health" in primary_paths
