"""Authenticated worker-side viewing of files from prepared workspaces."""

import asyncio
import io
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from agno.tools.function import ToolResult
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from mindroom.api import sandbox_runner
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import FileAccess
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.media_transport import decode_media_result

TOKEN = "view-file-test-token"  # noqa: S105
HEADERS = {"x-mindroom-sandbox-token": TOKEN}


def _png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (4, 3), (10, 20, 30)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def view_file_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    """Return an authenticated runner app with one prepared workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    app = FastAPI()
    app.state.sandbox_runner_context = sandbox_runner._SandboxRunnerContext(
        runtime_paths=runtime_paths,
        config=Config(agents={}, models={}),
        tool_metadata={},
        runner_token=TOKEN,
    )
    app.include_router(sandbox_runner.router)
    monkeypatch.setattr(sandbox_runner, "_runner_tool_output_workspace_root", lambda **_kwargs: workspace)
    return TestClient(app), workspace


def test_view_file_endpoint_requires_runner_authentication(
    view_file_client: tuple[TestClient, Path],
) -> None:
    """The internal file endpoint inherits runner-token authentication."""
    client, workspace = view_file_client
    (workspace / "plot.png").write_bytes(_png_bytes())

    response = client.post("/api/sandbox-runner/view-file", json={"path": "plot.png"})

    assert response.status_code == 401


def test_view_file_endpoint_returns_bounded_image_envelope(
    view_file_client: tuple[TestClient, Path],
) -> None:
    """Worker image bytes cross the endpoint only inside the bounded envelope."""
    client, workspace = view_file_client
    data = _png_bytes()
    (workspace / "plot.png").write_bytes(data)

    response = client.post("/api/sandbox-runner/view-file", headers=HEADERS, json={"path": "plot.png"})

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

    response = client.post("/api/sandbox-runner/view-file", headers=HEADERS, json={"path": "escape.png"})

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
        config=Config(agents={"writer": AgentConfig(display_name="Writer", file_access=file_access)}, models={}),
    )
    outside = tmp_path / "outside.png"
    data = _png_bytes()
    outside.write_bytes(data)

    response = client.post(
        "/api/sandbox-runner/view-file",
        headers=HEADERS,
        json={"path": str(outside), "routing_agent_name": "writer"},
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


def test_view_file_endpoint_rejects_missing_workspace(
    view_file_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Viewing fails closed when worker preparation yields no workspace."""
    client, _workspace = view_file_client
    monkeypatch.setattr(sandbox_runner, "_runner_tool_output_workspace_root", lambda **_kwargs: None)

    response = client.post("/api/sandbox-runner/view-file", headers=HEADERS, json={"path": "plot.png"})

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "result": None,
        "error": "Worker output workspace is unavailable.",
        "failure_kind": "worker",
    }


@pytest.mark.asyncio
async def test_view_file_endpoint_keeps_event_loop_responsive_during_decode(
    view_file_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace I/O, Pillow decoding, and transport encoding run off the event loop."""
    client, _workspace = view_file_client
    release = threading.Event()

    def blocking_view(_path: str, *, workspace: Path, file_access: FileAccess) -> ToolResult:
        assert workspace.name == "workspace"
        assert file_access == "workspace"
        release.wait(timeout=1)
        return ToolResult(content="decoded")

    monkeypatch.setattr(sandbox_runner, "view_agent_image", blocking_view)
    request = type("Request", (), {"app": client.app})()
    started_at = time.monotonic()
    task = asyncio.create_task(
        sandbox_runner.view_file_in_worker(
            request,
            sandbox_runner.SandboxRunnerViewFileRequest(path="plot.png"),
        ),
    )

    await asyncio.sleep(0.05)
    elapsed = time.monotonic() - started_at
    try:
        assert elapsed < 0.2
        assert not task.done()
    finally:
        release.set()
    response = await task
    assert response.ok is True


@pytest.mark.asyncio
async def test_view_file_endpoint_prefers_prepared_worker_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routed public agent reads from its prepared worker rather than an absent local tool directory."""
    workspace = tmp_path / "prepared-worker" / "workspace"
    workspace.mkdir(parents=True)
    data = _png_bytes()
    (workspace / "plot.png").write_bytes(data)
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    app = FastAPI()
    app.state.sandbox_runner_context = sandbox_runner._SandboxRunnerContext(
        runtime_paths=runtime_paths,
        config=Config(agents={}, models={}),
        tool_metadata={},
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
        lambda **_kwargs: SimpleNamespace(runtime_overrides={"base_dir": workspace}),
    )
    monkeypatch.setattr(
        sandbox_runner,
        "resolve_agent_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(tool_base_dir=None),
    )

    response = await sandbox_runner.view_file_in_worker(
        SimpleNamespace(app=app),
        sandbox_runner.SandboxRunnerViewFileRequest(
            worker_key="worker-key",
            routing_agent_name="writer",
            path="plot.png",
        ),
    )

    assert response.ok is True
    result = decode_media_result(response.result)
    assert isinstance(result, ToolResult)
    assert result.images is not None
    assert result.images[0].content == data
