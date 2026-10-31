"""Worker API tests for supervised background Python scripts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mindroom.api import sandbox_runner as sandbox_runner_module
from mindroom.api import sandbox_runner_scripts as sandbox_runner_scripts_module
from mindroom.api.sandbox_runner_app import app as sandbox_runner_app
from mindroom.api.sandbox_runner_scripts import router as sandbox_runner_scripts_router
from mindroom.api.sandbox_worker_prep import prepare_worker_request
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.script_runs import launch_contract
from mindroom.script_runs.models import script_worker_key_for_run
from mindroom.script_runs.supervised import worker_script_namespace
from mindroom.script_runs.wire import SCRIPT_PROTOCOL_VERSION, SandboxScriptRunRequest
from mindroom.shell import supervisor as shell_supervisor_module
from mindroom.shell.supervisor import ShellSupervisorStartupError, _ShellSupervisorManager
from mindroom.tool_system.sandbox_proxy import runner_config_snapshot
from mindroom.tool_system.worker_routing import private_instance_scope_root_path
from mindroom.workers.backends import local as local_workers_module
from tests.api.conftest import fake_local_worker_venv_create

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

_TOKEN = "worker-secret"  # noqa: S105
_HEADERS = {"x-mindroom-sandbox-token": _TOKEN}
_WORKER_KEY = "v1:test:shared:scripts"
_SUPERVISOR_HANDLE = f"shell:{'a' * 32}"


def _use_fake_local_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_workers_module, "_create_local_worker_venv", fake_local_worker_venv_create)
    monkeypatch.setattr(local_workers_module, "_local_worker_manager", None)
    monkeypatch.setattr(local_workers_module, "_local_worker_manager_config", None)


@pytest.fixture
def script_supervisor(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Serve supervised script processes from one real isolated shell supervisor."""
    supervisor = _ShellSupervisorManager()
    monkeypatch.setattr(shell_supervisor_module, "_manager", supervisor)
    try:
        yield
    finally:
        supervisor.shutdown()


@pytest.fixture
def runner_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    script_supervisor: None,
) -> tuple[TestClient, Path]:
    """Provide an authenticated runner with one real isolated supervisor."""
    del script_supervisor
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
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
    _use_fake_local_workers(monkeypatch)
    sandbox_runner_module.initialize_sandbox_runner_app(
        sandbox_runner_app,
        runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=_TOKEN,
    )
    prepared = prepare_worker_request(
        worker_key=_WORKER_KEY,
        base_dir=None,
        runtime_paths=runtime_paths,
        agent_policies={},
        runner_token=_TOKEN,
    )
    return TestClient(sandbox_runner_app), prepared.paths.workspace


def _dedicated_scripts_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker_key: str,
    config_yaml: str,
    private_agent_names: frozenset[str],
) -> tuple[FastAPI, Path]:
    """Return a script-routes app for one dedicated worker over `tmp_path/storage`, and its prepared workspace."""
    dedicated_root = tmp_path / "dedicated-worker"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(config_yaml, encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=dedicated_root,
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: worker_key,
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: str(dedicated_root),
            SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"]: str(tmp_path / "storage"),
        },
    )
    _use_fake_local_workers(monkeypatch)
    app = FastAPI()
    app.include_router(sandbox_runner_scripts_router)
    sandbox_runner_module.initialize_sandbox_runner_app(
        app,
        runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=_TOKEN,
    )
    prepared = prepare_worker_request(
        worker_key=worker_key,
        base_dir=None,
        runtime_paths=runtime_paths,
        agent_policies={},
        private_agent_names=private_agent_names,
        runner_token=_TOKEN,
    )
    return app, prepared.paths.workspace


def _write_run_files(workspace: Path, run_id: str, source: str) -> str:
    """Write one run's snapshot and return its source digest."""
    run_root = workspace / launch_contract.snapshot_dir(run_id)
    run_root.mkdir(parents=True)
    (run_root / launch_contract.SOURCE_FILE_NAME).write_text(source, encoding="utf-8")
    (run_root / launch_contract.CAPABILITY_FILE_NAME).write_text("capability-token", encoding="utf-8")
    return hashlib.sha256(source.encode()).hexdigest()


