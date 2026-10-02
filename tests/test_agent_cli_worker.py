"""Isolated CLI workers reuse shell semantics without receiving primary authority."""
# ruff: noqa: D103, S106, S107 - isolated tests use fake credentials

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
import pytest
from agno.tools.toolkit import Toolkit
from fastapi import FastAPI
from structlog.testing import capture_logs

from mindroom.agent_cli import shell_invocation
from mindroom.agent_cli import worker as cli_worker
from mindroom.agent_cli.session import CliAuthenticationError, TurnToolBridge, cli_turn_owner
from mindroom.agent_cli.worker import CliWorkerLease, _cli_worker_spec, open_cli_worker
from mindroom.agent_cli.worker_network import cli_primary_url, probe_cli_network
from mindroom.agent_cli.worker_protocol import CliShellSettings, CliWorkerLaunch
from mindroom.api import sandbox_runner_cli
from mindroom.api.sandbox_runner import initialize_sandbox_runner_app
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import (
    DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES,
    DEFAULT_TOOL_OUTPUT_MAX_BYTES,
    resolve_primary_runtime_paths,
)
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.runtime_state import clear_api_server_address, set_api_server_address
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    private_instance_scope_root_path,
    resolve_worker_key,
    resolved_worker_key_scope,
)
from mindroom.workers import runtime as worker_runtime
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends._dedicated_worker_common import resolve_state_scope_worker_key
from mindroom.workers.compatibility import WORKER_PROTOCOL_VERSION
from mindroom.workers.models import WorkerHandle, WorkerSpec, is_cli_worker_key, process_worker_key
from tests.conftest import bind_runtime_paths
from tests.test_agent_cli_authority import _runtime_context, _turn_context
from tests.test_docker_worker_backend import _backend

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

KEY = "v1:default:user_agent:~alice:!agent-turn-00000000000000000000000000000001:code"
BASE = "v1:default:user_agent:alice:code"
USER = "v1:default:user:alice"


def _app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    worker_key: str = KEY,
    runner_token: str = "worker-only",
) -> tuple[FastAPI, Path]:
    """Build a CLI worker runner like Docker starts it: dedicated, with no agent config."""
    storage = tmp_path / "storage"
    workspace = storage / "agents/code/workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    runtime = resolve_primary_runtime_paths(
        config_path=tmp_path / "missing.yaml",
        storage_path=storage,
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: worker_key,
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: str(storage),
        },
    )
    app = FastAPI()
    initialize_sandbox_runner_app(app, runtime, config=Config(), runner_token=runner_token)
    app.include_router(sandbox_runner_cli.router)
    monkeypatch.setattr(sandbox_runner_cli, "_CLI_PRIVATE_ROOT", tmp_path / "private")

    async def network_ok(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sandbox_runner_cli, "probe_cli_network", network_ok)
    return app, workspace


def _shell(workspace: str) -> CliShellSettings:
    return CliShellSettings(
        workspace=workspace,
        shell_path_prepend=None,
        output_max_bytes=DEFAULT_TOOL_OUTPUT_MAX_BYTES,
        output_auto_save_threshold_bytes=DEFAULT_TOOL_OUTPUT_AUTO_SAVE_THRESHOLD_BYTES,
    )


def _launch(workspace: Path) -> dict[str, object]:
    return {
        "protocol_version": WORKER_PROTOCOL_VERSION,
        "worker_key": KEY,
        "private_agent_names": [],
        "turn_id": "turn",
        "generation": "generation",
        "token": "turn-capability-0123456789",
        "primary_url": "http://primary:8766",
        "control_urls": [],
        "shell": _shell(str(workspace)).model_dump(),
    }


@pytest.mark.asyncio
async def test_authenticated_install_is_once_and_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, workspace = _app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        route = "/api/sandbox-runner/agent-cli/install"
        assert (await client.post(route, json=_launch(workspace))).status_code == 401
        headers = {"x-mindroom-sandbox-token": "worker-only"}
        response = await client.post(route, headers=headers, json=_launch(workspace))
        assert response.status_code == 200, response.text
        token_file = tmp_path / "private/capability"
        assert token_file.read_text() == "turn-capability-0123456789"
        assert token_file.stat().st_mode & 0o777 == 0o600
        assert not token_file.is_relative_to(tmp_path / "storage")
        assert "turn-capability-0123456789" not in response.text
        assert (await client.post(route, headers=headers, json=_launch(workspace))).status_code == 409
        assert not list((tmp_path / "storage").rglob("capability"))


