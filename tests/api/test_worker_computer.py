"""Authenticated internal computer HTTP routes."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Never

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from structlog.testing import capture_logs

from mindroom.api import sandbox_runner, sandbox_runner_app, sandbox_worker_prep, worker_computer
from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.api.worker_computer import router
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.browser import BrowserTools
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key
from mindroom.worker_browser import WorkerBrowserRuntime
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
        payload["kwargs"]["mindroom_output_path"] = "tabs.json"
        saved = client.post("/api/sandbox-runner/execute", headers=headers, json=payload)
        receipt = saved.json()["result"]["mindroom_tool_output"]
        assert receipt["status"] == "saved_to_file"
        assert json.loads((root / "workspace" / "tabs.json").read_text())["same_target"] is True
        app.state.worker_computer = None
        assert (
            client.post("/api/sandbox-runner/execute", headers=headers, json=payload).json()["result"] == "subprocess"
        )


@pytest.mark.parametrize("termination", ["normal", "text", "bug"])
def test_rfb_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, termination: str) -> None:  # noqa: PLR0915 - transport lifecycle through teardown
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
    with TestClient(app) as client, capture_logs() as logs:
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
            if termination == "bug":

                def fail_parser(*_args: object, **_kwargs: object) -> Never:
                    message = "credential-bearing-secret"
                    raise KeyError(message)

                monkeypatch.setattr(worker_computer.RfbClientFilter, "feed", fail_parser)
                ws.send_bytes(key)
            elif termination == "text":
                ws.send_text("credential-bearing-secret")
            if termination != "normal":
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_bytes()
        assert client.get("/computer", headers=headers).json()["controller_session_id"] is None
        if termination == "bug":
            assert any(entry.get("error_type") == "KeyError" for entry in logs)
        assert "credential-bearing-secret" not in str(logs)
        assert all(not entry.get("exc_info") for entry in logs)


@pytest.mark.asyncio
async def test_worker_stream_cancellation_drains_socket_and_exact_ownership(  # noqa: PLR0915 - real transport ownership through repeated cancellation
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated cancellation during sibling drain cannot strand the Unix socket or viewer lease."""
    runtime = WorkerComputerRuntime(FakeDisplay())
    runtime.display.socket_path = tmp_path / "cancellation-rfb"
    status = await runtime.ensure_started()
    closed = asyncio.Event()

    async def peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_unix_server(peer, path=runtime.display.socket_path)
    app = FastAPI()
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    initialize_sandbox_runner_app(app, paths, config=Config(), runner_token=RUNNER_TOKEN)
    app.state.worker_computer = runtime
    messages: asyncio.Queue[dict] = asyncio.Queue()
    await messages.put({"type": "websocket.connect"})
    accepted, draining, proceed = (asyncio.Event() for _ in range(3))

    async def send(message: dict) -> None:
        if message["type"] == "websocket.accept":
            accepted.set()

    websocket = WebSocket(
        {"type": "websocket", "app": app, "headers": [(b"x-mindroom-sandbox-token", RUNNER_TOKEN.encode())]},
        messages.get,
        send,
    )
    downstream = worker_computer._display_to_client

    async def pause_drain(reader: asyncio.StreamReader, socket: WebSocket) -> None:
        try:
            await downstream(reader, socket)
        finally:
            draining.set()
            await proceed.wait()

    monkeypatch.setattr(worker_computer, "_display_to_client", pause_drain)
    handler = asyncio.create_task(worker_computer.stream(websocket, "viewer", status["generation"]))
    try:
        await asyncio.wait_for(accepted.wait(), timeout=1)
        await runtime.take_control("viewer")
        await messages.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(draining.wait(), timeout=1)
        for _ in range(3):
            handler.cancel()
            await asyncio.sleep(0)
        assert not handler.done(), "Cancellation escaped before socket and ownership cleanup"
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await handler
        assert runtime.status()["controller_session_id"] is None
        assert not runtime._streams
        await asyncio.wait_for(closed.wait(), timeout=1)
    finally:
        proceed.set()
        await asyncio.gather(handler, return_exceptions=True)
        await runtime.close()
        server.close()
        server.abort_clients()
        await server.wait_closed()


