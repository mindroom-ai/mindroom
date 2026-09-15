"""Authenticated internal computer HTTP routes."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom.api import sandbox_runner, sandbox_worker_prep
from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.api.worker_computer import router
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.browser import BrowserTools
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key
from mindroom.worker_computer.runtime import WorkerComputerRuntime
from mindroom.workers.backends.local import local_worker_state_paths_for_root
from mindroom.workers.models import WorkerHandle
from tests.test_worker_computer_runtime import FakeDisplay

RUNNER_TOKEN = "test-runner-token"  # noqa: S105 - isolated test credential


def test_internal_routes_require_runner_token_and_preserve_stopped_status(tmp_path: Path) -> None:
    """Internal routes deny missing tokens and do not restart stopped displays."""
    app = FastAPI()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    initialize_sandbox_runner_app(app, paths, config=Config(), runner_token=RUNNER_TOKEN)
    app.state.worker_computer = WorkerComputerRuntime(FakeDisplay())
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/computer").status_code == 401
        headers = {"X-Mindroom-Sandbox-Token": RUNNER_TOKEN}
        response = client.post("/computer/start", headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "ready"
        response = client.post("/computer/control", headers=headers, json={"session_id": "viewer", "action": "take"})
        assert response.status_code == 409
        assert (
            client.post(
                "/computer/control",
                headers=headers,
                json={"session_id": "other", "action": "take"},
            ).status_code
            == 409
        )
        client.post("/computer/control", headers=headers, json={"session_id": "viewer", "action": "stop"})
        assert client.get("/computer", headers=headers).json()["state"] == "stopped"


@pytest.mark.parametrize("worker_scope", ["user_agent", None])
def test_two_execute_requests_reuse_browser_and_disabled_uses_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_scope: str | None,
) -> None:
    """Intercept only validated browser calls; preserve the dedicated subprocess fallback."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="writer",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )
    worker_key = resolve_worker_key("user_agent", identity, agent_name="writer")
    root = tmp_path / "worker"
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=root,
        process_env={
            "MINDROOM_WORKER_COMPUTER_ENABLED": "true",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": worker_key,
            "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(root),
        },
    )
    app = FastAPI()
    initialize_sandbox_runner_app(app, paths, config=Config(), runner_token=RUNNER_TOKEN)
    app.state.worker_computer = WorkerComputerRuntime(FakeDisplay())
    app.include_router(sandbox_runner.router)
    prepared = sandbox_worker_prep.PreparedWorkerRequest(
        handle=WorkerHandle("test", worker_key, "http://worker/execute", "secret", "ready", "docker", 0, 0),
        paths=local_worker_state_paths_for_root(root),
        runtime_overrides={"base_dir": root / "workspace"},
    )
    monkeypatch.setattr(sandbox_worker_prep, "prepare_worker_request", lambda **_kwargs: prepared)
    pages = {}

    async def browser(self: BrowserTools, action: str, targetId: str | None = None) -> str:  # noqa: N803
        if action == "open":
            pages[id(self)] = "target-one"
        return json.dumps({"targetId": pages.get(id(self)), "same_target": targetId == pages.get(id(self))})

    async def subprocess(*_args: object, **_kwargs: object) -> sandbox_runner.SandboxRunnerExecuteResponse:
        return sandbox_runner.SandboxRunnerExecuteResponse(ok=True, result="subprocess")

    monkeypatch.setattr(BrowserTools, "browser", browser)
    monkeypatch.setattr(sandbox_runner, "_execute_request_subprocess", subprocess)
    payload = {
        "tool_name": "browser",
        "function_name": "browser_control",
        "worker_key": worker_key,
        "worker_scope": "user_agent",
        "execution_identity": asdict(identity),
        "private_agent_names": [],
        "kwargs": {"action": "open"},
    }
    if worker_scope is None:
        payload.pop("worker_scope")
    headers = {"X-Mindroom-Sandbox-Token": RUNNER_TOKEN}
    with TestClient(app) as client:
        first = client.post("/api/sandbox-runner/execute", headers=headers, json=payload)
        if worker_scope is None:
            assert first.status_code == 400, first.text
            assert "user_agent" in first.json()["detail"]
            assert not pages
            return
        assert first.status_code == 200, first.text
        assert json.loads(first.json()["result"])["targetId"] == "target-one"
        payload["kwargs"] = {"action": "tabs", "targetId": "target-one"}
        second = client.post("/api/sandbox-runner/execute", headers=headers, json=payload)
        assert json.loads(second.json()["result"])["same_target"] is True
        app.state.worker_computer = None
        assert (
            client.post("/api/sandbox-runner/execute", headers=headers, json=payload).json()["result"] == "subprocess"
        )


def test_rfb_stream(tmp_path: Path) -> None:
    """Watcher input is filtered on the worker even when the viewer sends raw key bytes."""
    received = bytearray()
    display = FakeDisplay()
    display.socket_path = tmp_path / "rfb"
    runtime = WorkerComputerRuntime(display)

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(4096):
                received.extend(data)
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        server = await asyncio.start_unix_server(echo, path=display.socket_path)
        await runtime.ensure_started()
        yield
        await runtime.close()
        server.close()
        await server.wait_closed()

    app = FastAPI(lifespan=lifespan)
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    initialize_sandbox_runner_app(app, paths, config=Config(), runner_token=RUNNER_TOKEN)
    app.state.worker_computer = runtime
    app.include_router(router)
    headers = {"X-Mindroom-Sandbox-Token": RUNNER_TOKEN}
    key = bytes.fromhex("0401000000000061")
    update = bytes.fromhex("03010000000005000320")
    hello = b"RFB 003.008\n\x01\x01"
    with TestClient(app) as client:
        generation = client.get("/computer", headers=headers).json()["generation"]
        with client.websocket_connect(
            f"/computer/stream?session_id=viewer&generation={generation}",
            headers=headers,
        ) as ws:
            ws.send_bytes(hello + key[:3])
            assert ws.receive_bytes() == hello
            ws.send_bytes(key[3:] + update)
            assert ws.receive_bytes() == update
            assert key not in received
            takeover = client.post(
                "/computer/control",
                headers=headers,
                json={"session_id": "viewer", "action": "take"},
            )
            assert takeover.status_code == 200
            ws.send_bytes(key)
            assert ws.receive_bytes() == key
            assert (
                client.post(
                    "/computer/control",
                    headers=headers,
                    json={"session_id": "other", "action": "take"},
                ).status_code
                == 409
            )
        assert client.get("/computer", headers=headers).json()["controller_session_id"] is None