@pytest.mark.asyncio
async def test_shell_runs_normal_argv_output_and_rejects_foreign_handles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, workspace = _app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"x-mindroom-sandbox-token": "worker-only"},
    ) as client:
        response = await client.post("/api/sandbox-runner/agent-cli/install", json=_launch(workspace))
        assert response.status_code == 200, response.text
        route = "/api/sandbox-runner/agent-cli/shell"
        handle = "shell:" + "a" * 32
        run = {
            "worker_key": KEY,
            "handle": handle,
            "operation": {
                "function_name": "run_shell_command",
                "args": ["bash", "-c", "printf '%s' \"$MINDROOM_AGENT_CLI_TOKEN_PATH\"; printf '\\n'; pwd"],
                "tail": 2,
            },
        }
        response = await client.post(route, json=run)
        assert response.status_code == 200, response.text
        assert str(tmp_path / "private/capability") in response.json()["result"]
        assert str(workspace) in response.json()["result"]
        response = await client.post(
            route,
            json={
                "worker_key": KEY,
                "handle": "shell:" + "b" * 32,
                "operation": {"function_name": "kill_shell_command", "force": True},
            },
        )
        assert response.status_code == 403
        response = await client.post(
            route,
            json={"worker_key": KEY, "handle": handle, "operation": {"function_name": "check_shell_command"}},
        )
        assert response.status_code == 200
        assert (
            "unknown handle" in response.json()["result"].lower()
        )  # Completed foreground runs have no background record.


def _primary_response(request: httpx.Request, *, accepts_grant: bool = True) -> httpx.Response:
    """Answer like a protected primary: only this worker's grant reaches its CLI operations."""
    if (
        accepts_grant
        and request.method == "POST"
        and request.url.path == "/api/agent-cli/operations"
        and request.headers.get("authorization") == "Bearer turn-capability-0123456789"
    ):
        return httpx.Response(200, json={"items": []})
    return httpx.Response(401)


@pytest.mark.asyncio
async def test_probe_rejects_unprotected_known_route() -> None:
    launch = CliWorkerLaunch.model_validate(_launch(Path("/app/worker/agents/code/workspace")))

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="protected"):
            await probe_cli_network(launch, control_token="worker-only", client=client)