def _launch_body(
    run_id: str,
    *,
    worker_key: str = _WORKER_KEY,
    source_digest: str = "a" * 64,
    **fields: object,
) -> dict[str, object]:
    return {
        "protocol_version": SCRIPT_PROTOCOL_VERSION,
        "run_id": run_id,
        "worker_key": worker_key,
        "source_digest": source_digest,
        "gateway_url": "http://primary:8765/api/script-gateway",
        "max_runtime_seconds": 3600,
        **fields,
    }


def _run_payload(workspace: Path, *, run_id: str, source: str) -> dict[str, object]:
    return _launch_body(run_id, source_digest=_write_run_files(workspace, run_id, source))


def _control(
    client: TestClient,
    run_id: str,
    operation: str,
    *,
    worker_key: str = _WORKER_KEY,
    **fields: object,
) -> httpx.Response:
    return client.post(
        f"/api/sandbox-runner/scripts/{run_id}/{operation}",
        headers=_HEADERS,
        json={"worker_key": worker_key, **fields},
    )


def _settled_status(client: TestClient, run_id: str, *, worker_key: str) -> httpx.Response:
    """Poll one run's status until it stops running, for at most five seconds."""
    deadline = time.monotonic() + 5
    while True:
        status = _control(client, run_id, "status", worker_key=worker_key)
        if status.json()["state"] != "running" or time.monotonic() >= deadline:
            break
        time.sleep(0.01)
    return status


@pytest.mark.parametrize("protocol_version", [SCRIPT_PROTOCOL_VERSION - 1, SCRIPT_PROTOCOL_VERSION + 1])
def test_worker_script_endpoint_rejects_mismatched_protocol_version(
    runner_client: tuple[TestClient, Path],
    protocol_version: int,
) -> None:
    """A worker must reject launches from an incompatible script protocol."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'0' * 32}", source="print('no')\n")
    payload["protocol_version"] = protocol_version

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "value_error"


@pytest.mark.parametrize(
    ("max_runtime_seconds", "error_type"),
    [
        (0, "greater_than"),
        (-1, "greater_than"),
        (1.5, "int_type"),
        (True, "int_type"),
        (float("inf"), "int_type"),
        (float("nan"), "int_type"),
    ],
)
def test_worker_script_launch_rejects_invalid_maximum_runtime(
    max_runtime_seconds: object,
    error_type: str,
) -> None:
    """The worker accepts only finite positive integer process deadlines."""
    with pytest.raises(ValidationError) as exc_info:
        SandboxScriptRunRequest.model_validate(
            _launch_body(f"script-{'0' * 32}", max_runtime_seconds=max_runtime_seconds),
        )

    assert exc_info.value.errors()[0]["type"] == error_type


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
    )

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error": "Background scripts require Linux process-group containment.",
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

    response = _control(client, run_id, operation)

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == "supervisor unavailable"


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

    status = _control(client, run_id, "status")
    assert status.status_code == 200
    assert status.json()["state"] == "running"

    cancelled = _control(client, run_id, "cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested"] is True


def test_worker_script_receipts_report_exit_and_unknown_handles(
    runner_client: tuple[TestClient, Path],
) -> None:
    """Status and cancellation receipts carry the supervisor's exit, finished, and unknown-handle facts."""
    client, workspace = runner_client
    run_id = f"script-{'d' * 32}"
    launched = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json=_run_payload(workspace, run_id=run_id, source="print('done', flush=True)\n"),
    )
    assert launched.json() == {"ok": True, "error": None}
    status = _settled_status(client, run_id, worker_key=_WORKER_KEY)
    exited = status.json()
    assert exited.pop("output").startswith("Status: FINISHED (exit code 0")
    assert exited == {"ok": True, "state": "exited", "exit_code": 0, "error": None}

    finished = _control(client, run_id, "cancel")
    assert finished.json() == {
        "ok": True,
        "cancel_requested": False,
        "already_finished": True,
        "unknown_handle": False,
        "error": None,
    }

    unknown_run_id = f"script-{'e' * 32}"
    unknown_status = _control(client, unknown_run_id, "status")
    assert unknown_status.json() == {
        "ok": True,
        "state": "unknown",
        "output": "",
        "exit_code": None,
        "error": None,
    }
    unknown_cancel = _control(client, unknown_run_id, "cancel", force=True)
    assert unknown_cancel.json() == {
        "ok": True,
        "cancel_requested": False,
        "already_finished": False,
        "unknown_handle": True,
        "error": None,
    }


