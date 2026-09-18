"""Process-level integration coverage for durable background script recovery."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import FastAPI

from mindroom.api.sandbox_runner_scripts import _script_namespace
from mindroom.api.script_gateway import bind_script_tool_broker
from mindroom.api.script_gateway import router as script_gateway_router
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.orchestration.script_runtime import ScriptRuntimeLifecycle
from mindroom.script_runs.broker import ScriptToolBroker
from mindroom.script_runs.manager import ScriptRunManager
from mindroom.script_runs.models import (
    ScriptCallState,
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
    script_worker_key_for_run,
    supervisor_handle_for_run,
)
from mindroom.script_runs.recovery import script_recovery_signature
from mindroom.script_runs.store import ScriptRunStore, mint_script_capability
from mindroom.script_runs.worker_client import WorkerScriptCancel, WorkerScriptStatus
from mindroom.shell_supervisor import (
    _ShellSupervisorManager,
    check_command_via_supervisor,
    kill_command_via_supervisor,
    parse_shell_supervisor_status,
    run_command_via_supervisor,
)
from mindroom.workers.models import WorkerHandle, WorkerSpec

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = f"script-{'a' * 32}"
_GATEWAY_URL = "http://primary.test/api/script-gateway"
_MINIMAL_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


@dataclass
class _Lease:
    manager: _RecoveringWorkerBackend
    released: bool = False

    def release(self) -> None:
        self.released = True


@dataclass
class _RecoveringWorkerBackend:
    """Model the external worker API while retaining its original pod template."""

    handle: WorkerHandle
    configured_image: str
    worker_template_image: str
    cleanup_locator: str | None = "kubernetes:test"
    backend_name: str = "kubernetes"
    idle_timeout_seconds: float = 3600
    ensure_calls: int = 0
    touched_worker_keys: list[str] = field(default_factory=list)

    def script_recovery_signature(self) -> str:
        return "stable-worker-authority"

    def legacy_pre_seccomp_script_recovery_signature(self) -> str:
        return "pre-seccomp-worker-authority"

    def script_resource_recovery_authority(self, resource_profile: str | None) -> dict[str, object]:
        return {"profile": resource_profile, "requests": {}, "limits": {}}

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: object | None = None,
    ) -> WorkerHandle:
        del spec, now, progress_sink
        self.ensure_calls += 1
        self.worker_template_image = self.configured_image
        return self.handle

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        del include_idle, now
        return [self.handle]

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        del now
        self.touched_worker_keys.append(worker_key)
        return self.handle if worker_key == self.handle.worker_key else None

    def retire_worker(self, worker_key: str) -> None:
        assert worker_key == self.handle.worker_key


@dataclass
class _AuthorizedResolver:
    settled_runs: list[str] = field(default_factory=list)
    settled_calls: list[str] = field(default_factory=list)

    def is_authorized(self, run: ScriptRunRecord, *, config: Config | None = None) -> bool:
        del run, config
        return True

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del reason
        self.settled_runs.append(run_id)

    async def settle_approval(self, origin: object, *, reason: str) -> None:
        del reason
        self.settled_calls.append(str(origin))

    async def prune_approvals(self, run_id: str) -> bool:
        del run_id
        return True


@dataclass
class _SupervisorWorkerClient:
    socket_path: str
    worker_key: str

    async def status(self, worker: WorkerHandle, *, run_id: str) -> WorkerScriptStatus:
        assert worker.worker_key == self.worker_key
        message = await asyncio.to_thread(
            check_command_via_supervisor,
            self.socket_path,
            namespace=_script_namespace(self.worker_key, run_id),
            handle=supervisor_handle_for_run(run_id),
        )
        parsed = parse_shell_supervisor_status(message)
        if parsed.state == "running":
            return WorkerScriptStatus(state="running", output=parsed.output)
        if parsed.state == "exited":
            return WorkerScriptStatus(state="exited", output=parsed.output, exit_code=parsed.exit_code)
        return WorkerScriptStatus(state="unknown", output=parsed.output)

    async def cancel(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        force: bool = False,
    ) -> WorkerScriptCancel:
        assert worker.worker_key == self.worker_key
        message = await asyncio.to_thread(
            kill_command_via_supervisor,
            self.socket_path,
            namespace=_script_namespace(self.worker_key, run_id),
            handle=supervisor_handle_for_run(run_id),
            force=force,
        )
        return WorkerScriptCancel(
            cancel_requested=message.startswith(("Terminated process", "Force-killed process")),
            already_finished=message.startswith("Process already finished"),
            unknown_handle=message.startswith("Error: Unknown handle"),
        )


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_SCRIPT_GATEWAY_ISOLATED": "true",
            "MINDROOM_SCRIPT_GATEWAY_URL": _GATEWAY_URL,
        },
    )


def _lifecycle(
    *,
    runtime_paths: RuntimePaths,
    store: ScriptRunStore,
    backend: _RecoveringWorkerBackend,
    worker_client: _SupervisorWorkerClient,
    config: Config,
) -> ScriptRuntimeLifecycle:
    resolver = _AuthorizedResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)  # type: ignore[arg-type]
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=worker_client,  # type: ignore[arg-type]
        worker_backend=backend,
        gateway_url=_GATEWAY_URL,
    )
    lifecycle = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,  # type: ignore[arg-type]
        config_provider=lambda: config,
        worker_lease_provider=lambda locator: _Lease(backend) if locator in {None, backend.cleanup_locator} else None,
        reconcile_interval_seconds=30,
    )
    lifecycle.bind_api(_GATEWAY_URL)
    return lifecycle


async def _read_progress(path: Path, *, greater_than: int = -1) -> int:
    for _ in range(100):
        try:
            value = int(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            value = -1
        if value > greater_than:
            return value
        await asyncio.sleep(0.05)
    message = f"Script progress never advanced beyond {greater_than}."
    raise AssertionError(message)


async def _wait_for_finished(socket_path: str, *, worker_key: str) -> str:
    for _ in range(120):
        message = await asyncio.to_thread(
            check_command_via_supervisor,
            socket_path,
            namespace=_script_namespace(worker_key, _RUN_ID),
            handle=supervisor_handle_for_run(_RUN_ID),
        )
        if "Status: FINISHED" in message:
            return message
        await asyncio.sleep(0.05)
    error = f"Script process did not reach its supervisor deadline: {message}"
    raise AssertionError(error)


async def _wait_for_pid_exit(process_id: int) -> None:
    for _ in range(100):
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.05)
    error = f"Script PID {process_id} survived its supervisor deadline."
    raise AssertionError(error)


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_seccomp", [False, True])
async def test_reconstructed_primary_adopts_same_process_and_durable_receipt(  # noqa: PLR0915
    tmp_path: Path,
    pre_seccomp: bool,
) -> None:
    """A compatible primary restart preserves one exact process until its original deadline."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config(agents={"watcher": {"display_name": "Watcher", "tools": ["script"]}}, defaults={"tools": []})
    base_worker_key = "v1:test:user_agent:alice:watcher"
    worker_key = script_worker_key_for_run(base_worker_key, _RUN_ID)
    worker = WorkerHandle(
        worker_id="worker-1",
        worker_key=worker_key,
        endpoint="http://worker.test/api/sandbox-runner/execute",
        auth_token="worker-token",  # noqa: S106
        status="ready",
        backend_name="kubernetes",
        last_used_at=1.0,
        created_at=1.0,
        debug_metadata={"image": "worker-image-v1"},
    )
    backend = _RecoveringWorkerBackend(
        handle=worker,
        configured_image="worker-image-v1",
        worker_template_image="worker-image-v1",
    )
    recovery_signature = script_recovery_signature(
        backend=backend,  # type: ignore[arg-type]
        config=config,
        agent_name="watcher",
        gateway_url=_GATEWAY_URL,
    )
    assert recovery_signature is not None
    token, token_hash = mint_script_capability()
    store = ScriptRunStore(runtime_paths)
    store.create_run(
        ScriptRunRecord(
            run_id=_RUN_ID,
            agent_name="watcher",
            owner_user_id="@alice:example.test",
            room_id="!room:example.test",
            source_digest="source-digest",
            grants=(ScriptToolGrant("calculator", "add"),),
            token_hash=token_hash,
            worker_key=worker_key,
            worker_backend_locator=backend.cleanup_locator,
            recovery_signature=recovery_signature,
            max_runtime_seconds=4,
        ),
    )
    store.transition_run(_RUN_ID, state=ScriptRunState.RUNNING, worker_id=worker.worker_id)

    supervisor = _ShellSupervisorManager()
    first_lifecycle: ScriptRuntimeLifecycle | None = None
    second_lifecycle: ScriptRuntimeLifecycle | None = None
    first_shutdown = False
    second_shutdown = False
    socket_path = supervisor.ensure()
    pid_path = tmp_path / "script.pid"
    progress_path = tmp_path / "script.progress"
    script = (
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
        "progress = pathlib.Path(sys.argv[2])\n"
        "counter = 0\n"
        "while True:\n"
        "    progress.write_text(str(counter), encoding='utf-8')\n"
        "    counter += 1\n"
        "    time.sleep(0.05)\n"
    )

    try:
        launch = await run_command_via_supervisor(
            socket_path,
            namespace=_script_namespace(worker_key, _RUN_ID),
            argv=[sys.executable, "-c", script, str(pid_path), str(progress_path)],
            env=_MINIMAL_ENV,
            cwd=None,
            tail=100,
            timeout=0,
            handle=supervisor_handle_for_run(_RUN_ID),
            max_runtime_seconds=4,
        )
        process_group_leader = int(re.search(r"PID (\d+)", launch).group(1))  # type: ignore[union-attr]
        first_progress = await _read_progress(progress_path)
        script_pid = int(pid_path.read_text(encoding="utf-8"))
        os.kill(script_pid, 0)
        worker_client = _SupervisorWorkerClient(socket_path=socket_path, worker_key=worker_key)

        first_lifecycle = _lifecycle(
            runtime_paths=runtime_paths,
            store=store,
            backend=backend,
            worker_client=worker_client,
            config=config,
        )
        await first_lifecycle.start()
        initially_adopted = store.get_run(_RUN_ID)
        assert initially_adopted.state is ScriptRunState.RUNNING
        assert initially_adopted.cancel_requested_at is None
        store.claim_call(
            run_id=_RUN_ID,
            call_id="accepted-before-restart",
            grant=ScriptToolGrant("calculator", "add"),
            arguments_digest="arguments-digest",
        )

        await first_lifecycle.shutdown()
        first_shutdown = True
        after_detach = store.get_run(_RUN_ID)
        assert after_detach.state is ScriptRunState.RUNNING
        assert after_detach.cancel_requested_at is None
        assert store.get_call(_RUN_ID, "accepted-before-restart").state is ScriptCallState.INDETERMINATE

        if pre_seccomp:
            historical_payload = {
                "protocol": 1,
                "backend": "pre-seccomp-worker-authority",
                "agent": "watcher",
                "process_authority": {
                    "execution_scope": None,
                    "private": None,
                    "knowledge_paths": [],
                    "grantable_credentials": [],
                },
                "gateway": _GATEWAY_URL,
                "resources": {"profile": None, "requests": {}, "limits": {}},
            }
            digest = hashlib.sha256(
                json.dumps(historical_payload, sort_keys=True, separators=(",", ":")).encode(),
            ).hexdigest()
            store.replace_recovery_signature(
                _RUN_ID,
                expected_signature=recovery_signature,
                recovery_signature=f"v2:{digest}",
            )

        backend.configured_image = "worker-image-v2"
        reopened_store = ScriptRunStore(runtime_paths)
        second_lifecycle = _lifecycle(
            runtime_paths=runtime_paths,
            store=reopened_store,
            backend=backend,
            worker_client=worker_client,
            config=config,
        )
        await second_lifecycle.start()

        recovered = reopened_store.require_active_capability(_RUN_ID, token)
        assert recovered.state is ScriptRunState.RUNNING
        assert recovered.worker_id == worker.worker_id
        assert recovered.recovery_signature == recovery_signature
        assert backend.ensure_calls == 0
        assert backend.worker_template_image == "worker-image-v1"
        assert backend.handle.debug_metadata["image"] == "worker-image-v1"
        status = await worker_client.status(worker, run_id=_RUN_ID)
        assert status.state == "running"
        assert f"PID {process_group_leader}" in status.output
        assert int(pid_path.read_text(encoding="utf-8")) == script_pid
        os.kill(script_pid, 0)
        assert await _read_progress(progress_path, greater_than=first_progress) > first_progress

        gateway = FastAPI()
        gateway.include_router(script_gateway_router)
        bind_script_tool_broker(gateway, second_lifecycle.broker)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                f"/api/script-gateway/runs/{_RUN_ID}/calls/accepted-before-restart",
                headers={"authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        assert response.json()["state"] == ScriptCallState.INDETERMINATE

        finished = await _wait_for_finished(socket_path, worker_key=worker_key)
        assert "exit code -9" in finished
        await _wait_for_pid_exit(script_pid)
        terminal = await second_lifecycle.manager.reconcile_durable(run_id=_RUN_ID)
        assert terminal.state is ScriptRunState.INTERRUPTED
        await second_lifecycle.shutdown()
        second_shutdown = True
    finally:
        if second_lifecycle is not None and not second_shutdown:
            with contextlib.suppress(Exception):
                await second_lifecycle.shutdown(timeout_seconds=1)
        if first_lifecycle is not None and not first_shutdown:
            with contextlib.suppress(Exception):
                await first_lifecycle.shutdown(timeout_seconds=1)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                kill_command_via_supervisor,
                socket_path,
                namespace=_script_namespace(worker_key, _RUN_ID),
                handle=supervisor_handle_for_run(_RUN_ID),
                force=True,
            )
        supervisor.shutdown()
