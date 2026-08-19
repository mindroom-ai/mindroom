"""Worker API tests for supervised background Python scripts."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom import shell_supervisor as shell_supervisor_module
from mindroom.api import sandbox_runner as sandbox_runner_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.api.sandbox_runner_scripts import _script_namespace
from mindroom.api.sandbox_runner_scripts import router as sandbox_runner_scripts_router
from mindroom.api.sandbox_worker_prep import prepare_worker_request
from mindroom.constants import resolve_runtime_paths
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.shell_supervisor import _ShellSupervisorManager
from mindroom.workers.backends import local as local_workers_module

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

_TOKEN = "worker-secret"  # noqa: S105
_HEADERS = {"x-mindroom-sandbox-token": _TOKEN}
_WORKER_KEY = "v1:test:shared:scripts"
_SUPERVISOR_HANDLE = f"shell:{'a' * 32}"


def _fake_local_worker_venv_create(_self: object, venv_dir: Path) -> None:
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "python").symlink_to(Path(sys.executable))


@pytest.fixture
def runner_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, Path]]:
    """Provide an authenticated runner with one real isolated supervisor."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )
    monkeypatch.setattr(local_workers_module.venv.EnvBuilder, "create", _fake_local_worker_venv_create)
    monkeypatch.setattr(local_workers_module, "_local_worker_manager", None)
    monkeypatch.setattr(local_workers_module, "_local_worker_manager_config", None)
    sandbox_runner_module.initialize_sandbox_runner_app(
        sandbox_runner_app,
        runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=_TOKEN,
    )
    prepared = prepare_worker_request(
        worker_key=_WORKER_KEY,
        tool_init_overrides={},
        runtime_paths=runtime_paths,
        runner_token=_TOKEN,
    )
    supervisor = _ShellSupervisorManager()
    monkeypatch.setattr(shell_supervisor_module, "_manager", supervisor)
    try:
        yield TestClient(sandbox_runner_app), prepared.paths.workspace
    finally:
        supervisor.shutdown()


def _write_run_files(workspace: Path, run_id: str, source: str) -> tuple[str, str, str]:
    relative_root = Path(".mindroom-script-runs") / run_id
    run_root = workspace / relative_root
    run_root.mkdir(parents=True)
    source_path = run_root / "source.py"
    token_path = run_root / "capability"
    source_path.write_text(source, encoding="utf-8")
    token_path.write_text("capability-token", encoding="utf-8")
    return (
        str(relative_root / source_path.name),
        str(relative_root / token_path.name),
        hashlib.sha256(source.encode()).hexdigest(),
    )


def _run_payload(workspace: Path, *, run_id: str, source: str) -> dict[str, object]:
    source_path, token_path, source_digest = _write_run_files(workspace, run_id, source)
    return {
        "run_id": run_id,
        "worker_key": _WORKER_KEY,
        "source_path": source_path,
        "source_digest": source_digest,
        "token_path": token_path,
        "supervisor_handle": _SUPERVISOR_HANDLE,
        "environment": {"MINDROOM_SCRIPT_GATEWAY_URL": "http://primary:8765/api/script-gateway"},
        "tail_lines": 100,
    }