@pytest.mark.parametrize("enabled", [True, False])
def test_native_functions_reuse_one_guarded_session(  # noqa: PLR0915 - full HTTP ownership lifecycle
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """Current native names route through one retained toolkit; disabled mode fails closed."""
    from agno.tools.function import ToolResult  # noqa: PLC0415

    from mindroom.worker_computer.mcp_provider import WorkerBrowserMCP  # noqa: PLC0415
    from mindroom.worker_computer.mcp_results import decode_browser_mcp_result  # noqa: PLC0415

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
            "MINDROOM_WORKER_COMPUTER_ENABLED": str(enabled).lower(),
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": worker_key,
            "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(root),
        },
    )
    app = FastAPI()
    initialize_sandbox_runner_app(app, paths, config=Config(), runner_token=RUNNER_TOKEN)
    computer = WorkerComputerRuntime(FakeDisplay())
    app.state.worker_browser = WorkerBrowserRuntime()
    if enabled:
        app.state.worker_computer = computer
    app.include_router(sandbox_runner.router)
    prepared = sandbox_worker_prep.PreparedWorkerRequest(
        handle=WorkerHandle("test", worker_key, "http://worker/execute", "secret", "ready", "docker", 0, 0),
        paths=local_worker_state_paths_for_root(root),
        runtime_overrides={"base_dir": root / "workspace"},
    )
    monkeypatch.setattr(sandbox_worker_prep, "prepare_worker_request", lambda **_kwargs: prepared)
    calls = []

    async def execute(self: WorkerBrowserMCP, name: str, arguments: dict[str, object]) -> ToolResult:
        calls.append((id(self), name, arguments))
        return ToolResult(content=name)

    monkeypatch.setattr(WorkerBrowserMCP, "execute", execute)
    payload = {
        "tool_name": "browser_mcp",
        "function_name": "browser_snapshot",
        "worker_key": worker_key,
        "worker_scope": "user_agent",
        "execution_identity": asdict(identity),
        "private_agent_names": [],
        "kwargs": {},
    }
    headers = {"X-Mindroom-Sandbox-Token": RUNNER_TOKEN}
    with TestClient(app) as client:
        for name in ["browser_snapshot", "browser_tabs", "browser_close"]:
            payload["function_name"] = name
            payload["kwargs"] = {"action": "list"} if name == "browser_tabs" else {}
            response = client.post("/api/sandbox-runner/execute", headers=headers, json=payload)
            if not enabled:
                assert response.status_code == 400
                assert not calls
                return
            assert response.status_code == 200, response.text
            assert response.json()["ok"], response.text
            result = decode_browser_mcp_result(response.json()["result"])
            assert result.content == name
        assert [call[1] for call in calls] == ["browser_snapshot", "browser_tabs", "browser_close"]
        payload["function_name"] = "browser_snapshot"
        payload["kwargs"] = {"mindroom_output_path": "snapshot.txt"}
        saved = client.post("/api/sandbox-runner/execute", headers=headers, json=payload)
        receipt = decode_browser_mcp_result(saved.json()["result"])["mindroom_tool_output"]
        assert receipt["status"] == "saved_to_file"
        assert (root / "workspace" / "snapshot.txt").read_text() == "browser_snapshot"
        assert len({call[0] for call in calls}) == 1
        generation = computer.status()["generation"]
        client.portal.call(computer.attach_stream, "viewer", generation)
        client.portal.call(computer.take_control, "viewer")
        for name in ["browser_snapshot", "browser_tabs", "browser_close"]:
            payload["function_name"] = name
            payload["kwargs"] = {"action": "list"} if name == "browser_tabs" else {}
            denied = client.post("/api/sandbox-runner/execute", headers=headers, json=payload)
            assert denied.json()["ok"] is False
            assert "under user control" in denied.json()["error"]
        assert len(calls) == 4
        payload["function_name"] = "browser_run_code_unsafe"
        payload["kwargs"] = {}
        unsupported = client.post("/api/sandbox-runner/execute", headers=headers, json=payload)
        assert unsupported.status_code == 400
        payload["function_name"] = "browser_snapshot"
        monkeypatch.setattr(sandbox_runner.TOOL_METADATA["browser_mcp"], "factory", lambda: type(None))
        replaced = client.post("/api/sandbox-runner/execute", headers=headers, json=payload)
        assert replaced.status_code == 400
        assert "built-in browser factory" in replaced.json()["detail"]
        assert len(calls) == 4
        client.portal.call(computer.close)


@pytest.mark.parametrize(
    ("provider", "function"),
    [("browser", "browser_control"), ("browser_mcp", "browser_snapshot")],
)
@pytest.mark.parametrize("agent_name", [[], {}, ["writer"], {"name": "writer"}, 7, True])
def test_computer_execute_rejects_malformed_agent_name(
    tmp_path: Path,
    provider: str,
    function: str,
    agent_name: object,
) -> None:
    """Arbitrary identity values return a client error before provider dispatch."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    app = FastAPI()
    initialize_sandbox_runner_app(app, paths, config=Config(), runner_token=RUNNER_TOKEN)
    app.state.worker_computer = WorkerComputerRuntime(FakeDisplay())
    app.include_router(sandbox_runner.router)
    with TestClient(app) as client:
        response = client.post(
            "/api/sandbox-runner/execute",
            headers={"X-Mindroom-Sandbox-Token": RUNNER_TOKEN},
            json={
                "tool_name": provider,
                "function_name": function,
                "execution_identity": {
                    "channel": "matrix",
                    "agent_name": agent_name,
                    "requester_id": "@alice:example.org",
                    "room_id": "!room:example.org",
                    "thread_id": None,
                    "resolved_thread_id": None,
                    "session_id": "session",
                },
            },
        )
    assert response.status_code == 400, response.text
    assert "agent_name" in response.json()["detail"]


@pytest.mark.parametrize(
    ("enabled", "effective_uid", "platform"),
    [(True, 0, "posix"), (True, 1000, "posix"), (False, 0, "posix"), (True, 0, "nt")],
)
def test_computer_startup_requires_nonroot_effective_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
    effective_uid: int,
    platform: str,
) -> None:
    """Actual worker identity covers named/image users before any requests are prepared."""
    process = SimpleNamespace(name=platform)
    if platform == "posix":
        process.geteuid = lambda: effective_uid
    monkeypatch.setattr(sandbox_runner_app, "os", process)
    prepared = []

    async def prepare(_app: FastAPI) -> None:
        prepared.append(True)

    monkeypatch.setattr(sandbox_runner_app, "prepare_script_worker_before_serving", prepare)
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_WORKER_COMPUTER_ENABLED": str(enabled).lower(),
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": "v1:default:user_agent:~@alice:example.org:writer",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(tmp_path),
        },
    )
    app = FastAPI(lifespan=sandbox_runner_app._lifespan)
    initialize_sandbox_runner_app(app, paths, config=Config(), runner_token=RUNNER_TOKEN)
    if enabled and platform == "posix" and effective_uid == 0:
        with pytest.raises(RuntimeError, match="non-root"), TestClient(app):
            pass
        assert not prepared
    else:
        with TestClient(app):
            assert isinstance(app.state.worker_computer, WorkerComputerRuntime) is enabled
            assert prepared == [True]