def test_worker_script_endpoint_applies_workspace_environment_overlay(
    runner_client: tuple[TestClient, Path],
) -> None:
    """Background scripts inherit trusted workspace env without losing their run identity."""
    client, workspace = runner_client
    run_id = f"script-{'9' * 32}"
    hook_path = workspace / ".mindroom" / "worker-env.sh"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(
        "export SCRIPT_TEST_OVERLAY=from-hook\nexport MINDROOM_SCRIPT_RUN_ID=overridden\n",
        encoding="utf-8",
    )
    response = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json=_run_payload(
            workspace,
            run_id=run_id,
            source=(
                "import os\n"
                "print(f\"overlay={os.environ['SCRIPT_TEST_OVERLAY']}\", flush=True)\n"
                "print(f\"run_id={os.environ['MINDROOM_SCRIPT_RUN_ID']}\", flush=True)\n"
            ),
        ),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    status = _settled_status(client, run_id, worker_key=_WORKER_KEY)

    assert status.json()["state"] == "exited"
    assert status.json()["exit_code"] == 0
    assert "overlay=from-hook" in status.json()["output"]
    assert f"run_id={run_id}" in status.json()["output"]


@pytest.mark.usefixtures("script_supervisor")
def test_private_worker_script_executes_in_canonical_requester_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private script uses its mounted requester workspace, not the run snapshot scratch directory."""
    run_id = f"script-{'a' * 32}"
    state_scope_worker_key = "v1:test:user_agent:@alice:example.test:watcher"
    worker_key = script_worker_key_for_run(state_scope_worker_key, run_id)
    app, snapshot_workspace = _dedicated_scripts_app(
        tmp_path,
        monkeypatch,
        worker_key=worker_key,
        config_yaml="""
agents:
  watcher:
    display_name: Watcher
    role: Watch for changes.
    model: default
    private:
      per: user_agent
      root: private-workspace
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        private_agent_names=frozenset({"watcher"}),
    )
    private_workspace = (
        private_instance_scope_root_path(tmp_path / "storage", state_scope_worker_key) / "watcher"
    ) / "private-workspace"
    private_workspace.mkdir(parents=True)
    hook_path = private_workspace / ".mindroom" / "worker-env.sh"
    hook_path.parent.mkdir(parents=True)
    hook_path.write_text("export SCRIPT_TEST_OVERLAY=private-workspace\n", encoding="utf-8")
    source = (
        "import os\n"
        "from pathlib import Path\n"
        "workspace = Path(os.environ['MINDROOM_SCRIPT_WORKSPACE_ROOT'])\n"
        "workspace.joinpath('script-result.txt').write_text(\n"
        "    f\"{os.environ['SCRIPT_TEST_OVERLAY']}|{Path.cwd()}\",\n"
        "    encoding='utf-8',\n"
        ")\n"
    )
    source_digest = _write_run_files(snapshot_workspace, run_id, source)
    client = TestClient(app)
    response = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json=_launch_body(
            run_id,
            worker_key=worker_key,
            source_digest=source_digest,
            state_scope_worker_key=state_scope_worker_key,
            private_agent_names=["watcher"],
        ),
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    status = _settled_status(client, run_id, worker_key=worker_key)

    assert status.json()["state"] == "exited"
    assert status.json()["exit_code"] == 0
    assert (private_workspace / "script-result.txt").read_text(encoding="utf-8") == (
        f"private-workspace|{private_workspace}"
    )
    assert not (snapshot_workspace / "script-result.txt").exists()