@pytest.mark.asyncio
@pytest.mark.parametrize("accepts_grant", [True, False])
async def test_probe_requires_protected_routes_and_this_primarys_cli(accepts_grant: bool) -> None:
    """Protected routes reject every worker credential; only the turn's own grant proves the URL names this MindRoom."""
    launch = CliWorkerLaunch.model_validate(_launch(Path("/app/worker/agents/code/workspace")))
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _primary_response(request, accepts_grant=accepts_grant)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if accepts_grant:
            await probe_cli_network(launch, control_token="worker-only", client=client)
        else:
            with pytest.raises(ValueError, match="cannot reach this MindRoom"):
                await probe_cli_network(launch, control_token="worker-only", client=client)
    assert any(request.url.path == "/api/config/raw" for request in seen)
    assert any(request.url.path == "/v1/models" for request in seen)
    assert all("operator" not in json.dumps(dict(request.headers)) for request in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_after_refusal", [False, True])
async def test_probe_accepts_closed_peer_but_rejects_unsafe_peer_response(unsafe_after_refusal: bool) -> None:
    """A peer starting or stopping cannot block a turn unless it accepts shell authority."""
    payload = _launch(Path("/app/worker/agents/code/workspace"))
    payload["control_urls"] = ["http://peer:8766"]
    launch = CliWorkerLaunch.model_validate(payload)
    peer_requests = 0
    peer_headers: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal peer_requests
        if request.url.host == "peer":
            peer_requests += 1
            peer_headers.append(json.dumps(dict(request.headers)))
            if unsafe_after_refusal and peer_requests > 1:
                return httpx.Response(200)
            message = "Connection refused"
            raise httpx.ConnectError(message, request=request)
        return _primary_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        if unsafe_after_refusal:
            with pytest.raises(ValueError, match="protected"):
                await probe_cli_network(launch, control_token="worker-only", client=client)
        else:
            await probe_cli_network(launch, control_token="worker-only", client=client)
    # Peers never validate this worker's credentials, so they only see throwaway values.
    assert peer_headers
    assert not any(
        secret in headers for headers in peer_headers for secret in (launch.token.get_secret_value(), "worker-only")
    )


@pytest.mark.asyncio
async def test_probe_requires_reachable_primary() -> None:
    """Only unavailable peers are acceptable; the primary must be verified."""
    launch = CliWorkerLaunch.model_validate(_launch(Path("/app/worker/agents/code/workspace")))

    def respond(request: httpx.Request) -> httpx.Response:
        message = "Connection refused"
        raise httpx.ConnectError(message, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.ConnectError):
            await probe_cli_network(launch, control_token="worker-only", client=client)


@pytest.mark.asyncio
async def test_probe_checks_peers_concurrently_and_rechecks_each_launch() -> None:
    """Independent slow peer checks overlap, without trusting a previous launch's topology."""
    payload = _launch(Path("/app/worker/agents/code/workspace"))
    payload["control_urls"] = [f"http://peer-{index}:8766" for index in range(12)]
    launch = CliWorkerLaunch.model_validate(payload)
    release = asyncio.Event()
    overlapping = asyncio.Event()
    active = peak = 0
    unsafe = False

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        if request.url.host.startswith("peer-"):
            active += 1
            peak = max(peak, active)
            if active > 1:
                overlapping.set()
            try:
                await release.wait()
                return httpx.Response(200 if unsafe else 401)
            finally:
                active -= 1
        return _primary_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        probe = asyncio.create_task(probe_cli_network(launch, control_token="worker-only", client=client))
        try:
            await asyncio.wait_for(overlapping.wait(), timeout=1)
        finally:
            release.set()
            await probe
        assert 1 < peak < 12
        unsafe = True
        with pytest.raises(ValueError, match="protected"):
            await probe_cli_network(launch, control_token="worker-only", client=client)


@pytest.mark.asyncio
async def test_worker_lease_cancellation_drains_startup_then_retires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a pending Docker ensure must not leak its eventual container."""
    runtime = _runtime_context(tmp_path)
    runtime = replace(
        runtime,
        runtime_paths=replace(
            runtime.runtime_paths,
            process_env=MappingProxyType(
                {
                    "MINDROOM_API_KEY": "operator-only",
                    "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://primary:8766",
                },
            ),
        ),
    )
    backend, docker, _ = _backend(monkeypatch, tmp_path)
    backend.config = replace(backend.config, extra_env={})
    started = threading.Event()
    finish = threading.Event()
    original = backend.ensure_worker

    def delayed(spec: WorkerSpec) -> WorkerHandle:
        started.set()
        assert finish.wait(5)
        return original(spec)

    monkeypatch.setattr(backend, "ensure_worker", delayed)

    async def acquire() -> None:
        async with open_cli_worker(backend, runtime):
            pytest.fail("cancelled acquisition yielded a worker")

    task = asyncio.create_task(acquire())
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(docker.containers.created_containers) == 1
    assert docker.containers.created_containers[0].removed == 1


@pytest.mark.asyncio
async def test_lease_rejects_foreign_handle_before_http(tmp_path: Path) -> None:
    context = _runtime_context(tmp_path)
    spec = _cli_worker_spec(context)
    handle = WorkerHandle(
        "worker-id",
        spec.worker_key,
        "http://worker/api/sandbox-runner/execute",
        "worker-only",
        "ready",
        "docker",
        0,
        0,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("foreign handle reached worker")),
    ) as client:
        lease = CliWorkerLease(handle, client, context, spec)
        with pytest.raises(ValueError, match="handle"):
            await lease.invoke_shell("kill_shell_command", {"handle": "shell:" + "f" * 32})


@pytest.mark.asyncio
async def test_lease_reports_worker_image_without_cli_routes(tmp_path: Path) -> None:
    """An older worker image lacks the CLI routes; its 404 names the image, not a transport fault."""
    context = _runtime_context(tmp_path)
    spec = _cli_worker_spec(context)
    handle = WorkerHandle(
        "worker-id",
        spec.worker_key,
        "http://worker/api/sandbox-runner/execute",
        "worker-only",
        "ready",
        "docker",
        0,
        0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as client:
        lease = CliWorkerLease(handle, client, context, spec)
        with pytest.raises(RuntimeError, match="does not support minimal mode"):
            await lease._post("agent-cli-install", {})


def test_restart_process_nonce_is_independent_of_preserved_turn(tmp_path: Path) -> None:
    """A preserved native generation cannot recover an old physical shell process."""
    context = _runtime_context(tmp_path)
    first, second = _cli_worker_spec(context), _cli_worker_spec(context)
    assert first.worker_key != second.worker_key
    assert first.state_scope_worker_key == second.state_scope_worker_key
    assert first.mirrored_credential_services == second.mirrored_credential_services == frozenset()


@pytest.mark.parametrize("server", ["agent-turn-", "!agent-turn-"])
def test_requester_ids_cannot_forge_cli_worker_keys(server: str) -> None:
    """A requester whose ID mimics the process segment still gets an ordinary worker key."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id=f"@bob:{server}{UUID(int=1).hex}",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    ordinary = resolve_worker_key("user_agent", identity)
    assert ordinary is not None
    assert not is_cli_worker_key(ordinary)
    assert is_cli_worker_key(process_worker_key(ordinary, purpose="agent-turn", process_id=UUID(int=2)))


@pytest.mark.asyncio
async def test_lease_revokes_without_redundant_kills_and_keeps_canonical_argv(tmp_path: Path) -> None:
    """Closing revokes the lease; the owning context retires the whole worker."""
    context = _runtime_context(tmp_path)
    context = replace(
        context,
        runtime_paths=replace(
            context.runtime_paths,
            process_env=MappingProxyType(
                {
                    "MINDROOM_API_KEY": "primary-only",
                    "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://primary",
                },
            ),
        ),
    )
    spec = _cli_worker_spec(context)
    worker = WorkerHandle(
        "actual-id",
        spec.worker_key,
        "http://worker/api/sandbox-runner/execute",
        "independent-control",
        "ready",
        "docker",
        0,
        0,
    )
    bridge = TurnToolBridge(cli_turn_owner(context, _turn_context(), worker_id=worker.worker_id))
    now = time.time_ns()
    grant = bridge.issue(now_ns=now, expires_at_ns=now + 10**12)
    messages = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary":
            assert request.headers["authorization"] == "Bearer primary-only"
            return httpx.Response(200)
        assert request.headers["x-mindroom-sandbox-token"] == "independent-control"
        assert "primary-only" not in request.content.decode()
        assert request.url.path in {"/api/sandbox-runner/agent-cli/install", "/api/sandbox-runner/agent-cli/shell"}
        payload = json.loads(request.content)
        messages.append(payload)
        if request.url.path.endswith("/shell"):
            return httpx.Response(200, json={"result": "done"})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        lease = CliWorkerLease(worker, client, context, spec)
        await lease.install_grant(
            bridge,
            grant,
            shell=_shell("/app/worker/agents/helper/workspace"),
        )
        assert await lease.invoke_shell("run_shell_command", {"args": ["printf", "%s", "hello"]}) == "done"
        await lease.close()
    assert messages[1]["operation"]["args"] == ["printf", "%s", "hello"]
    assert len(messages) == 2
    with pytest.raises(CliAuthenticationError):
        bridge.authenticate(grant.raw_token, now_ns=now)


@pytest.mark.asyncio
async def test_worker_retirement_waits_for_revoked_shell_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context exit revokes and drains shell IO before removing the exact container."""
    context = _runtime_context(tmp_path)
    context = replace(
        context,
        runtime_paths=replace(
            context.runtime_paths,
            process_env=MappingProxyType(
                {
                    "MINDROOM_API_KEY": "primary-only",
                    "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://primary",
                },
            ),
        ),
    )
    backend, docker, _ = _backend(monkeypatch, tmp_path)
    backend.config = replace(backend.config, extra_env={})
    monkeypatch.setattr(backend, "inspect_cli_worker", lambda _handle: ())
    shell_started = asyncio.Event()
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()
    operations = []

    async def run() -> None:
        async with open_cli_worker(backend, context) as lease:
            bridge = TurnToolBridge(cli_turn_owner(context, _turn_context(), worker_id=lease.handle.worker_id))
            now = time.time_ns()
            grant = bridge.issue(now_ns=now, expires_at_ns=now + 10**12)

            async def respond(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/shell"):
                    operation = json.loads(request.content)["operation"]["function_name"]
                    operations.append(operation)
                    if operation == "run_shell_command":
                        shell_started.set()
                        try:
                            await asyncio.Event().wait()
                        finally:
                            with pytest.raises(CliAuthenticationError):
                                bridge.authenticate(grant.raw_token, now_ns=now)
                            drain_started.set()
                            await release_drain.wait()
                return httpx.Response(200, json={"result": "done"})

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                lease.client = client
                await lease.install_grant(
                    bridge,
                    grant,
                    shell=_shell(str(context.runtime_paths.storage_root / "agents/helper/workspace")),
                )
                shell = asyncio.create_task(lease.invoke_shell("run_shell_command", {"args": ["true"]}))
                await shell_started.wait()
                await lease.close()
                with pytest.raises(asyncio.CancelledError):
                    await shell

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(drain_started.wait(), timeout=5)
        assert not task.done()
        assert docker.containers.created_containers[0].removed == 0
    finally:
        release_drain.set()
        await task
    assert operations == ["run_shell_command"]
    assert len(docker.containers.created_containers) == 1
    assert docker.containers.created_containers[0].removed == 1


@pytest.mark.asyncio
async def test_worker_startup_failure_never_yields_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = _runtime_context(tmp_path)
    context = replace(
        context,
        runtime_paths=replace(
            context.runtime_paths,
            process_env=MappingProxyType(
                {
                    "MINDROOM_API_KEY": "operator-only",
                    "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://primary",
                },
            ),
        ),
    )
    backend, docker, _ = _backend(monkeypatch, tmp_path)
    backend.config = replace(backend.config, extra_env={})

    def fail_ready(_container: object) -> str:
        message = "cold startup failure"
        raise WorkerBackendError(message)

    monkeypatch.setattr(backend, "_wait_for_ready", fail_ready)
    with pytest.raises(WorkerBackendError, match="cold startup failure"):
        async with open_cli_worker(backend, context):
            pytest.fail("failed worker yielded")
    assert docker.containers.created_containers[-1].removed == 1


@pytest.mark.asyncio
async def test_worker_heartbeat_logs_touch_failure_and_keeps_touching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed touch is visible and retried so idle cleanup cannot reap a live turn."""
    context = _runtime_context(tmp_path)
    context = replace(
        context,
        runtime_paths=replace(
            context.runtime_paths,
            process_env=MappingProxyType(
                {
                    "MINDROOM_API_KEY": "operator-only",
                    "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://primary",
                },
            ),
        ),
    )
    backend, _docker, _ = _backend(monkeypatch, tmp_path, idle_timeout_seconds=0.3)
    backend.config = replace(backend.config, extra_env={})
    monkeypatch.setattr(backend, "inspect_cli_worker", lambda _handle: ())
    touches: list[str] = []
    retried = threading.Event()

    def touch(worker_key: str) -> None:
        touches.append(worker_key)
        if len(touches) == 1:
            message = "temporary Docker API failure"
            raise WorkerBackendError(message)
        retried.set()

    monkeypatch.setattr(backend, "touch_worker", touch)
    with capture_logs() as logs:
        async with open_cli_worker(backend, context) as lease:
            assert await asyncio.to_thread(retried.wait, 5)
    assert set(touches) == {lease.handle.worker_key}
    failures = [entry for entry in logs if entry["event"] == "CLI worker heartbeat failed"]
    assert len(failures) == 1
    assert failures[0]["worker_id"] == lease.handle.worker_id
    assert failures[0]["log_level"] == "error"


@pytest.mark.asyncio
async def test_worker_sync_poll_is_joined_on_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, workspace = _app(tmp_path, monkeypatch)
    started = threading.Event()
    finished = threading.Event()

    def check_shell_command(handle: str) -> str:
        started.set()
        assert finished.wait(5)
        return handle

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"x-mindroom-sandbox-token": "worker-only"},
    ) as client:
        assert (await client.post("/api/sandbox-runner/agent-cli/install", json=_launch(workspace))).status_code == 200
        handle = "shell:" + "e" * 32
        assert (
            await client.post(
                "/api/sandbox-runner/agent-cli/shell",
                json={
                    "worker_key": KEY,
                    "handle": handle,
                    "operation": {"function_name": "run_shell_command", "args": "true"},
                },
            )
        ).status_code == 200
        monkeypatch.setattr(
            shell_invocation,
            "shell_tools",
            lambda: lambda **_kwargs: Toolkit(name="shell", tools=[check_shell_command]),
        )
        task = asyncio.create_task(
            client.post(
                "/api/sandbox-runner/agent-cli/shell",
                json={"worker_key": KEY, "handle": handle, "operation": {"function_name": "check_shell_command"}},
            ),
        )
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finished.set()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_cancelled_shell_revokes_before_transport_shutdown(tmp_path: Path) -> None:
    context = _runtime_context(tmp_path)
    context = replace(
        context,
        runtime_paths=replace(
            context.runtime_paths,
            process_env=MappingProxyType(
                {
                    "MINDROOM_API_KEY": "primary-only",
                    "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://primary",
                },
            ),
        ),
    )
    spec = _cli_worker_spec(context)
    worker = WorkerHandle(
        "actual-id",
        spec.worker_key,
        "http://worker/api/sandbox-runner/execute",
        "independent-control",
        "ready",
        "docker",
        0,
        0,
    )
    bridge = TurnToolBridge(cli_turn_owner(context, _turn_context(), worker_id=worker.worker_id))
    now = time.time_ns()
    grant = bridge.issue(now_ns=now, expires_at_ns=now + 10**12)
    started = asyncio.Event()
    revoked_at_shutdown = []

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/shell"):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                try:
                    bridge.authenticate(grant.raw_token, now_ns=now)
                except CliAuthenticationError:
                    revoked_at_shutdown.append(True)
                else:
                    revoked_at_shutdown.append(False)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        lease = CliWorkerLease(worker, client, context, spec)
        await lease.install_grant(
            bridge,
            grant,
            shell=_shell("/app/worker/agents/helper/workspace"),
        )
        task = asyncio.create_task(lease.invoke_shell("run_shell_command", {"args": "sleep 100"}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert revoked_at_shutdown == [True]


@pytest.mark.asyncio
async def test_cli_shell_preserves_workspace_home_hook_path_and_output_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, workspace = _app(tmp_path, monkeypatch)
    (workspace / ".mindroom").mkdir()
    (workspace / ".mindroom/worker-env.sh").write_text("export CLI_TEST_HOOK=from-hook\nexport HOME=/forged-home\n")
    launch = _launch(workspace)
    launch["shell"] = _shell(str(workspace)).model_copy(update={"shell_path_prepend": "/custom/bin"}).model_dump()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"x-mindroom-sandbox-token": "worker-only"},
    ) as client:
        assert (await client.post("/api/sandbox-runner/agent-cli/install", json=launch)).status_code == 200
        response = await client.post(
            "/api/sandbox-runner/agent-cli/shell",
            json={
                "worker_key": KEY,
                "handle": "shell:" + "d" * 32,
                "operation": {
                    "function_name": "run_shell_command",
                    "args": 'printf \'%s\\n\' "$HOME" "$CLI_TEST_HOOK" "$PATH"',
                    "mindroom_output_path": "output.txt",
                    "tail": 1,
                },
            },
        )
        assert response.status_code == 200, response.text
        saved = (workspace / "output.txt").read_text()
        assert f"\n{workspace}\nfrom-hook\n/custom/bin:" in saved
        assert "/forged-home" not in saved


