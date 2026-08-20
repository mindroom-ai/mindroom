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
from mindroom.api import sandbox_runner_scripts as sandbox_runner_scripts_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.api.sandbox_runner_scripts import _script_namespace
from mindroom.api.sandbox_runner_scripts import router as sandbox_runner_scripts_router
from mindroom.api.sandbox_worker_prep import prepare_worker_request
from mindroom.constants import resolve_runtime_paths
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.shell_supervisor import ShellSupervisorStartupError, _ShellSupervisorManager
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
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: _WORKER_KEY,
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: str(tmp_path / "dedicated-worker"),
        },
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
    relative_root = Path(".mindroom") / "script-runs" / run_id
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
    _source_path, _token_path, source_digest = _write_run_files(workspace, run_id, source)
    return {
        "run_id": run_id,
        "worker_key": _WORKER_KEY,
        "source_digest": source_digest,
        "gateway_url": "http://primary:8765/api/script-gateway",
    }


def test_worker_script_endpoint_narrow_request_derives_fixed_snapshot_paths(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A fixed run ID derives the worker paths and supervisor handle without caller control."""
    client, workspace = runner_client
    run_id = f"script-{'a' * 32}"
    payload = _run_payload(workspace, run_id=run_id, source="print('ready')\n")

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 200
    assert response.json() == {"ok": True, "error": None, "failure_kind": None}
    client.post(
        f"/api/sandbox-runner/scripts/{run_id}/cancel",
        headers=_HEADERS,
        json={"worker_key": _WORKER_KEY, "force": True},
    )


def test_worker_script_endpoint_rejects_uncontained_platform(
    runner_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker scripts fail closed where supervisor hard-crash containment is unavailable."""
    client, workspace = runner_client
    run_id = f"script-{'b' * 32}"
    payload = _run_payload(workspace, run_id=run_id, source="print('must not run')\n")
    monkeypatch.setattr(
        sandbox_runner_scripts_module,
        "background_script_supervision_supported",
        lambda: False,
        raising=False,
    )

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error": "Background scripts require Linux process-group containment.",
        "failure_kind": "worker",
    }


@pytest.mark.parametrize("operation", ["status", "cancel"])
def test_worker_script_controls_report_supervisor_startup_failure(
    runner_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Control routes return a worker failure instead of leaking supervisor startup errors."""
    client, _workspace = runner_client
    run_id = f"script-{'c' * 32}"
    monkeypatch.setattr(
        sandbox_runner_scripts_module,
        "ensure_shell_supervisor",
        lambda: (_ for _ in ()).throw(ShellSupervisorStartupError("supervisor unavailable")),
    )

    if operation == "status":
        response = client.get(
            f"/api/sandbox-runner/scripts/{run_id}",
            headers=_HEADERS,
            params={"worker_key": _WORKER_KEY},
        )
    else:
        response = client.post(
            f"/api/sandbox-runner/scripts/{run_id}/cancel",
            headers=_HEADERS,
            json={"worker_key": _WORKER_KEY, "force": False},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == "supervisor unavailable"
    assert response.json()["failure_kind"] == "worker"


@pytest.mark.parametrize(
    "extra_field",
    [
        ("source_path", ".mindroom/script-runs/elsewhere/source.py"),
        ("token_path", ".mindroom/script-runs/elsewhere/capability"),
        ("supervisor_handle", _SUPERVISOR_HANDLE),
        ("environment", {"MINDROOM_CONTROL_STATE_PATH": "/primary/private"}),
        ("tail_lines", 1),
    ],
)
def test_worker_script_endpoint_narrow_request_rejects_extra_process_controls(
    runner_client: tuple[TestClient, Path],
    extra_field: tuple[str, object],
) -> None:
    """The actual request model rejects every primary-controlled launch setting."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'b' * 32}", source="print('no')\n")
    payload[extra_field[0]] = extra_field[1]

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 422
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])


def test_worker_script_endpoint_launches_statuses_and_cancels_process(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A valid request should traverse the real supervisor handle lifecycle."""
    client, workspace = runner_client
    run_id = f"script-{'c' * 32}"
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
    assert response.json()["ok"] is True

    status = client.get(
        f"/api/sandbox-runner/scripts/{run_id}",
        headers=_HEADERS,
        params={"worker_key": _WORKER_KEY},
    )
    assert status.status_code == 200
    assert status.json()["state"] == "running"

    cancelled = client.post(
        f"/api/sandbox-runner/scripts/{run_id}/cancel",
        headers=_HEADERS,
        json={"worker_key": _WORKER_KEY},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True


def test_worker_script_endpoint_rejects_source_digest_mismatch(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A changed snapshot must not launch under the primary's digest receipt."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'d' * 32}", source="print('expected')\n")
    payload["source_digest"] = "0" * 64

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 400
    assert "digest" in response.json()["detail"].lower()


def test_worker_script_endpoint_rejects_caller_selected_source_path(
    runner_client: tuple[TestClient, Path],
    tmp_path: Path,
) -> None:
    """The route does not accept a caller-selected source snapshot path."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'e' * 32}", source="print('no')\n")
    outside = tmp_path / "outside.py"
    outside.write_text("print('escaped')\n", encoding="utf-8")
    payload["source_path"] = str(outside)

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 422
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])