@pytest.mark.usefixtures("script_supervisor")
def test_worker_script_does_not_load_workspace_code_into_its_launchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workspace files must not replace MindRoom's script launchers or inject site code into them."""
    run_id = f"script-{'b' * 32}"
    state_scope_worker_key = "v1:test:user_agent:@victim:example.test:watcher"
    worker_key = script_worker_key_for_run(state_scope_worker_key, run_id)
    app, snapshot_workspace = _dedicated_scripts_app(
        tmp_path,
        monkeypatch,
        worker_key=worker_key,
        config_yaml="""
agents:
  watcher:
    display_name: Watcher
    role: Watch for changes.
    model: default
    memory_backend: file
    worker_scope: user_agent
models:
  default:
    provider: openai
    id: gpt-6-astra
router:
  model: default
""".lstrip(),
        private_agent_names=frozenset(),
    )
    shared_workspace = tmp_path / "storage" / "agents" / "watcher" / "workspace"
    (shared_workspace / "mindroom").mkdir(parents=True)
    (shared_workspace / "mindroom" / "__init__.py").write_text(
        "from pathlib import Path\nPath.cwd().joinpath('planted-package-ran').touch()\n",
        encoding="utf-8",
    )
    (shared_workspace / "helper.py").write_text('VALUE = "helper-value"\n', encoding="utf-8")
    user_site = subprocess.run(
        [sys.executable, "-c", "import site; print(site.getusersitepackages())"],
        env={**os.environ, "HOME": str(shared_workspace.resolve())},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    Path(user_site).mkdir(parents=True)
    (Path(user_site) / "planted.pth").write_text(
        "import pathlib; pathlib.Path.cwd().joinpath('planted-pth-ran').touch()\n",
        encoding="utf-8",
    )
    source = (
        "import os\n"
        "from pathlib import Path\n"
        "import helper\n"
        "workspace = Path(os.environ['MINDROOM_SCRIPT_WORKSPACE_ROOT'])\n"
        "workspace.joinpath('script-result.txt').write_text(helper.VALUE, encoding='utf-8')\n"
    )
    source_digest = _write_run_files(snapshot_workspace, run_id, source)
    client = TestClient(app)
    response = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json=_launch_body(
            run_id,
            worker_key=worker_key,
            source_digest=source_digest,
            state_scope_worker_key=state_scope_worker_key,
            private_agent_names=[],
        ),
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    status = _settled_status(client, run_id, worker_key=worker_key)

    assert status.json()["state"] == "exited"
    assert status.json()["exit_code"] == 0
    assert (shared_workspace / "script-result.txt").read_text(encoding="utf-8") == "helper-value"
    assert not (shared_workspace / "planted-package-ran").exists()
    assert not (shared_workspace / "planted-pth-ran").exists()


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


def test_worker_script_endpoint_rejects_oversized_request(
    runner_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker must reject oversized control bodies before process launch."""
    monkeypatch.setattr(sandbox_runner_scripts_module, "_MAX_REQUEST_BYTES", 16 * 1024)
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'2' * 32}", source="print('no')\n")
    payload["environment"] = {"MINDROOM_SCRIPT_GATEWAY_URL": f"http://primary.test/{'x' * 20_000}"}

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_worker_script_endpoint_stops_reading_chunked_body_over_limit(
    runner_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body boundary must stop receiving chunks as soon as the limit is crossed."""
    monkeypatch.setattr(sandbox_runner_scripts_module, "_MAX_REQUEST_BYTES", 16 * 1024)
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
            json=_launch_body(f"script-{'a' * 32}"),
        )
        authenticated = await client.post(
            "/api/sandbox-runner/scripts/run",
            headers=_HEADERS,
            json=_launch_body(f"script-{'a' * 32}"),
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
    _control(client, run_id, "cancel", force=True)


def test_worker_script_endpoint_rejects_mismatched_dedicated_worker_key(tmp_path: Path) -> None:
    """A worker-scoped auth endpoint must not control a sibling worker namespace."""
    previous_context = getattr(sandbox_runner_app.state, "sandbox_runner_context", None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
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
        json=_launch_body(f"script-{'4' * 32}", worker_key="worker-b"),
    )

    assert response.status_code == 400
    assert "dedicated worker" in response.json()["detail"].lower()
    assert getattr(sandbox_runner_app.state, "sandbox_runner_context", None) is previous_context


def test_worker_script_status_is_bound_to_run_namespace(
    runner_client: tuple[TestClient, Path],
) -> None:
    """Knowing a handle must not make it visible through another run ID."""
    client, workspace = runner_client
    owned_run_id = f"script-{'5' * 32}"
    response = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json=_run_payload(workspace, run_id=owned_run_id, source="import time\ntime.sleep(60)\n"),
    )
    assert response.status_code == 200
    try:
        status = _control(client, f"script-{'6' * 32}", "status")

        assert status.status_code == 200
        assert status.json()["state"] == "unknown"
    finally:
        _control(client, owned_run_id, "cancel", force=True)


def test_script_namespace_distinguishes_delimiter_ambiguous_identities() -> None:
    """Worker and run boundaries must not depend on ambiguous delimiter concatenation."""
    assert worker_script_namespace("a:b", "c") != worker_script_namespace("a", "b:c")


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

    response = _control(client, f"script-{'8' * 32}", "cancel", supervisor_handle=f"{_SUPERVISOR_HANDLE}-suffix")

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


@pytest.mark.parametrize("layout", ["missing", "linked", "mounted"])
def test_script_state_workspace_is_only_the_mounted_workspace(tmp_path: Path, layout: str) -> None:
    """Script workers use the mounted workspace as is and never create a stand-in or follow a replacement."""
    state_scope_worker_key = "v1:test:user_agent:@alice:localhost:watcher"
    shared_storage_root = tmp_path / "storage"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "agents:\n  watcher:\n    display_name: Watcher\n    private:\n      per: user_agent\n"
        "      root: private-workspace\nmodels:\n  default:\n    provider: openai\n    id: gpt-6-astra\n"
        "router:\n  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "dedicated-worker",
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: state_scope_worker_key,
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: str(tmp_path / "dedicated-worker"),
            SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"]: str(shared_storage_root),
        },
    )
    app = FastAPI()
    sandbox_runner_module.initialize_sandbox_runner_app(
        app,
        runtime_paths,
        config=sandbox_runner_module._runtime_config_or_empty(runtime_paths),
        runner_token=_TOKEN,
    )
    agent_root = private_instance_scope_root_path(shared_storage_root, state_scope_worker_key) / "watcher"
    workspace = agent_root / "private-workspace"
    if layout == "mounted":
        workspace.mkdir(parents=True)
    elif layout == "linked":
        other = tmp_path / "other-instance"
        other.mkdir()
        agent_root.mkdir(parents=True)
        workspace.symlink_to(other, target_is_directory=True)

    def resolve() -> Path:
        return sandbox_runner_module.resolve_script_state_workspace(
            app,
            sandbox_runner_module.app_runtime_config(app),
            state_scope_worker_key=state_scope_worker_key,
            agent_name="watcher",
            private_agent_names=frozenset({"watcher"}),
        )

    if layout == "mounted":
        assert resolve() == workspace
        return
    with pytest.raises(ValueError, match="not mounted"):
        resolve()
    assert workspace.is_symlink() == (layout == "linked")
    assert agent_root.exists() == (layout == "linked")