@pytest.mark.asyncio
async def test_worker_validates_transport_arguments_with_canonical_shell_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, workspace = _app(tmp_path, monkeypatch)

    class Shell(Toolkit):
        def __init__(self, **_kwargs: object) -> None:
            async def run_shell_command(args: str, canonical_option: int = 7) -> str:
                return f"{args}:{canonical_option}"

            super().__init__(name="shell", tools=[run_shell_command])

    monkeypatch.setattr(shell_invocation, "shell_tools", lambda: Shell)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"x-mindroom-sandbox-token": "worker-only"},
    ) as client:
        assert (await client.post("/api/sandbox-runner/agent-cli/install", json=_launch(workspace))).status_code == 200
        payload = {
            "worker_key": KEY,
            "handle": "shell:" + "c" * 32,
            "operation": {"function_name": "run_shell_command", "args": "value", "canonical_option": 42},
        }
        response = await client.post("/api/sandbox-runner/agent-cli/shell", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["result"] == "value:42"
        payload["handle"] = "shell:" + "d" * 32
        payload["operation"]["canonical_option"] = "invalid"
        assert (await client.post("/api/sandbox-runner/agent-cli/shell", json=payload)).status_code == 422
        payload["handle"] = "shell:" + "e" * 32
        payload["operation"] = {"function_name": "not_shell", "args": "value"}
        assert (await client.post("/api/sandbox-runner/agent-cli/shell", json=payload)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("private_scope", "private_agent_names", "workspace_below_scope"),
    [
        pytest.param(None, [], None, id="worker-scope-user"),
        pytest.param(USER, [], "code/code_data", id="private-per-user"),
        pytest.param(BASE, ["code"], "code/notes/work", id="custom-private-root"),
    ],
)
async def test_config_less_worker_runs_in_the_primary_resolved_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private_scope: str | None,
    private_agent_names: list[str],
    workspace_below_scope: str | None,
) -> None:
    """Real CLI workers have no agent config, so only the primary can resolve these workspaces."""
    app, workspace = _app(tmp_path, monkeypatch)
    if private_scope is not None and workspace_below_scope is not None:
        workspace = private_instance_scope_root_path(tmp_path / "storage", private_scope) / workspace_below_scope
    launch = _launch(workspace) | {"private_agent_names": private_agent_names}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"x-mindroom-sandbox-token": "worker-only"},
    ) as client:
        response = await client.post("/api/sandbox-runner/agent-cli/install", json=launch)
        assert response.status_code == 200, response.text
        response = await client.post(
            "/api/sandbox-runner/agent-cli/shell",
            json={
                "worker_key": KEY,
                "handle": "shell:" + "a" * 32,
                "operation": {"function_name": "run_shell_command", "args": ["pwd"], "tail": 1},
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"].strip().endswith(str(workspace))


@pytest.mark.asyncio
async def test_worker_rejects_workspace_outside_its_storage_mount_before_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, workspace = _app(tmp_path, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"x-mindroom-sandbox-token": "worker-only"},
    ) as client:
        route = "/api/sandbox-runner/agent-cli/install"
        for outside in (tmp_path / "other", tmp_path, workspace / "../../../.."):
            assert (await client.post(route, json=_launch(outside))).status_code == 400
        assert not (tmp_path / "private").exists()
        assert (await client.post(route, json=_launch(workspace))).status_code == 200


def _scoped_context(tmp_path: Path, agent: AgentConfig) -> ToolRuntimeContext:
    """Primary context for agent ``helper`` configured as given, beside a user-scoped ``other``."""
    context = _runtime_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"helper": agent, "other": AgentConfig(display_name="Other", worker_scope="user")},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        context.runtime_paths,
    )
    return replace(
        context,
        config=config,
        runtime_paths=replace(
            context.runtime_paths,
            process_env=MappingProxyType(
                {
                    "MINDROOM_AGENT_CLI_PRIMARY_URL": "http://primary",
                },
            ),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "allowed_below_scope"),
    [
        pytest.param(AgentConfig(display_name="Helper", worker_scope="user"), None, id="worker-scope-user"),
        pytest.param(
            AgentConfig(display_name="Helper", private=AgentPrivateConfig(per="user", root="notes")),
            "helper/notes",
            id="private-per-user",
        ),
    ],
)
async def test_user_scoped_agent_gets_a_docker_cli_worker_and_runs_in_its_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: AgentConfig,
    allowed_below_scope: str | None,
) -> None:
    """End to end: Docker acquisition under the requester's user scope, grant install, and a shell call."""
    context = _scoped_context(tmp_path, agent)
    storage = context.runtime_paths.storage_root
    container_root = tmp_path / "container/storage"
    spec = _cli_worker_spec(context)
    assert spec.state_scope_worker_key is not None
    assert resolved_worker_key_scope(spec.state_scope_worker_key) == "user"

    def canonical(root: Path) -> Path:
        if allowed_below_scope is None:
            return root / "agents/helper/workspace"
        return private_instance_scope_root_path(root, spec.state_scope_worker_key or "") / allowed_below_scope

    if allowed_below_scope is not None:
        # The primary creates a private workspace before a worker can mount it.
        canonical(storage).mkdir(parents=True)
    authored = {
        "agents": {name: config.model_dump(exclude_none=True) for name, config in context.config.agents.items()},
    }
    backend, docker, _ = _backend(monkeypatch, tmp_path, storage_path=storage, config_text=json.dumps(authored))
    backend.config = replace(backend.config, extra_env={}, storage_mount_path=str(container_root))
    handle = backend.ensure_worker(spec)
    volumes = docker.containers.run_calls[-1]["volumes"]
    assert any(volume.startswith(f"{canonical(storage)}:{canonical(container_root)}") for volume in volumes)
    assert not any(str(storage / "agents/helper/sessions") in volume for volume in volumes)

    app, _workspace = _app(
        tmp_path / "container",
        monkeypatch,
        worker_key=spec.worker_key,
        runner_token=handle.auth_token or "",
    )
    worker_transport = httpx.ASGITransport(app=app)

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "primary":
            return httpx.Response(200)
        return await worker_transport.handle_async_request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        bridge = TurnToolBridge(cli_turn_owner(context, _turn_context(), worker_id=handle.worker_id))
        grant = bridge.issue(now_ns=time.time_ns(), expires_at_ns=time.time_ns() + 10**12)
        lease = CliWorkerLease(handle, client, context, spec, container_storage_root=container_root)
        await lease.install_grant(bridge, grant, shell=_shell(str(canonical(storage))))
        result = await lease.invoke_shell("run_shell_command", {"args": ["pwd"], "tail": 1})
    assert isinstance(result, str)
    assert result.strip().endswith(str(canonical(container_root)))


