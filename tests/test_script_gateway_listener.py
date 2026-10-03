"""Gateway-only listener that isolated background-script workers reach instead of the primary API."""

from __future__ import annotations

import re
import socket
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
from fastapi.routing import APIRoute

from mindroom.api import main as api_main
from mindroom.api.script_gateway import serve_script_gateway_listener
from mindroom.constants import RuntimePaths
from mindroom.script_runs.models import ScriptCallRecord, ScriptCallState, ScriptToolGrant

if TYPE_CHECKING:
    from pathlib import Path

_GATEWAY_PREFIX = "/api/script-gateway"


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


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _runtime_paths(tmp_path: Path, process_env: dict[str, str]) -> RuntimePaths:
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env=process_env,
    )


def _primary_api_requests() -> list[tuple[str, str]]:
    """Return one concrete request for every primary API route outside the gateway."""
    requests = []
    for route in api_main.app.routes:
        if not isinstance(route, APIRoute) or route.path.startswith(_GATEWAY_PREFIX):
            continue
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        requests.extend((method, path) for method in sorted(route.methods))
    return requests


@pytest.mark.asyncio
async def test_listener_serves_only_script_gateway_routes(tmp_path: Path) -> None:
    """The listener answers gateway calls with the bound broker and nothing from the primary API."""
    port = _free_port()
    runtime_paths = _runtime_paths(tmp_path, {"MINDROOM_SCRIPT_GATEWAY_PORT": str(port)})
    primary_requests = _primary_api_requests()
    assert ("GET", "/api/health") in primary_requests

    async with (
        serve_script_gateway_listener(runtime_paths, host="127.0.0.1", broker=_ReceiptBroker(), log_level="INFO"),
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client,
    ):
        receipt = await client.get(
            f"{_GATEWAY_PREFIX}/runs/run-1/calls/call-1",
            headers={"Authorization": "Bearer capability"},
        )
        assert receipt.status_code == 200
        assert receipt.json()["result"] == 3

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
async def test_listener_is_absent_without_a_configured_port(tmp_path: Path) -> None:
    """Deployments that do not opt in keep a single primary API listener."""
    with patch("mindroom.api.script_gateway.socket.create_server") as create_server:
        async with serve_script_gateway_listener(
            _runtime_paths(tmp_path, {}),
            host="127.0.0.1",
            broker=_ReceiptBroker(),
            log_level="INFO",
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
            broker=_ReceiptBroker(),
            log_level="INFO",
        ):
            pass


def test_primary_api_still_serves_gateway_and_general_routes() -> None:
    """The dedicated listener is additive; the primary API route table is unchanged."""
    primary_paths = {route.path for route in api_main.app.routes if isinstance(route, APIRoute)}

    assert f"{_GATEWAY_PREFIX}/calls" in primary_paths
    assert f"{_GATEWAY_PREFIX}/runs/{{run_id}}/calls/{{call_id}}" in primary_paths
    assert "/api/health" in primary_paths