_LIVE_SCRIPT_STATE_SCOPE = "v1:test:user_agent:@alice:example.test:watcher"
_LIVE_SCRIPT_RUN_ID = f"script-{'e' * 32}"


def _live_primary_script_snapshot(tmp_path: Path, *, private: bool) -> dict[str, object]:
    """Return what a Kubernetes primary sends for a `watcher` agent created after the worker was seeded."""
    watcher: dict[str, object] = {"display_name": "Watcher", "tools": ["script"]}
    if private:
        watcher["private"] = {"per": "user_agent", "root": "private-workspace"}
    else:
        watcher["worker_scope"] = "user_agent"
    primary_paths = resolve_runtime_paths(
        config_path=tmp_path / "primary" / "config.yaml",
        storage_path=tmp_path / "primary" / "storage",
        process_env={"MINDROOM_WORKER_BACKEND": "kubernetes"},
    )
    live_config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra", "api_key": "sk-live-model-key"}},
            "router": {"model": "default"},
            # Snapshots grow with the agent count, so launches must fit well past the old 16 KiB body limit.
            "agents": {
                "watcher": watcher,
                **{
                    f"helper_{index:03d}": {"display_name": f"Helper {index}", "tools": ["shell", "file", "python"]}
                    for index in range(256)
                },
            },
        },
        primary_paths,
    )
    snapshot = runner_config_snapshot(primary_paths, live_config)
    assert snapshot is not None
    assert "sk-live-model-key" not in json.dumps(snapshot)
    return snapshot


