"""Worker API tests for supervised background Python scripts."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from mindroom import shell_supervisor as shell_supervisor_module
from mindroom.api import sandbox_runner as sandbox_runner_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.api.sandbox_worker_prep import prepare_worker_request
from mindroom.constants import resolve_runtime_paths
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.shell_supervisor import _ShellSupervisorManager
from mindroom.workers.backends import local as local_workers_module

if TYPE_CHECKING:
    from collections.abc import Iterator

_TOKEN = "worker-secret"  # noqa: S105
_HEADERS = {"x-mindroom-sandbox-token": _TOKEN}
_WORKER_KEY = "v1:test:shared:scripts"


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


def test_worker_script_endpoint_rejects_mismatched_dedicated_worker_key(tmp_path: Path) -> None:
    """A worker-scoped auth endpoint must not control a sibling worker namespace."""
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
    sandbox_runner_module.initialize_sandbox_runner_app(
        sandbox_runner_app,
        runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=_TOKEN,
    )

    response = TestClient(sandbox_runner_app).post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json={
            "run_id": "run-sibling",
            "worker_key": "worker-b",
            "source_path": "source.py",
            "source_digest": "a" * 64,
            "token_path": "capability",
            "environment": {"MINDROOM_SCRIPT_GATEWAY_URL": "http://primary.test/api/script-gateway"},
        },
    )

    assert response.status_code == 400
    assert "dedicated worker" in response.json()["detail"].lower()


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
