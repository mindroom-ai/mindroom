"""File viewing preserves the selected workspace across worker transport."""

import json
import sys
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom.api import sandbox_runner
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.attachments import AttachmentTools
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.media_transport import decode_media_result
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    ToolExecutionIdentity,
    _private_instance_state_root_path,
    resolve_worker_target,
    worker_dir_name,
)
from mindroom.workers.models import WorkerHandle
from tests.api.test_view_file import HEADERS, TOKEN, _png_bytes
from tests.test_attachments_tool import _tool_context


@pytest.fixture
def routed_workspace(tmp_path: Path) -> tuple[TestClient, ResolvedWorkerTarget, Path, Path]:
    """Prepare real dedicated-worker paths without requiring agent config on the worker."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="writer",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    target = resolve_worker_target("user_agent", "writer", identity, private_agent_names=frozenset({"writer"}))
    assert target.worker_key is not None
    shared_root = tmp_path / "worker-storage"
    worker_root = shared_root / "workers" / worker_dir_name(target.worker_key)
    workspace = (
        _private_instance_state_root_path(
            shared_root,
            worker_key=target.worker_key,
            agent_name="writer",
        )
        / "workspace"
    )
    workspace.mkdir(parents=True)
    (workspace / "sample.png").write_bytes(_png_bytes())
    # Path-only requests reuse an existing interpreter without bootstrapping packages.
    interpreter = worker_root / "venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=worker_root,
        process_env={
            "MINDROOM_SANDBOX_RUNNER_MODE": "true",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": target.worker_key,
            "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(worker_root),
            "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT": str(shared_root),
        },
    )
    app = FastAPI()
    app.state.sandbox_runner_context = sandbox_runner._SandboxRunnerContext(
        runtime_paths=runtime_paths,
        config=Config(agents={}, models={}),
        tool_metadata={},
        runner_token=TOKEN,
    )
    app.include_router(sandbox_runner.router)
    return TestClient(app), target, shared_root, workspace


@pytest.mark.parametrize("path_kind", ["relative", "absolute", "internal_symlink"])
def test_view_file_uses_validated_agent_workspace(
    routed_workspace: tuple[TestClient, ResolvedWorkerTarget, Path, Path],
    path_kind: str,
) -> None:
    """Images in an agent workspace remain visible when the worker default is elsewhere."""
    client, target, shared_root, workspace = routed_workspace
    path = str(workspace / "sample.png") if path_kind == "absolute" else "sample.png"
    if path_kind == "internal_symlink":
        (workspace / "linked.png").symlink_to("sample.png")
        path = "linked.png"

    response = client.post(
        "/api/sandbox-runner/view-file",
        headers=HEADERS,
        json={
            "worker_key": target.worker_key,
            "routing_agent_name": "writer",
            "private_agent_names": ["writer"],
            "execution_identity": asdict(target.execution_identity),
            "tool_init_overrides": {"base_dir": workspace.relative_to(shared_root).as_posix()},
            "path": path,
        },
    )

    assert response.status_code == 200
    result = decode_media_result(response.json()["result"])
    assert result.images, result.content
    assert result.images[0].content == _png_bytes()


@pytest.mark.parametrize("foreign_root", ["requester", "agent", "symlink"])
def test_view_file_rejects_unauthorized_workspace_override(
    routed_workspace: tuple[TestClient, ResolvedWorkerTarget, Path, Path],
    foreign_root: str,
) -> None:
    """An explicit workspace cannot expand the worker's requester or agent authority."""
    client, target, shared_root, workspace = routed_workspace
    assert target.execution_identity is not None
    other = resolve_worker_target(
        "user_agent",
        "writer" if foreign_root == "requester" else "reader",
        replace(target.execution_identity, requester_id="@bob:example.org")
        if foreign_root == "requester"
        else target.execution_identity,
    )
    assert other.worker_key is not None
    forbidden = (
        _private_instance_state_root_path(
            shared_root,
            worker_key=other.worker_key,
            agent_name=other.routing_agent_name,
        )
        / "workspace"
    )
    forbidden.mkdir(parents=True)
    (forbidden / "sample.png").write_bytes(_png_bytes())
    if foreign_root == "symlink":
        (workspace / "escape").symlink_to(forbidden, target_is_directory=True)
        forbidden = workspace / "escape"

    response = client.post(
        "/api/sandbox-runner/view-file",
        headers=HEADERS,
        json={
            "worker_key": target.worker_key,
            "private_agent_names": ["writer"],
            "tool_init_overrides": {"base_dir": str(forbidden)},
            "path": "sample.png",
        },
    )

    assert response.status_code == 400
    assert "allowed state roots" in response.json()["detail"]


def test_view_file_without_override_keeps_worker_default(
    routed_workspace: tuple[TestClient, ResolvedWorkerTarget, Path, Path],
) -> None:
    """Callers without an agent workspace still read their dedicated worker workspace."""
    client, _target, _shared_root, _workspace = routed_workspace
    worker_root = client.app.state.sandbox_runner_context.runtime_paths.storage_root
    default_workspace = worker_root / "workspace"
    default_workspace.mkdir()
    (default_workspace / "default.png").write_bytes(_png_bytes())

    response = client.post(
        "/api/sandbox-runner/view-file",
        headers=HEADERS,
        json={"private_agent_names": ["writer"], "path": "default.png"},
    )

    assert response.status_code == 200
    result = decode_media_result(response.json()["result"])
    assert result.images
    assert result.images[0].content == _png_bytes()


@pytest.mark.asyncio
async def test_view_file_transports_workspace_between_storage_mounts(
    routed_workspace: tuple[TestClient, ResolvedWorkerTarget, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One public tool call reads worker-only pixels and retains a reusable attachment."""
    client, target, shared_root, workspace = routed_workspace
    primary_root = tmp_path / "primary-storage"
    primary_workspace = primary_root / workspace.relative_to(shared_root)
    assert not primary_workspace.exists()
    context = _tool_context(primary_root, process_env={"MINDROOM_SANDBOX_EXECUTION_MODE": "all"})
    manager = Mock()
    manager.ensure_worker.return_value = WorkerHandle(
        worker_id="test-worker",
        worker_key=target.worker_key,
        endpoint="http://worker/api/sandbox-runner/execute",
        auth_token=TOKEN,
        status="ready",
        backend_name="local",
        last_used_at=0.0,
        created_at=0.0,
    )
    monkeypatch.setattr(sandbox_proxy, "lease_primary_worker_manager", lambda *_args, **_kwargs: nullcontext(manager))
    # Replace only the network connection; routing, request validation and media delivery stay real.
    monkeypatch.setattr(sandbox_proxy.httpx, "Client", lambda **_kwargs: client)
    tools = AttachmentTools(
        runtime_paths=context.runtime_paths,
        worker_target=target,
        tool_output_workspace_root=primary_workspace,
    )

    with tool_runtime_context(context):
        result = await tools.view_file(path="sample.png")
        assert result.images, result.content
        receipt = json.loads(result.content)
        reopened = await tools.view_file(attachment_id=receipt["attachment_id"])

    assert result.images[0].content == _png_bytes()
    assert reopened.images
    assert reopened.images[0].content == _png_bytes()
    assert receipt["view_status"] == "ready"
    assert not primary_workspace.exists()
    assert not context.client.room_send.called