@pytest.fixture
def seeded_script_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script_supervisor: None,
) -> tuple[TestClient, dict[str, object], Path]:
    """Provide a dedicated worker for a private `watcher` script run whose seed config lacks that agent."""
    del script_supervisor
    worker_key = script_worker_key_for_run(_LIVE_SCRIPT_STATE_SCOPE, _LIVE_SCRIPT_RUN_ID)
    app, snapshot_workspace = _dedicated_scripts_app(
        tmp_path,
        monkeypatch,
        worker_key=worker_key,
        config_yaml="models:\n  default:\n    provider: openai\n    id: gpt-6-astra\nagents: {}\nrouter:\n  model: default\n",
        private_agent_names=frozenset({"watcher"}),
    )
    assert "watcher" not in sandbox_runner_module.app_runtime_config(app).agents
    private_workspace = (
        private_instance_scope_root_path(tmp_path / "storage", _LIVE_SCRIPT_STATE_SCOPE) / "watcher"
    ) / "private-workspace"
    private_workspace.mkdir(parents=True)
    source = (
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['MINDROOM_SCRIPT_WORKSPACE_ROOT'], 'result.txt').write_text('live', encoding='utf-8')\n"
    )
    payload = _launch_body(
        _LIVE_SCRIPT_RUN_ID,
        worker_key=worker_key,
        source_digest=_write_run_files(snapshot_workspace, _LIVE_SCRIPT_RUN_ID, source),
        state_scope_worker_key=_LIVE_SCRIPT_STATE_SCOPE,
        private_agent_names=["watcher"],
    )
    return TestClient(app), payload, private_workspace


def test_worker_script_runs_agent_known_only_to_primary_config_snapshot(
    seeded_script_worker: tuple[TestClient, dict[str, object], Path],
    tmp_path: Path,
) -> None:
    """A script for an agent added after the worker was seeded runs in that agent's workspace via the snapshot."""
    client, payload, private_workspace = seeded_script_worker

    without_snapshot = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)
    assert without_snapshot.status_code == 400
    assert without_snapshot.json()["detail"] == "Script state scope does not resolve an agent workspace."

    snapshot = _live_primary_script_snapshot(tmp_path, private=True)
    assert len(json.dumps(snapshot)) > 16 * 1024
    response = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json={**payload, "config_snapshot": snapshot},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    status = _settled_status(client, _LIVE_SCRIPT_RUN_ID, worker_key=str(payload["worker_key"]))
    assert status.json()["state"] == "exited"
    assert status.json()["exit_code"] == 0
    assert (private_workspace / "result.txt").read_text(encoding="utf-8") == "live"