def test_worker_script_endpoint_launches_statuses_and_cancels_process(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A valid request should traverse the real supervisor handle lifecycle."""
    client, workspace = runner_client
    run_id = "run-1"
    response = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json=_run_payload(
            workspace,
            run_id=run_id,
            source="import time\nprint('ready', flush=True)\ntime.sleep(60)\n",
        ),
    )

    assert response.status_code == 200
    launch = response.json()
    assert launch["ok"] is True
    handle = launch["supervisor_handle"]
    assert handle == _SUPERVISOR_HANDLE

    status = client.get(
        f"/api/sandbox-runner/scripts/{run_id}",
        headers=_HEADERS,
        params={"worker_key": _WORKER_KEY, "supervisor_handle": handle},
    )
    assert status.status_code == 200
    assert status.json()["state"] == "running"

    cancelled = client.post(
        f"/api/sandbox-runner/scripts/{run_id}/cancel",
        headers=_HEADERS,
        json={"worker_key": _WORKER_KEY, "supervisor_handle": handle},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True


def test_worker_script_endpoint_rejects_source_digest_mismatch(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A changed snapshot must not launch under the primary's digest receipt."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id="run-digest", source="print('expected')\n")
    payload["source_digest"] = "0" * 64

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 400
    assert "digest" in response.json()["detail"].lower()


def test_worker_script_endpoint_rejects_path_outside_worker_workspace(
    runner_client: tuple[TestClient, Path],
    tmp_path: Path,
) -> None:
    """A relative traversal must not escape the selected worker workspace."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id="run-escape", source="print('no')\n")
    outside = tmp_path / "outside.py"
    outside.write_text("print('escaped')\n", encoding="utf-8")
    payload["source_path"] = str(outside)
    payload["source_digest"] = hashlib.sha256(outside.read_bytes()).hexdigest()

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 400
    assert "workspace" in response.json()["detail"].lower()


def test_worker_script_endpoint_rejects_nul_in_snapshot_path(
    runner_client: tuple[TestClient, Path],
) -> None:
    """Malformed path bytes must produce a client error instead of escaping as a server failure."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id="run-nul", source="print('no')\n")
    payload["source_path"] = "source\x00.py"

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 400
    assert "workspace" in response.json()["detail"].lower()


def test_worker_script_endpoint_rejects_unapproved_environment_name(
    runner_client: tuple[TestClient, Path],
) -> None:
    """The launch protocol must not become an arbitrary environment injection channel."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id="run-env", source="print('no')\n")
    payload["environment"] = {"MINDROOM_CONTROL_STATE_PATH": "/primary/private"}

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 400
    assert "environment" in response.json()["detail"].lower()


def test_worker_script_endpoint_rejects_oversized_request(
    runner_client: tuple[TestClient, Path],
) -> None:
    """The worker must reject oversized control bodies before process launch."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id="run-large", source="print('no')\n")
    payload["environment"] = {"MINDROOM_SCRIPT_GATEWAY_URL": f"http://primary.test/{'x' * 20_000}"}

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_worker_script_endpoint_stops_reading_chunked_body_over_limit(
    runner_client: tuple[TestClient, Path],
) -> None:
    """The body boundary must stop receiving chunks as soon as the limit is crossed."""
    _client, _workspace = runner_client
    consumed_chunks: list[int] = []

    async def oversized_body() -> AsyncIterator[bytes]:
        for index in range(3):
            consumed_chunks.append(index)
            yield b"x" * 9000

    transport = httpx.ASGITransport(app=sandbox_runner_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.post(
            "/api/sandbox-runner/scripts/run",
            headers=_HEADERS,
            content=oversized_body(),
        )

    assert consumed_chunks == [0, 1]
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_worker_script_endpoint_replays_valid_chunked_body_for_model_validation(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A bounded streamed body must remain available to FastAPI's Pydantic parser."""
    client, workspace = runner_client
    run_id = "run-streamed"
    raw_body = json.dumps(
        _run_payload(
            workspace,
            run_id=run_id,
            source="import time\ntime.sleep(60)\n",
        ),
    ).encode()

    async def chunked_body() -> AsyncIterator[bytes]:
        midpoint = len(raw_body) // 2
        yield raw_body[:midpoint]
        yield raw_body[midpoint:]

    transport = httpx.ASGITransport(app=sandbox_runner_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        response = await async_client.post(
            "/api/sandbox-runner/scripts/run",
            headers={**_HEADERS, "content-type": "application/json"},
            content=chunked_body(),
        )

    assert response.status_code == 200
    handle = response.json()["supervisor_handle"]
    client.post(
        f"/api/sandbox-runner/scripts/{run_id}/cancel",
        headers=_HEADERS,
        json={"worker_key": _WORKER_KEY, "supervisor_handle": handle, "force": True},
    )


def test_worker_script_endpoint_rejects_mismatched_dedicated_worker_key(tmp_path: Path) -> None:
    """A worker-scoped auth endpoint must not control a sibling worker namespace."""
    previous_context = getattr(sandbox_runner_app.state, "sandbox_runner_context", None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-5.4\nagents: {}\nrouter:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: "worker-a",
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: str(tmp_path / "worker-a"),
        },
    )
    dedicated_worker_app = FastAPI()
    dedicated_worker_app.include_router(sandbox_runner_scripts_router)
    sandbox_runner_module.initialize_sandbox_runner_app(
        dedicated_worker_app,
        runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=_TOKEN,
    )

    response = TestClient(dedicated_worker_app).post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json={
            "run_id": "run-sibling",
            "worker_key": "worker-b",
            "source_path": "source.py",
            "source_digest": "a" * 64,
            "token_path": "capability",
            "supervisor_handle": _SUPERVISOR_HANDLE,
            "environment": {"MINDROOM_SCRIPT_GATEWAY_URL": "http://primary.test/api/script-gateway"},
        },
    )

    assert response.status_code == 400
    assert "dedicated worker" in response.json()["detail"].lower()
    assert getattr(sandbox_runner_app.state, "sandbox_runner_context", None) is previous_context


def test_worker_script_status_is_bound_to_run_namespace(
    runner_client: tuple[TestClient, Path],
) -> None:
    """Knowing a handle must not make it visible through another run ID."""
    client, workspace = runner_client
    response = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json=_run_payload(
            workspace,
            run_id="run-owned",
            source="import time\ntime.sleep(60)\n",
        ),
    )
    handle = response.json()["supervisor_handle"]
    try:
        status = client.get(
            "/api/sandbox-runner/scripts/run-other",
            headers=_HEADERS,
            params={"worker_key": _WORKER_KEY, "supervisor_handle": handle},
        )

        assert status.status_code == 200
        assert status.json()["state"] == "unknown"
    finally:
        client.post(
            "/api/sandbox-runner/scripts/run-owned/cancel",
            headers=_HEADERS,
            json={"worker_key": _WORKER_KEY, "supervisor_handle": handle, "force": True},
        )


def test_script_namespace_distinguishes_delimiter_ambiguous_identities() -> None:
    """Worker and run boundaries must not depend on ambiguous delimiter concatenation."""
    assert _script_namespace("a:b", "c") != _script_namespace("a", "b:c")


def test_worker_script_launch_rejects_path_like_run_id(
    runner_client: tuple[TestClient, Path],
) -> None:
    """The entire launch run ID must match the filesystem-safe identifier grammar."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id="run-safe", source="print('no')\n")
    payload["run_id"] = "run-safe/child"

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 422


def test_worker_script_cancel_rejects_handle_with_valid_prefix(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A supervisor handle prefix must not authorize a different full handle string."""
    client, _workspace = runner_client

    response = client.post(
        "/api/sandbox-runner/scripts/run-safe/cancel",
        headers=_HEADERS,
        json={"worker_key": _WORKER_KEY, "supervisor_handle": f"{_SUPERVISOR_HANDLE}-suffix"},
    )

    assert response.status_code == 422


def test_worker_script_endpoints_use_runner_authentication(
    runner_client: tuple[TestClient, Path],
) -> None:
    """Script process control must inherit the sandbox runner's authentication boundary."""
    client, workspace = runner_client

    response = client.post(
        "/api/sandbox-runner/scripts/run",
        json=_run_payload(workspace, run_id="run-auth", source="print('no')\n"),
    )

    assert response.status_code == 401