def test_worker_script_endpoint_rejects_caller_selected_token_path(
    runner_client: tuple[TestClient, Path],
) -> None:
    """The route does not accept a caller-selected capability path."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'f' * 32}", source="print('no')\n")
    payload["token_path"] = "capability"  # noqa: S105

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 422
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])


def test_worker_script_endpoint_rejects_unapproved_environment_name(
    runner_client: tuple[TestClient, Path],
) -> None:
    """The launch protocol must not become an arbitrary environment injection channel."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'1' * 32}", source="print('no')\n")
    payload["environment"] = {"MINDROOM_CONTROL_STATE_PATH": "/primary/private"}

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 422
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])


def test_worker_script_endpoint_rejects_oversized_request(
    runner_client: tuple[TestClient, Path],
) -> None:
    """The worker must reject oversized control bodies before process launch."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'2' * 32}", source="print('no')\n")
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
async def test_shared_runner_authenticates_before_revealing_script_topology(tmp_path: Path) -> None:
    """An unauthenticated caller cannot distinguish shared and dedicated runner topology."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={},
    )
    shared_app = FastAPI()
    shared_app.include_router(sandbox_runner_scripts_router)
    sandbox_runner_module.initialize_sandbox_runner_app(
        shared_app,
        runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=_TOKEN,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=shared_app),
        base_url="http://testserver",
    ) as client:
        unauthenticated = await client.post(
            "/api/sandbox-runner/scripts/run",
            json={
                "run_id": f"script-{'a' * 32}",
                "worker_key": _WORKER_KEY,
                "source_digest": "0" * 64,
                "gateway_url": "http://primary:8765/api/script-gateway",
            },
        )
        authenticated = await client.post(
            "/api/sandbox-runner/scripts/run",
            headers=_HEADERS,
            json={
                "run_id": f"script-{'a' * 32}",
                "worker_key": _WORKER_KEY,
                "source_digest": "0" * 64,
                "gateway_url": "http://primary:8765/api/script-gateway",
            },
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 404


@pytest.mark.asyncio
async def test_worker_script_endpoint_replays_valid_chunked_body_for_model_validation(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A bounded streamed body must remain available to FastAPI's Pydantic parser."""
    client, workspace = runner_client
    run_id = f"script-{'3' * 32}"
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
    client.post(
        f"/api/sandbox-runner/scripts/{run_id}/cancel",
        headers=_HEADERS,
        json={"worker_key": _WORKER_KEY, "force": True},
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
            "run_id": f"script-{'4' * 32}",
            "worker_key": "worker-b",
            "source_digest": "a" * 64,
            "gateway_url": "http://primary.test/api/script-gateway",
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
            run_id=f"script-{'5' * 32}",
            source="import time\ntime.sleep(60)\n",
        ),
    )
    assert response.status_code == 200
    owned_run_id = f"script-{'5' * 32}"
    try:
        status = client.get(
            f"/api/sandbox-runner/scripts/script-{'6' * 32}",
            headers=_HEADERS,
            params={"worker_key": _WORKER_KEY},
        )

        assert status.status_code == 200
        assert status.json()["state"] == "unknown"
    finally:
        client.post(
            f"/api/sandbox-runner/scripts/{owned_run_id}/cancel",
            headers=_HEADERS,
            json={"worker_key": _WORKER_KEY, "force": True},
        )


def test_script_namespace_distinguishes_delimiter_ambiguous_identities() -> None:
    """Worker and run boundaries must not depend on ambiguous delimiter concatenation."""
    assert _script_namespace("a:b", "c") != _script_namespace("a", "b:c")


def test_worker_script_launch_rejects_path_like_run_id(
    runner_client: tuple[TestClient, Path],
) -> None:
    """The entire launch run ID must match the filesystem-safe identifier grammar."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'7' * 32}", source="print('no')\n")
    payload["run_id"] = "script-safe/child"

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 422


def test_worker_script_cancel_rejects_caller_selected_handle(
    runner_client: tuple[TestClient, Path],
) -> None:
    """Cancellation derives the handle and rejects caller-selected alternatives."""
    client, _workspace = runner_client

    response = client.post(
        f"/api/sandbox-runner/scripts/script-{'8' * 32}/cancel",
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