def test_worker_script_enforces_snapshot_agent_visibility(
    seeded_script_worker: tuple[TestClient, dict[str, object], Path],
    tmp_path: Path,
) -> None:
    """The snapshot decides whether the agent is private, so a private launch for a now-shared agent is refused."""
    client, payload, private_workspace = seeded_script_worker

    response = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json={**payload, "config_snapshot": _live_primary_script_snapshot(tmp_path, private=False)},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Script state scope does not match private-agent visibility."
    assert not (private_workspace / "result.txt").exists()


def test_worker_script_rejects_invalid_config_snapshot(
    seeded_script_worker: tuple[TestClient, dict[str, object], Path],
    tmp_path: Path,
) -> None:
    """An invalid snapshot is rejected instead of falling back to the worker's seed config."""
    client, payload, _private_workspace = seeded_script_worker
    snapshot = _live_primary_script_snapshot(tmp_path, private=True)
    agents = snapshot["agents"]
    assert isinstance(agents, dict)
    agents["watcher"]["file_access"] = "everything"

    response = client.post(
        "/api/sandbox-runner/scripts/run",
        headers=_HEADERS,
        json={**payload, "config_snapshot": snapshot},
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Invalid config_snapshot:")


def test_worker_script_launch_reports_a_supervisor_refusal(
    runner_client: tuple[TestClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supervisor that refuses the run's handle fails the launch with its refusal."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'9' * 32}", source="print('must not run')\n")

    async def refuse(*_args: object, **_kwargs: object) -> str:
        return "Error: handle is already in use"

    monkeypatch.setattr(sandbox_runner_scripts_module, "launch_supervised_script", refuse)

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 200
    assert response.json() == {"ok": False, "error": "Error: handle is already in use"}


def test_worker_script_launch_rejects_a_state_scope_that_does_not_own_its_worker(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A state scope whose run-pinned key is not the launch's worker key cannot pick the run's workspace."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'a' * 32}", source="print('must not run')\n")
    payload["state_scope_worker_key"] = "v1:test:user_agent:@alice:example.test:watcher"

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "Script state scope does not own this run worker."


@pytest.mark.parametrize("operation", ["status", "cancel"])
def test_worker_script_controls_reject_path_like_run_id(
    runner_client: tuple[TestClient, Path],
    operation: str,
) -> None:
    """Status and cancel accept only the filesystem-safe run ID grammar."""
    client, _workspace = runner_client

    response = _control(client, "script-..", operation)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "gateway_url",
    ["ftp://primary:8765/api/script-gateway", "http://user:secret@primary:8765/api/script-gateway"],
    ids=["scheme", "user_info"],
)
def test_worker_script_launch_rejects_an_unsafe_gateway_url(
    runner_client: tuple[TestClient, Path],
    gateway_url: str,
) -> None:
    """The shim only ever gets a plain HTTP(S) gateway URL without credentials."""
    client, workspace = runner_client
    payload = _run_payload(workspace, run_id=f"script-{'b' * 32}", source="print('must not run')\n")
    payload["gateway_url"] = gateway_url

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "Script gateway environment must contain a safe HTTP(S) URL."


def test_worker_script_launch_rejects_an_oversized_capability_file(
    runner_client: tuple[TestClient, Path],
) -> None:
    """A capability file over the token limit is refused before launch."""
    client, workspace = runner_client
    run_id = f"script-{'c' * 32}"
    payload = _run_payload(workspace, run_id=run_id, source="print('must not run')\n")
    capability = workspace / launch_contract.snapshot_dir(run_id) / launch_contract.CAPABILITY_FILE_NAME
    capability.write_text("x" * (launch_contract.MAX_TOKEN_BYTES + 1), encoding="utf-8")

    response = client.post("/api/sandbox-runner/scripts/run", headers=_HEADERS, json=payload)

    assert response.status_code == 400
    assert response.json()["detail"] == "Script capability file exceeds its supported size."