@pytest.mark.parametrize(
    "foreign_scope",
    [
        "v1:default:user:~bob",
        "v1:other:user:~alice",
        "v1:default:user_agent:~alice:other",
        "v1:default:shared:code",
    ],
)
def test_cli_worker_key_is_owned_only_by_its_own_requester_scopes(foreign_scope: str) -> None:
    for owner in ("v1:default:user_agent:~alice:code", "v1:default:user:~alice"):
        assert resolve_state_scope_worker_key(KEY, owner) == owner
    # Only CLI turn processes run under a per-user scope; ordinary per-agent keys keep their own.
    with pytest.raises(WorkerBackendError, match="does not own"):
        resolve_state_scope_worker_key("v1:default:user_agent:~alice:code", "v1:default:user:~alice")
    with pytest.raises(WorkerBackendError, match="does not own"):
        resolve_state_scope_worker_key(KEY, foreign_scope)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "state_scope", "allowed_below_scope"),
    [
        pytest.param(AgentConfig(display_name="Helper", worker_scope="user"), "user", None, id="worker-scope-user"),
        pytest.param(
            AgentConfig(display_name="Helper", private=AgentPrivateConfig(per="user", root="notes")),
            "user",
            "helper/notes",
            id="private-per-user",
        ),
        pytest.param(
            AgentConfig(display_name="Helper", private=AgentPrivateConfig(per="user_agent", root="notes")),
            "user_agent",
            "helper/notes",
            id="custom-private-root",
        ),
    ],
)
async def test_primary_sends_only_the_agents_own_canonical_workspace(
    tmp_path: Path,
    agent: AgentConfig,
    state_scope: str,
    allowed_below_scope: str | None,
) -> None:
    """The primary resolves the workspace from live policies and maps it onto the worker's mounts."""
    context = _scoped_context(tmp_path, agent)
    spec = _cli_worker_spec(context)
    scope_key = spec.state_scope_worker_key
    assert scope_key is not None
    assert resolved_worker_key_scope(scope_key) == state_scope
    storage = context.runtime_paths.storage_root

    def canonical(root: Path) -> Path:
        if allowed_below_scope is None:
            return root / "agents/helper/workspace"
        return private_instance_scope_root_path(root, scope_key) / allowed_below_scope

    installs: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/install"):
            installs.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    worker = WorkerHandle("worker-id", spec.worker_key, "http://worker/api", "control", "ready", "docker", 0, 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        outside = [
            storage / "agents/helper",
            private_instance_scope_root_path(storage, "v1:default:user:@mallory:example.test") / "helper/notes",
        ]
        if allowed_below_scope is not None:
            outside += [storage / "agents/helper/workspace", canonical(storage).parent / "helper_data"]
        if state_scope == "user_agent":
            outside.append(storage / "agents/other/workspace")
        for workspace in outside:
            bridge = TurnToolBridge(cli_turn_owner(context, _turn_context(), worker_id=worker.worker_id))
            grant = bridge.issue(now_ns=time.time_ns(), expires_at_ns=time.time_ns() + 10**12)
            lease = CliWorkerLease(worker, client, context, spec, container_storage_root=Path("/app/worker"))
            with pytest.raises(ValueError, match="no unique canonical workspace mount"):
                await lease.install_grant(bridge, grant, shell=_shell(str(workspace)))
        assert installs == []
        bridge = TurnToolBridge(cli_turn_owner(context, _turn_context(), worker_id=worker.worker_id))
        grant = bridge.issue(now_ns=time.time_ns(), expires_at_ns=time.time_ns() + 10**12)
        lease = CliWorkerLease(worker, client, context, spec, container_storage_root=Path("/app/worker"))
        await lease.install_grant(bridge, grant, shell=_shell(str(canonical(storage) / "src")))
    assert installs[0]["shell"]["workspace"] == str(canonical(Path("/app/worker")) / "src")


@pytest.mark.asyncio
async def test_configured_manager_acquisition_releases_on_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation must release the manager returned by the drained acquisition thread."""
    started = threading.Event()
    finish = threading.Event()
    backend, docker, _ = _backend(monkeypatch, tmp_path)
    entry = worker_runtime._WorkerManagerEntry(backend, (), active_leases=1)
    lease = worker_runtime.PrimaryWorkerManagerLease(entry)

    def acquire(*_args: object, **_kwargs: object) -> worker_runtime.PrimaryWorkerManagerLease:
        started.set()
        assert finish.wait(5)
        return lease

    monkeypatch.setattr(cli_worker, "lease_configured_primary_worker_manager", acquire)

    async def use() -> None:
        async with cli_worker._open_configured_cli_worker(_runtime_context(tmp_path)):
            pytest.fail("cancelled manager acquisition yielded a worker")

    task = asyncio.create_task(use())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert entry.active_leases == 0
    assert docker.containers.run_calls == []


@pytest.mark.parametrize(
    ("env", "api_address", "expected"),
    [
        ({"MINDROOM_AGENT_CLI_PRIMARY_URL": "https://mindroom.example.org/"}, None, "https://mindroom.example.org"),
        ({}, ("0.0.0.0", 8765), "http://host.docker.internal:8765"),  # noqa: S104 - the default bind address
        ({}, ("::", 8765), "http://host.docker.internal:8765"),
        ({}, ("192.0.2.10", 9000), "http://192.0.2.10:9000"),
        ({}, ("127.0.0.1", 8765), None),
        ({}, ("localhost", 8765), None),
        ({}, None, None),
    ],
)
def test_primary_url_is_explicit_or_derived_from_the_running_api(
    tmp_path: Path,
    env: dict[str, str],
    api_address: tuple[str, int] | None,
    expected: str | None,
) -> None:
    """Workers call back through an explicit origin, or the API's own address as a container reaches it."""
    paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)
    if api_address is not None:
        set_api_server_address(*api_address)
    try:
        assert cli_primary_url(paths) == expected
    finally:
        clear_api_server_address()


