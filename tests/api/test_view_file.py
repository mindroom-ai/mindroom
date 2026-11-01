"""Authenticated worker-side viewing of files from prepared workspaces."""

import asyncio
import io
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from mindroom.api import sandbox_runner
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import FileAccess
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.media_transport import decode_media_result
from mindroom.tool_system.tool_function import ToolValue
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from mindroom.workers.protocol import WORKER_PROTOCOL_VERSION, ViewFileRequest
from tests.conftest import SETTLE_TIMEOUT_SECONDS

TOKEN = "view-file-test-token"  # noqa: S105
HEADERS = {"x-mindroom-sandbox-token": TOKEN}


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), (10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def view_file_client(tmp_path: Path) -> tuple[TestClient, Path]:
    """Return an authenticated runner app and the workspace its requests name."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    app = FastAPI()
    app.state.sandbox_runner_context = sandbox_runner._SandboxRunnerContext(
        runtime_paths=runtime_paths,
        config=Config(agents={}, models={}),
        runner_token=TOKEN,
    )
    app.include_router(sandbox_runner.router)
    return TestClient(app), workspace


def _view(workspace: Path, path: str) -> dict[str, object]:
    return {"protocol": WORKER_PROTOCOL_VERSION, "base_dir": str(workspace), "path": path}


def test_view_file_endpoint_requires_runner_authentication(
    view_file_client: tuple[TestClient, Path],
) -> None:
    """The internal file endpoint inherits runner-token authentication, refused untyped so it blames the worker."""
    client, workspace = view_file_client
    (workspace / "plot.png").write_bytes(_png_bytes())

    response = client.post("/api/sandbox-runner/view-file", json=_view(workspace, "plot.png"))

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized sandbox runner request"}


def test_view_file_endpoint_returns_bounded_image_envelope(
    view_file_client: tuple[TestClient, Path],
) -> None:
    """Worker image bytes cross the endpoint only inside the bounded envelope."""
    client, workspace = view_file_client
    data = _png_bytes()
    (workspace / "plot.png").write_bytes(data)

    response = client.post("/api/sandbox-runner/view-file", headers=HEADERS, json=_view(workspace, "plot.png"))

    assert response.status_code == 200
    assert response.json()["ok"] is True
    result = decode_media_result(response.json()["result"])
    assert result.images
    assert result.images[0].content == data
    assert json.loads(result.content)["path"] == "plot.png"


def test_view_file_endpoint_rejects_paths_outside_prepared_workspace(
    view_file_client: tuple[TestClient, Path],
    tmp_path: Path,
) -> None:
    """A symlink cannot make the worker open a file outside its workspace."""
    client, workspace = view_file_client
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes())
    (workspace / "escape.png").symlink_to(outside)

    response = client.post(
        "/api/sandbox-runner/view-file",
        headers=HEADERS,
        json=_view(workspace, "escape.png"),
    )

    assert response.status_code == 200
    result = decode_media_result(response.json()["result"])
    assert not result.images
    assert json.loads(result.content)["view_status"] == "error"


@pytest.mark.parametrize("file_access", ["workspace", "unrestricted"])
def test_view_file_endpoint_follows_routing_agent_file_access(
    view_file_client: tuple[TestClient, Path],
    tmp_path: Path,
    file_access: FileAccess,
) -> None:
    """The worker applies the routing agent's configured file_access to paths outside its workspace."""
    client, _workspace = view_file_client
    client.app.state.sandbox_runner_context = replace(
        client.app.state.sandbox_runner_context,
        config=Config(
            agents={"writer": AgentConfig(display_name="Writer", file_access=file_access, memory_backend="file")},
            models={},
        ),
    )
    outside = tmp_path / "outside.png"
    data = _png_bytes()
    outside.write_bytes(data)

    response = client.post(
        "/api/sandbox-runner/view-file",
        headers=HEADERS,
        json={"protocol": WORKER_PROTOCOL_VERSION, "path": str(outside), "routing_agent_name": "writer"},
    )

    assert response.status_code == 200
    result = decode_media_result(response.json()["result"])
    if file_access == "workspace":
        assert not result.images
        assert json.loads(result.content)["view_status"] == "error"
        return
    assert result.images
    assert result.images[0].content == data
    assert json.loads(result.content)["path"] == str(outside.resolve())


def test_view_file_endpoint_rejects_missing_workspace(view_file_client: tuple[TestClient, Path]) -> None:
    """Viewing fails the call when the request names no workspace."""
    client, _workspace = view_file_client

    response = client.post(
        "/api/sandbox-runner/view-file",
        headers=HEADERS,
        json={"protocol": WORKER_PROTOCOL_VERSION, "path": "plot.png"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error": "This request has no workspace in this worker.",
        "failure_kind": "tool",
    }


@pytest.mark.asyncio
async def test_view_file_endpoint_keeps_event_loop_responsive_during_decode(
    view_file_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace I/O, Pillow decoding, and transport encoding run off the event loop."""
    client, workspace = view_file_client
    loop = asyncio.get_running_loop()
    view_saw_loop_running: list[bool] = []

    def blocking_view(_path: str, *, workspace: Path, file_access: FileAccess) -> ToolValue:
        assert workspace.name == "workspace"
        assert file_access == "workspace"
        # The loop runs this callback during the view only when the view leaves the loop free.
        loop_kept_running = threading.Event()
        loop.call_soon_threadsafe(loop_kept_running.set)
        view_saw_loop_running.append(loop_kept_running.wait(timeout=SETTLE_TIMEOUT_SECONDS))
        return ToolValue(content="decoded")

    monkeypatch.setattr(sandbox_runner, "view_agent_image", blocking_view)
    request = type("Request", (), {"app": client.app})()

    response = await sandbox_runner.view_file_in_worker(request, ViewFileRequest(**_view(workspace, "plot.png")))

    assert response.ok is True
    assert view_saw_loop_running == [True]


@pytest.mark.asyncio
async def test_view_file_endpoint_reads_the_routing_agent_workspace_in_a_prepared_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed public agent without file memory reads from its own workspace, not its prepared worker's default."""
    workspace = tmp_path / "prepared-worker" / "workspace"
    workspace.mkdir(parents=True)
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    agent_workspace = agent_workspace_root_path(runtime_paths.storage_root, "writer")
    agent_workspace.mkdir(parents=True)
    data = _png_bytes()
    (agent_workspace / "plot.png").write_bytes(data)
    app = FastAPI()
    app.state.sandbox_runner_context = sandbox_runner._SandboxRunnerContext(
        runtime_paths=runtime_paths,
        config=Config(agents={"writer": AgentConfig(display_name="Writer", memory_backend="none")}, models={}),
        runner_token=TOKEN,
    )
    monkeypatch.setattr(
        sandbox_runner.sandbox_worker_prep,
        "normalize_request_worker_key",
        lambda worker_key, _runtime_paths: worker_key,
    )
    monkeypatch.setattr(
        sandbox_runner.sandbox_worker_prep,
        "prepare_worker_request",
        lambda **_kwargs: SimpleNamespace(base_dir=workspace),
    )

    response = await sandbox_runner.view_file_in_worker(
        SimpleNamespace(app=app),
        ViewFileRequest(
            protocol=WORKER_PROTOCOL_VERSION,
            worker_key="worker-key",
            routing_agent_name="writer",
            path="plot.png",
        ),
    )

    assert response.ok is True
    result = decode_media_result(response.result)
    assert isinstance(result, ToolValue)
    assert result.images
    assert result.images[0].content == data