@pytest.mark.asyncio
async def test_unreachable_primary_is_reported_with_its_address_and_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that cannot call MindRoom back names the address it tried, not a generic isolation failure."""
    app, workspace = _app(tmp_path, monkeypatch)

    async def unreachable(launch: CliWorkerLaunch, **_kwargs: object) -> None:
        message = "Connection refused"
        raise httpx.ConnectError(message, request=httpx.Request("GET", launch.primary_url))

    monkeypatch.setattr(sandbox_runner_cli, "probe_cli_network", unreachable)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://worker",
        headers={"x-mindroom-sandbox-token": "worker-only"},
    ) as worker_client:
        response = await worker_client.post("/api/sandbox-runner/agent-cli/install", json=_launch(workspace))
    assert response.status_code == 504
    assert not (tmp_path / "private").exists()

    context = _scoped_context(tmp_path, AgentConfig(display_name="Helper", worker_scope="user"))
    spec = _cli_worker_spec(context)
    worker = WorkerHandle("worker-id", spec.worker_key, "http://worker/api", "control", "ready", "docker", 0, 0)
    bridge = TurnToolBridge(cli_turn_owner(context, _turn_context(), worker_id=worker.worker_id))
    grant = bridge.issue(now_ns=time.time_ns(), expires_at_ns=time.time_ns() + 10**12)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(504))) as client:
        lease = CliWorkerLease(worker, client, context, spec, container_storage_root=Path("/app/worker"))
        workspace_root = context.runtime_paths.storage_root / "agents/helper/workspace"
        with pytest.raises(RuntimeError, match="could not reach MindRoom at http://primary") as raised:
            await lease.install_grant(bridge, grant, shell=_shell(str(workspace_root)))
    assert "MINDROOM_AGENT_CLI_PRIMARY_URL" in str(raised.value)
    with pytest.raises(CliAuthenticationError):
        bridge.authenticate(grant.raw_token, now_ns=time.time_ns())
