"""Tests for primary-owned background script lifecycle management."""

from __future__ import annotations

import asyncio
import stat
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mindroom.api import sandbox_runner as sandbox_runner_module
from mindroom.api.sandbox_runner_scripts import router as sandbox_runner_scripts_router
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.message_target import MessageTarget
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.manager import (
    ScriptRunLimits,
    ScriptRunManager,
    ScriptRunManagerError,
)
from mindroom.script_runs.models import ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.store import ScriptRunStore
from mindroom.script_runs.worker_client import (
    WorkerScriptCancel,
    WorkerScriptStatus,
)
from mindroom.tool_system.worker_routing import agent_workspace_root_path, worker_root_path
from mindroom.workers.models import WorkerHandle, WorkerSpec
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup

if TYPE_CHECKING:
    import os

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _runtime_paths(
    tmp_path: Path,
    *,
    mode: str | None = "all",
    backend: str | None = None,
) -> RuntimePaths:
    process_env = {"MINDROOM_SANDBOX_EXECUTION_MODE": mode} if mode is not None else {}
    if backend is not None:
        process_env["MINDROOM_WORKER_BACKEND"] = backend
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env=process_env,
    )


def _context(
    tmp_path: Path,
    *,
    agent_name: str = "watcher",
    requester_id: str = "@alice:example.test",
    mode: str | None = "all",
    private: bool = False,
    worker_scope: str = "user_agent",
    backend: str | None = None,
) -> ToolRuntimeContext:
    runtime_paths = _runtime_paths(tmp_path, mode=mode, backend=backend)
    watcher: dict[str, object] = {
        "display_name": "Watcher",
        "worker_scope": worker_scope,
        "tools": ["script", "calculator"],
    }
    if private:
        watcher.pop("worker_scope")
        watcher["private"] = {"per": "user_agent", "root": "private/watcher"}
    config = Config(
        agents={
            "watcher": watcher,
            "analyzer": {
                "display_name": "Analyzer",
                "worker_scope": worker_scope,
                "tools": ["script", "calculator"],
            },
        },
        defaults={"tools": []},
    )
    return make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget.resolve(
            room_id="!room:example.test",
            thread_id="$thread:example.test",
            reply_to_event_id=None,
        ),
        requester_id=requester_id,
        client=SimpleNamespace(),
        config=config,
        runtime_paths=runtime_paths,
        storage_path=runtime_paths.storage_root,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


@dataclass
class _Broker:
    store: ScriptRunStore
    cancelled_runs: list[str] = field(default_factory=list)
    cancelled_states: list[ScriptRunState] = field(default_factory=list)
    failures: list[BaseException] = field(default_factory=list)

    async def cancel_run(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        assert run.cancel_requested_at is not None or run.state in {
            ScriptRunState.EXITED,
            ScriptRunState.FAILED,
            ScriptRunState.CANCELLED,
            ScriptRunState.INTERRUPTED,
        }
        self.cancelled_runs.append(run_id)
        self.cancelled_states.append(run.state)
        if self.failures:
            raise self.failures.pop(0)


@dataclass
class _WorkerBackend:
    store: ScriptRunStore
    runtime_paths: RuntimePaths
    handles: dict[str, WorkerHandle] = field(default_factory=dict)
    specs: list[WorkerSpec] = field(default_factory=list)
    saw_starting: bool = False
    list_worker_thread_ids: list[int] = field(default_factory=list)

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: object | None = None,
    ) -> WorkerHandle:
        del now, progress_sink
        active = self.store.list_runs(include_finished=False)
        self.saw_starting = len(active) == 1 and active[0].state is ScriptRunState.STARTING
        self.specs.append(spec)
        root = worker_root_path(self.runtime_paths.storage_root, spec.worker_key)
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        handle = WorkerHandle(
            worker_id=f"worker-{len(self.handles) + 1}",
            worker_key=spec.worker_key,
            endpoint="http://worker.test/api/sandbox-runner/execute",
            auth_token="worker-token",  # noqa: S106
            status="ready",
            backend_name="test",
            last_used_at=1.0,
            created_at=1.0,
            debug_metadata={"state_root": str(root), "api_root": "http://worker.test/api/sandbox-runner"},
        )
        self.handles[spec.worker_key] = handle
        return handle

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        del include_idle, now
        self.list_worker_thread_ids.append(threading.get_ident())
        return list(self.handles.values())

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        del now
        return self.handles.get(worker_key)

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        del now
        return []

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        del failure_reason, now
        return self.handles[worker_key]

    def shutdown(self) -> None:
        return None


@dataclass
class _WorkerClient:
    store: ScriptRunStore
    launch_paths: dict[str, tuple[Path, Path]] = field(default_factory=dict)
    cancel_observed_revocation: bool = False
    cancel_forces: list[bool] = field(default_factory=list)
    cancel_handles: list[str] = field(default_factory=list)
    requested_handles: list[str] = field(default_factory=list)
    next_status: WorkerScriptStatus = field(
        default_factory=lambda: WorkerScriptStatus(state="exited", exit_code=-15),
    )
    status_results: list[WorkerScriptStatus] = field(default_factory=list)
    cancel_failures: list[BaseException] = field(default_factory=list)
    launch_failure: BaseException | None = None
    launch_entered: asyncio.Event | None = None
    second_launch_entered: asyncio.Event | None = None
    launch_release: asyncio.Event | None = None

    async def launch(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        source_digest: str,
        gateway_url: str,
        private_agent_names: tuple[str, ...] | None = None,
    ) -> None:
        del source_digest, gateway_url, private_agent_names
        starting = self.store.get_run(run_id)
        assert starting.state is ScriptRunState.STARTING
        assert starting.worker_id == worker.worker_id
        supervisor_handle = f"shell:{run_id.removeprefix('script-')}"
        assert len(supervisor_handle) == len("shell:") + 32
        self.requested_handles.append(supervisor_handle)
        if len(self.requested_handles) == 2 and self.second_launch_entered is not None:
            self.second_launch_entered.set()
        workspace = Path(worker.debug_metadata["state_root"]) / "workspace"
        source = workspace / ".mindroom" / "script-runs" / run_id / "source.py"
        token = workspace / ".mindroom" / "script-runs" / run_id / "capability"
        assert source.read_text(encoding="utf-8") == "print('ok')\n"
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert stat.S_IMODE(token.stat().st_mode) == 0o600
        assert stat.S_IMODE(source.parent.stat().st_mode) == 0o700
        self.launch_paths[run_id] = (source, token)
        if self.launch_entered is not None:
            self.launch_entered.set()
        if self.launch_release is not None:
            await self.launch_release.wait()
        if self.launch_failure is not None:
            raise self.launch_failure

    async def status(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
    ) -> WorkerScriptStatus:
        del worker, run_id
        if self.status_results:
            return self.status_results.pop(0)
        return self.next_status

    async def cancel(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        force: bool = False,
    ) -> WorkerScriptCancel:
        del worker
        self.cancel_forces.append(force)
        self.cancel_handles.append(f"shell:{run_id.removeprefix('script-')}")
        self.cancel_observed_revocation = self.store.get_run(run_id).cancel_requested_at is not None
        if self.cancel_failures:
            raise self.cancel_failures.pop(0)
        return WorkerScriptCancel(cancel_requested=True, already_finished=False, unknown_handle=False)


def _manager(
    tmp_path: Path,
    *,
    mode: str | None = "all",
    backend: str | None = None,
) -> tuple[ScriptRunManager, _WorkerBackend, _WorkerClient]:
    context = _context(tmp_path, mode=mode, backend=backend)
    store = ScriptRunStore(context.runtime_paths)
    backend = _WorkerBackend(store=store, runtime_paths=context.runtime_paths)
    client = _WorkerClient(store=store)
    manager = ScriptRunManager(
        store=store,
        broker=_Broker(store),
        worker_client=client,
        worker_backend=backend,
        gateway_url="http://primary.test/api/script-gateway",
        grant_resolver=lambda _context: (ScriptToolGrant("calculator", "add"),),
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    return manager, backend, client


@pytest.mark.asyncio
async def test_launch_uses_derived_supervisor_handle_from_the_run_id(tmp_path: Path) -> None:
    """Worker allocation sees durable intent, then the launch sees private snapshotted files."""
    manager, backend, client = _manager(tmp_path)
    context = _context(tmp_path)

    run = await manager.run(
        context,
        source="print('ok')\n",
        limits=ScriptRunLimits(max_concurrent_runs=2, max_tool_calls_per_minute=4, max_runtime_hours=1),
    )

    assert backend.saw_starting is True
    assert run.state is ScriptRunState.RUNNING
    assert run.worker_key is not None
    assert run.worker_id == "worker-1"
    assert client.requested_handles == [f"shell:{run.run_id.removeprefix('script-')}"]
    assert run.max_tool_calls_per_minute == 4
    assert run.max_runtime_seconds == 3600
    assert run.snapshot_locator is not None
    assert (context.runtime_paths.storage_root / run.snapshot_locator / "source.py").is_file()
    assert client.launch_paths[run.run_id][0].is_file()


@pytest.mark.asyncio
async def test_worker_replacement_waits_for_admitted_launch_and_rejects_racing_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement drains an admitted launch before rejecting scripts that race its boundary."""
    manager, backend, _client = _manager(tmp_path)
    worker_allocation_started = threading.Event()
    release_worker_allocation = threading.Event()
    original_ensure_worker = backend.ensure_worker

    def block_worker_allocation(*args: object, **kwargs: object) -> WorkerHandle:
        worker_allocation_started.set()
        assert release_worker_allocation.wait(timeout=5)
        return original_ensure_worker(*args, **kwargs)

    monkeypatch.setattr(backend, "ensure_worker", block_worker_allocation)
    admitted_launch = asyncio.create_task(manager.run(_context(tmp_path), source="print('ok')\n"))
    assert await asyncio.to_thread(worker_allocation_started.wait, 1)
    boundary = asyncio.create_task(manager.begin_worker_replacement())
    await asyncio.sleep(0)

    with pytest.raises(ScriptRunManagerError, match="worker replacement is in progress"):
        await manager.run(_context(tmp_path), source="print('blocked')\n")

    assert len(manager.store.list_runs()) == 1
    assert boundary.done() is False
    release_worker_allocation.set()
    await admitted_launch
    await boundary
    await manager.end_worker_replacement()


@pytest.mark.asyncio
async def test_worker_launch_without_a_backend_is_rejected_before_creating_durable_intent(tmp_path: Path) -> None:
    """A safe unavailable backend must not create a script row that can never launch."""
    manager, _backend, _client = _manager(tmp_path)
    manager.worker_backend = None

    with pytest.raises(ScriptRunManagerError, match="worker backend is unavailable"):
        await manager.run(_context(tmp_path), source="print('unavailable')\n")

    assert manager.store.list_runs() == []


@pytest.mark.asyncio
async def test_worker_launch_requires_primary_visible_state_before_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote handle without shared-state proof cannot receive primary-only paths."""
    manager, backend, _client = _manager(tmp_path)
    snapshot_writes: list[str] = []

    def ensure_worker_without_visible_state(
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: object | None = None,
    ) -> WorkerHandle:
        del now, progress_sink
        return WorkerHandle(
            worker_id="static-worker",
            worker_key=spec.worker_key,
            endpoint="http://worker.test/api/sandbox-runner/execute",
            auth_token="worker-token",  # noqa: S106
            status="ready",
            backend_name="static_sandbox_runner",
            last_used_at=1.0,
            created_at=1.0,
            debug_metadata={"api_root": "http://worker.test/api/sandbox-runner"},
        )

    def record_snapshot_write(
        _workspace: Path,
        run_id: str,
        *,
        source: bytes,
        token: str,
    ) -> tuple[Path, Path]:
        del source, token
        snapshot_writes.append(run_id)
        message = "snapshot creation must not be reached"
        raise AssertionError(message)

    monkeypatch.setattr(backend, "ensure_worker", ensure_worker_without_visible_state)
    monkeypatch.setattr(manager_module, "_write_snapshot", record_snapshot_write)

    with pytest.raises(ScriptRunManagerError, match="visible state root or subpath"):
        await manager.run(_context(tmp_path), source="print('ok')\n")

    assert snapshot_writes == []
    [failed] = manager.store.list_runs()
    assert failed.state is ScriptRunState.FAILED
    assert failed.snapshot_locator is None


@pytest.mark.asyncio
async def test_launch_grants_are_restricted_by_configured_allowed_tools(tmp_path: Path) -> None:
    """The authored allowlist can only narrow the agent's resolved launch surface."""
    manager, _backend, _client = _manager(tmp_path)
    manager.grant_resolver = lambda _context: (
        ScriptToolGrant("calculator", "add"),
        ScriptToolGrant("website", "read_url"),
    )

    run = await manager.run(
        _context(tmp_path),
        source="print('ok')\n",
        limits=ScriptRunLimits(allowed_tools=("calculator",)),
    )

    assert run.grants == (ScriptToolGrant("calculator", "add"),)
    assert run.preapprove_launch_grants is True


@pytest.mark.asyncio
async def test_worker_keys_are_requester_and_agent_scoped(tmp_path: Path) -> None:
    """Different owners cannot share a user-agent worker or its run directory."""
    manager, _backend, _client = _manager(tmp_path)

    alice = await manager.run(_context(tmp_path), source="print('ok')\n")
    bob = await manager.run(_context(tmp_path, requester_id="@bob:example.test"), source="print('ok')\n")

    assert alice.worker_key != bob.worker_key
    assert alice.worker_id != bob.worker_id


@pytest.mark.asyncio
async def test_concurrent_scripts_use_run_pinned_worker_roots_and_routes(tmp_path: Path) -> None:
    """A narrow run's worker cannot select or locate a sibling run's snapshot."""
    manager, backend, _client = _manager(tmp_path)
    manager.grant_resolver = lambda _context: (
        ScriptToolGrant("calculator", "add"),
        ScriptToolGrant("website", "read_url"),
    )
    context = _context(tmp_path)

    broad = await manager.run(
        context,
        source="print('ok')\n",
        limits=ScriptRunLimits(allowed_tools=("calculator", "website")),
    )
    narrow = await manager.run(
        context,
        source="print('ok')\n",
        limits=ScriptRunLimits(allowed_tools=("calculator",)),
    )

    assert broad.worker_key is not None
    assert narrow.worker_key is not None
    assert broad.worker_key != narrow.worker_key
    assert broad.worker_key.endswith(":watcher")
    assert narrow.worker_key.endswith(":watcher")
    assert broad.worker_id != narrow.worker_id
    broad_root = Path(backend.handles[broad.worker_key].debug_metadata["state_root"])
    narrow_root = Path(backend.handles[narrow.worker_key].debug_metadata["state_root"])
    assert broad_root != narrow_root
    assert not (narrow_root / "workspace" / ".mindroom" / "script-runs" / broad.run_id).exists()
    assert {path.parent.name for path in narrow_root.rglob("capability")} == {narrow.run_id}

    (narrow_root / "venv" / "bin").mkdir(parents=True)
    (narrow_root / "venv" / "bin" / "python").symlink_to(Path(sys.executable))
    dedicated_paths = RuntimePaths(
        config_path=context.runtime_paths.config_path,
        config_dir=context.runtime_paths.config_dir,
        env_path=context.runtime_paths.env_path,
        storage_root=narrow_root,
        process_env={
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: narrow.worker_key,
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: str(narrow_root),
        },
    )
    app = FastAPI()
    app.include_router(sandbox_runner_scripts_router)
    sandbox_runner_module.initialize_sandbox_runner_app(
        app,
        dedicated_paths,
        config=context.config,
        runner_token="worker-token",  # noqa: S106
    )
    route_client = TestClient(app)
    headers = {"x-mindroom-sandbox-token": "worker-token"}

    sibling_key = route_client.post(
        "/api/sandbox-runner/scripts/run",
        headers=headers,
        json={
            "run_id": broad.run_id,
            "worker_key": broad.worker_key,
            "source_digest": broad.source_digest,
            "gateway_url": "http://primary.test/api/script-gateway",
            "private_agent_names": [],
        },
    )
    sibling_snapshot = route_client.post(
        "/api/sandbox-runner/scripts/run",
        headers=headers,
        json={
            "run_id": broad.run_id,
            "worker_key": narrow.worker_key,
            "source_digest": broad.source_digest,
            "gateway_url": "http://primary.test/api/script-gateway",
            "private_agent_names": [],
        },
    )

    assert sibling_key.status_code == 400
    assert "dedicated worker" in sibling_key.json()["detail"].lower()
    assert sibling_snapshot.status_code == 400
    assert "unavailable" in sibling_snapshot.json()["detail"].lower()


@pytest.mark.parametrize("configured_scope", ["shared", "user"])
@pytest.mark.asyncio
async def test_script_process_scope_is_user_agent_independent_of_tool_scope(
    tmp_path: Path,
    configured_scope: str,
) -> None:
    """Script processes never reuse a worker across requesters or agents."""
    manager, _backend, _client = _manager(tmp_path)

    alice_watcher = await manager.run(
        _context(tmp_path, worker_scope=configured_scope),
        source="print('ok')\n",
    )
    bob_watcher = await manager.run(
        _context(tmp_path, requester_id="@bob:example.test", worker_scope=configured_scope),
        source="print('ok')\n",
    )
    alice_analyzer = await manager.run(
        _context(tmp_path, agent_name="analyzer", worker_scope=configured_scope),
        source="print('ok')\n",
    )

    assert alice_watcher.worker_key is not None
    assert ":user_agent:" in alice_watcher.worker_key
    assert len({alice_watcher.worker_key, bob_watcher.worker_key, alice_analyzer.worker_key}) == 3


@pytest.mark.asyncio
async def test_script_process_target_preserves_private_agent_visibility(tmp_path: Path) -> None:
    """Dedicated script workers retain the private-agent visibility required by their workspace."""
    manager, backend, _client = _manager(tmp_path)

    await manager.run(_context(tmp_path, private=True), source="print('ok')\n")

    assert backend.specs[-1].private_agent_names == frozenset({"watcher"})


@pytest.mark.asyncio
async def test_starting_run_with_durable_handle_can_be_reconciled_and_signalled(tmp_path: Path) -> None:
    """Restart reconciliation can observe and control a pre-spawn persisted handle."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    launched = await manager.run(context, source="print('ok')\n")
    orphan = replace(
        launched,
        run_id=f"script-{'b' * 32}",
        state=ScriptRunState.STARTING,
        started_at=None,
    )
    manager.store.create_run(orphan)
    client.status_results = [WorkerScriptStatus(state="running")]

    observed = await manager.reconcile(context, run_id=orphan.run_id)

    assert observed.state is ScriptRunState.STARTING
    client.next_status = WorkerScriptStatus(state="exited", exit_code=-9)
    cancelled = await manager.cancel(context, run_id=orphan.run_id, force=True)
    assert cancelled.state is ScriptRunState.CANCELLED
    assert client.cancel_handles[-1] == f"shell:{orphan.run_id.removeprefix('script-')}"


@pytest.mark.asyncio
async def test_ambiguous_worker_launch_failure_remains_retryable_until_exit(tmp_path: Path) -> None:
    """An unconfirmed launch owner stays nonterminal so reconciliation can signal it again."""
    manager, _backend, client = _manager(tmp_path)
    client.launch_failure = RuntimeError("launch response lost")
    client.cancel_failures.append(RuntimeError("worker unavailable"))
    client.next_status = WorkerScriptStatus(state="running")

    with pytest.raises(RuntimeError, match="launch response lost"):
        await manager.run(_context(tmp_path), source="print('ok')\n")

    stored = manager.store.list_runs()[0]
    assert stored.state is ScriptRunState.STARTING
    assert stored.cancel_requested_at is not None
    assert client.cancel_forces == [True]
    assert client.cancel_handles == [f"shell:{stored.run_id.removeprefix('script-')}"]

    client.next_status = WorkerScriptStatus(state="exited", exit_code=-9)
    reconciled = await manager.reconcile(_context(tmp_path), run_id=stored.run_id)

    assert reconciled.state is ScriptRunState.CANCELLED
    assert client.cancel_forces == [True, False]


@pytest.mark.asyncio
async def test_ambiguous_worker_running_persistence_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-spawn durable update failure keeps ownership retryable until exit."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    original_transition = manager.store.transition_run

    def fail_running_transition(
        run_id: str,
        *,
        state: ScriptRunState,
        worker_id: str | None = None,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> ScriptRunRecord:
        if state is ScriptRunState.RUNNING:
            msg = "durable update failed"
            raise RuntimeError(msg)
        return original_transition(
            run_id,
            state=state,
            worker_id=worker_id,
            exit_code=exit_code,
            error=error,
        )

    monkeypatch.setattr(manager.store, "transition_run", fail_running_transition)
    client.cancel_failures.append(RuntimeError("worker unavailable"))
    client.next_status = WorkerScriptStatus(state="running")

    with pytest.raises(RuntimeError, match="durable update failed"):
        await manager.run(context, source="print('ok')\n")

    stored = manager.store.list_runs()[0]
    assert stored.state is ScriptRunState.STARTING
    assert stored.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_launch_adopts_cancellation_that_finishes_before_running_transition(tmp_path: Path) -> None:
    """Launch completion cannot overwrite or error on a concurrently confirmed cancellation."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    client.launch_entered = asyncio.Event()
    client.launch_release = asyncio.Event()
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    await client.launch_entered.wait()
    [starting] = manager.store.list_runs(include_finished=False)

    cancelled = await manager.cancel(context, run_id=starting.run_id, force=True)
    client.launch_release.set()
    launch_result = await launch

    assert cancelled.state is ScriptRunState.CANCELLED
    assert launch_result.state is ScriptRunState.CANCELLED
    assert manager.store.get_run(starting.run_id).state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_worker_launch_does_not_publish_running_after_unconfirmed_cancel(tmp_path: Path) -> None:
    """A spawned process with cancellation intent remains retryable instead of becoming running."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    client.launch_entered = asyncio.Event()
    client.launch_release = asyncio.Event()
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    await client.launch_entered.wait()
    [starting] = manager.store.list_runs(include_finished=False)
    client.next_status = WorkerScriptStatus(state="running")

    with pytest.raises(ScriptRunManagerError, match="not yet confirmed"):
        await manager.cancel(context, run_id=starting.run_id, force=True)
    client.launch_release.set()
    launch_result = await launch

    assert launch_result.state is ScriptRunState.STARTING
    assert launch_result.cancel_requested_at is not None
    assert manager.store.get_run(starting.run_id).state is ScriptRunState.STARTING

    client.next_status = WorkerScriptStatus(state="exited", exit_code=-9)
    reconciled = await manager.reconcile(context, run_id=starting.run_id)
    assert reconciled.state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_worker_launch_stops_when_cancelled_before_worker_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after durable creation prevents a later worker process spawn."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    created = threading.Event()
    release_create = threading.Event()
    original_create = manager.store.create_run

    def create_then_pause(run: ScriptRunRecord) -> None:
        original_create(run)
        created.set()
        assert release_create.wait(timeout=5)

    monkeypatch.setattr(manager.store, "create_run", create_then_pause)
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    assert await asyncio.to_thread(created.wait, 5)
    [starting] = manager.store.list_runs(include_finished=False)

    with pytest.raises(ScriptRunManagerError, match="not yet confirmed"):
        await manager.cancel(context, run_id=starting.run_id, force=True)
    release_create.set()
    launch_result = await launch

    assert launch_result.state is ScriptRunState.CANCELLED
    assert client.requested_handles == []


@pytest.mark.asyncio
async def test_task_cancellation_during_durable_reservation_finishes_as_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot abandon a reservation whose durable write is still running."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    create_entered = threading.Event()
    release_create = threading.Event()
    create_finished = threading.Event()
    original_create = manager.store.create_run

    def blocked_create(run: ScriptRunRecord) -> None:
        create_entered.set()
        assert release_create.wait(timeout=5)
        original_create(run)
        create_finished.set()

    monkeypatch.setattr(manager.store, "create_run", blocked_create)
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    assert await asyncio.to_thread(create_entered.wait, 5)

    launch.cancel()
    await asyncio.sleep(0)
    cancellation_retained_ownership = not launch.done()
    release_create.set()
    assert await asyncio.to_thread(create_finished.wait, 5)

    with pytest.raises(asyncio.CancelledError):
        await launch

    [stored] = manager.store.list_runs()
    assert cancellation_retained_ownership is True
    assert stored.state is ScriptRunState.INTERRUPTED
    assert manager.broker.cancelled_runs == [stored.run_id]
    assert client.requested_handles == []


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_abandon_durable_reservation_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second cancellation must not detach cleanup after the reservation commits."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    create_entered = threading.Event()
    release_create = threading.Event()
    create_finished = threading.Event()
    finalization_entered = threading.Event()
    release_finalization = threading.Event()
    original_create = manager.store.create_run
    original_get = manager.store.get_run

    def blocked_create(run: ScriptRunRecord) -> None:
        create_entered.set()
        assert release_create.wait(timeout=5)
        original_create(run)
        create_finished.set()

    def blocked_finalization_get(run_id: str) -> ScriptRunRecord:
        finalization_entered.set()
        assert release_finalization.wait(timeout=5)
        return original_get(run_id)

    monkeypatch.setattr(manager.store, "create_run", blocked_create)
    monkeypatch.setattr(manager.store, "get_run", blocked_finalization_get)
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    assert await asyncio.to_thread(create_entered.wait, 5)

    launch.cancel()
    release_create.set()
    assert await asyncio.to_thread(create_finished.wait, 5)
    assert await asyncio.to_thread(finalization_entered.wait, 5)
    launch.cancel()
    await asyncio.sleep(0)
    repeated_cancellation_retained_ownership = not launch.done()
    release_finalization.set()

    with pytest.raises(asyncio.CancelledError):
        await launch

    [stored] = manager.store.list_runs()
    assert repeated_cancellation_retained_ownership is True
    assert stored.state is ScriptRunState.INTERRUPTED
    assert manager.broker.cancelled_runs == [stored.run_id]
    assert client.requested_handles == []


@pytest.mark.asyncio
async def test_preallocation_cancel_releases_capacity_when_broker_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known-no-child cancellation is terminal even when broker coordination fails."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    limits = ScriptRunLimits(max_concurrent_runs=1)
    created = threading.Event()
    release_create = threading.Event()
    original_create = manager.store.create_run

    def create_then_pause(run: ScriptRunRecord) -> None:
        original_create(run)
        created.set()
        assert release_create.wait(timeout=5)

    monkeypatch.setattr(manager.store, "create_run", create_then_pause)
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n", limits=limits))
    assert await asyncio.to_thread(created.wait, 5)
    [starting] = manager.store.list_runs(include_finished=False)

    with pytest.raises(ScriptRunManagerError, match="not yet confirmed"):
        await manager.cancel(context, run_id=starting.run_id, force=True)
    manager.broker.failures.append(RuntimeError("broker unavailable"))
    release_create.set()

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await launch

    assert manager.store.get_run(starting.run_id).state is ScriptRunState.CANCELLED
    assert client.requested_handles == []
    replacement = await manager.run(context, source="print('ok')\n", limits=limits)
    assert replacement.state is ScriptRunState.RUNNING


@pytest.mark.asyncio
async def test_worker_launch_rechecks_cancellation_after_worker_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation committed during worker assignment prevents process spawn."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    worker_assigned = threading.Event()
    release_assignment = threading.Event()
    original_transition = manager.store.transition_run

    def transition_then_pause(
        run_id: str,
        *,
        state: ScriptRunState,
        worker_id: str | None = None,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> ScriptRunRecord:
        result = original_transition(
            run_id,
            state=state,
            worker_id=worker_id,
            exit_code=exit_code,
            error=error,
        )
        if state is ScriptRunState.STARTING and worker_id is not None:
            worker_assigned.set()
            assert release_assignment.wait(timeout=5)
        return result

    monkeypatch.setattr(manager.store, "transition_run", transition_then_pause)
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    assert await asyncio.to_thread(worker_assigned.wait, 5)
    [starting] = manager.store.list_runs(include_finished=False)
    client.next_status = WorkerScriptStatus.unknown_handle()

    with pytest.raises(ScriptRunManagerError, match="not yet confirmed"):
        await manager.cancel(context, run_id=starting.run_id, force=True)
    release_assignment.set()
    launch_result = await launch

    assert launch_result.state is ScriptRunState.CANCELLED
    assert client.requested_handles == []


@pytest.mark.asyncio
async def test_assigned_prespawn_cancel_retries_broker_after_terminalizing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal control retries broker cancellation after a known-no-child broker failure."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    worker_assigned = threading.Event()
    release_assignment = threading.Event()
    original_transition = manager.store.transition_run

    def transition_then_pause(
        run_id: str,
        *,
        state: ScriptRunState,
        worker_id: str | None = None,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> ScriptRunRecord:
        result = original_transition(
            run_id,
            state=state,
            worker_id=worker_id,
            exit_code=exit_code,
            error=error,
        )
        if state is ScriptRunState.STARTING and worker_id is not None:
            worker_assigned.set()
            assert release_assignment.wait(timeout=5)
        return result

    monkeypatch.setattr(manager.store, "transition_run", transition_then_pause)
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    assert await asyncio.to_thread(worker_assigned.wait, 5)
    [starting] = manager.store.list_runs(include_finished=False)
    client.next_status = WorkerScriptStatus.unknown_handle()

    with pytest.raises(ScriptRunManagerError, match="not yet confirmed"):
        await manager.cancel(context, run_id=starting.run_id, force=True)
    manager.broker.failures.append(RuntimeError("broker unavailable"))
    release_assignment.set()

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await launch

    assert manager.store.get_run(starting.run_id).state is ScriptRunState.CANCELLED
    assert client.requested_handles == []
    status = await manager.status(context, run_id=starting.run_id)
    assert status.run.state is ScriptRunState.CANCELLED
    assert manager.broker.cancelled_states[-1] is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_worker_launch_rechecks_durable_intent_immediately_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable cancellation committed during snapshot preparation prevents worker spawn."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    original_write_snapshot = manager_module._write_snapshot

    def snapshot_then_cancel(
        workspace: Path,
        run_id: str,
        *,
        source: bytes,
        token: str,
    ) -> tuple[Path, Path]:
        paths = original_write_snapshot(workspace, run_id, source=source, token=token)
        manager.store.request_cancel(run_id, reason="cancelled during snapshot")
        return paths

    monkeypatch.setattr(manager_module, "_write_snapshot", snapshot_then_cancel)

    launch_result = await manager.run(context, source="print('ok')\n")

    assert launch_result.state is ScriptRunState.CANCELLED
    assert client.requested_handles == []


@pytest.mark.asyncio
async def test_configured_worker_backend_is_used_without_execution_mode_override(tmp_path: Path) -> None:
    """An enabled primary worker backend remains the default when no mode override is authored."""
    manager, _backend, _client = _manager(tmp_path, mode=None)

    run = await manager.run(_context(tmp_path, mode=None), source="print('ok')\n")

    assert run.state is ScriptRunState.RUNNING
    assert run.worker_id == "worker-1"
    assert run.local_unsafe is False


@pytest.mark.asyncio
async def test_worker_lookup_is_offloaded_from_event_loop(tmp_path: Path) -> None:
    """Docker or Kubernetes worker discovery cannot block the primary event loop."""
    manager, backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n")
    client.next_status = WorkerScriptStatus(state="running")
    event_loop_thread = threading.get_ident()

    await manager.status(context, run_id=run.run_id)

    assert backend.list_worker_thread_ids
    assert all(thread_id != event_loop_thread for thread_id in backend.list_worker_thread_ids)


def test_worker_workspace_symlink_cannot_escape_primary_storage(tmp_path: Path) -> None:
    """A worker workspace symlink cannot redirect private snapshots outside primary storage."""
    context = _context(tmp_path)
    state_root = context.runtime_paths.storage_root / "workers" / "worker-test"
    state_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (state_root / "workspace").symlink_to(outside, target_is_directory=True)
    worker = WorkerHandle(
        worker_id="worker-1",
        worker_key="user_agent:watcher:alice",
        endpoint="http://worker.test/api/sandbox-runner/execute",
        auth_token="worker-token",  # noqa: S106
        status="ready",
        backend_name="test",
        last_used_at=1.0,
        created_at=1.0,
        debug_metadata={"state_root": str(state_root)},
    )

    with pytest.raises(ScriptRunManagerError, match="inside its worker state root"):
        manager_module._worker_workspace(context, worker)


@pytest.mark.asyncio
async def test_cancel_revokes_before_signal_and_removes_force_kill_token(tmp_path: Path) -> None:
    """Force cancellation cannot leave a usable capability when SIGKILL skips shim cleanup."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n")
    token_path = client.launch_paths[run.run_id][1]

    cancelled = await manager.cancel(context, run_id=run.run_id, force=True)

    assert client.cancel_observed_revocation is True
    assert cancelled.state is ScriptRunState.CANCELLED
    assert not token_path.exists()


@pytest.mark.asyncio
async def test_lifecycle_revoke_is_durable_and_cancels_broker_without_live_context(tmp_path: Path) -> None:
    """Lifecycle revocation needs no bot and closes broker ownership immediately."""
    manager, _backend, _client = _manager(tmp_path)
    run = await manager.run(_context(tmp_path), source="print('ok')\n")

    revoked = await manager.revoke(run.run_id, reason="Owning agent was removed.")

    assert revoked.cancel_requested_at is not None
    assert revoked.cancellation_reason == "Owning agent was removed."
    assert manager.broker.cancelled_runs[-1] == run.run_id


@pytest.mark.asyncio
async def test_graceful_cancel_escalates_and_confirms_exit_before_terminal_state(tmp_path: Path) -> None:
    """A process still running after SIGTERM receives SIGKILL before CANCELLED is durable."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n")
    client.status_results = [
        WorkerScriptStatus(state="running"),
        WorkerScriptStatus(state="exited", exit_code=-9),
    ]

    cancelled = await manager.cancel(context, run_id=run.run_id)

    assert client.cancel_forces == [False, True]
    assert cancelled.state is ScriptRunState.CANCELLED
    assert cancelled.exit_code == -9


@pytest.mark.asyncio
async def test_broker_failure_does_not_skip_signal_and_status_retries_cancel(tmp_path: Path) -> None:
    """Revoked cancellation remains retryable when broker coordination fails after signaling."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n")
    manager.broker.failures.append(RuntimeError("broker unavailable"))

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await manager.cancel(context, run_id=run.run_id)

    pending = manager.store.get_run(run.run_id)
    assert pending.state is ScriptRunState.RUNNING
    assert pending.cancel_requested_at is not None
    assert client.cancel_forces == [False]

    status = await manager.status(context, run_id=run.run_id)

    assert status.run.state is ScriptRunState.CANCELLED
    assert client.cancel_forces == [False, False]


@pytest.mark.asyncio
async def test_signal_failure_stays_nonterminal_and_repeat_cancel_retries(tmp_path: Path) -> None:
    """An unconfirmed failed signal preserves cancellation intent for a later retry."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n")
    client.cancel_failures.append(RuntimeError("worker unavailable"))
    client.next_status = WorkerScriptStatus(state="running")

    with pytest.raises(RuntimeError, match="worker unavailable"):
        await manager.cancel(context, run_id=run.run_id, force=True)

    pending = manager.store.get_run(run.run_id)
    assert pending.state is ScriptRunState.RUNNING
    assert pending.cancel_requested_at is not None

    client.next_status = WorkerScriptStatus(state="exited", exit_code=-9)
    cancelled = await manager.cancel(context, run_id=run.run_id, force=True)

    assert cancelled.state is ScriptRunState.CANCELLED
    assert client.cancel_forces == [True, True]


@pytest.mark.asyncio
async def test_status_reports_pending_cancellation_with_recent_output(tmp_path: Path) -> None:
    """An unconfirmed termination remains inspectable through the status control."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n")
    client.next_status = WorkerScriptStatus(state="running", output="still stopping")

    with pytest.raises(ScriptRunManagerError, match="not yet confirmed"):
        await manager.cancel(context, run_id=run.run_id, force=True)

    status = await manager.status(context, run_id=run.run_id)

    assert status.run.state is ScriptRunState.RUNNING
    assert status.run.cancel_requested_at is not None
    assert status.output == "still stopping"


@pytest.mark.asyncio
async def test_token_cleanup_failure_does_not_mask_cancel_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort capability cleanup cannot replace a confirmed cancellation result."""
    manager, _backend, _client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n")

    def deny_cleanup(_workspace: Path, _run_id: str) -> None:
        message = "cleanup denied"
        raise PermissionError(message)

    monkeypatch.setattr(manager_module, "_remove_snapshot_token", deny_cleanup)

    cancelled = await manager.cancel(context, run_id=run.run_id, force=True)

    assert cancelled.state is ScriptRunState.CANCELLED


def test_token_cleanup_does_not_follow_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing a checked run directory cannot redirect capability deletion."""
    workspace = tmp_path / "workspace"
    run_dir = workspace / ".mindroom" / "script-runs" / "run-race"
    run_dir.mkdir(parents=True)
    (run_dir / "capability").write_text("original", encoding="utf-8")
    outside_run = tmp_path / "outside-run"
    outside_run.mkdir()
    outside_token = outside_run / "capability"
    outside_token.write_text("outside", encoding="utf-8")
    saved_run_dir = run_dir.with_name("run-race-saved")
    original_stat = manager_module.os.stat
    swapped = False

    def swap_directory() -> None:
        nonlocal swapped
        run_dir.rename(saved_run_dir)
        run_dir.symlink_to(outside_run, target_is_directory=True)
        swapped = True

    def swap_before_descriptor_stat(
        path: str,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if path == "capability" and dir_fd is not None and not swapped:
            swap_directory()
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(manager_module.os, "stat", swap_before_descriptor_stat)

    manager_module._remove_snapshot_token(
        tmp_path,
        "workspace/.mindroom/script-runs/run-race",
    )

    assert swapped is True
    assert outside_token.read_text(encoding="utf-8") == "outside"


def test_token_cleanup_ignores_directory_and_partial_snapshot(tmp_path: Path) -> None:
    """Directory mutation and an absent token are harmless cleanup outcomes."""
    workspace = tmp_path / "workspace"
    directory_token = workspace / ".mindroom" / "script-runs" / "run-directory" / "capability"
    directory_token.mkdir(parents=True)
    partial_run = workspace / ".mindroom" / "script-runs" / "run-partial"
    partial_run.mkdir()

    manager_module._remove_snapshot_token(
        tmp_path,
        "workspace/.mindroom/script-runs/run-directory",
    )
    manager_module._remove_snapshot_token(
        tmp_path,
        "workspace/.mindroom/script-runs/run-partial",
    )

    assert directory_token.is_dir()


def test_token_cleanup_ignores_descriptor_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Descriptor close errors cannot replace the best-effort cleanup outcome."""
    workspace = tmp_path / "workspace"
    token = workspace / ".mindroom" / "script-runs" / "run-close" / "capability"
    token.parent.mkdir(parents=True)
    token.write_text("secret", encoding="utf-8")
    original_close = manager_module.os.close

    def close_then_fail(descriptor: int) -> None:
        original_close(descriptor)
        message = "close failed"
        raise OSError(message)

    monkeypatch.setattr(manager_module.os, "close", close_then_fail)

    manager_module._remove_snapshot_token(
        tmp_path,
        "workspace/.mindroom/script-runs/run-close",
    )

    assert not token.exists()


@pytest.mark.asyncio
async def test_partial_snapshot_cleanup_preserves_original_launch_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token-write failure remains the launch error after partial snapshot cleanup."""
    manager, _backend, _client = _manager(tmp_path)
    original_write = manager_module._write_private_file
    calls = 0

    def fail_token_write(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            message = "token write denied"
            raise PermissionError(message)
        original_write(path, content)

    monkeypatch.setattr(manager_module, "_write_private_file", fail_token_write)

    with pytest.raises(PermissionError, match="token write denied"):
        await manager.run(_context(tmp_path), source="print('ok')\n")


@pytest.mark.asyncio
async def test_controls_hide_runs_from_other_requesters(tmp_path: Path) -> None:
    """Run lookup is both requester- and agent-scoped and fails as not found."""
    manager, _backend, _client = _manager(tmp_path)
    run = await manager.run(_context(tmp_path), source="print('ok')\n")

    with pytest.raises(ScriptRunManagerError, match="not found"):
        await manager.status(_context(tmp_path, requester_id="@bob:example.test"), run_id=run.run_id)


@pytest.mark.asyncio
async def test_source_path_must_be_regular_and_workspace_contained(tmp_path: Path) -> None:
    """Path launch rejects traversal and snapshots the original workspace bytes."""
    manager, _backend, _client = _manager(tmp_path)
    context = _context(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")

    with pytest.raises(ScriptRunManagerError, match="workspace"):
        await manager.run(context, path="../../outside.py")


@pytest.mark.asyncio
async def test_source_path_is_snapshotted_before_launch(tmp_path: Path) -> None:
    """A contained workspace file is copied so later source edits cannot change the run."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    workspace = agent_workspace_root_path(context.runtime_paths.storage_root, "watcher")
    workspace.mkdir(parents=True, exist_ok=True)
    original = workspace / "watch.py"
    original.write_text("print('ok')\n", encoding="utf-8")

    run = await manager.run(context, path="watch.py")
    original.write_text("print('changed')\n", encoding="utf-8")

    snapshot, _token = client.launch_paths[run.run_id]
    assert snapshot.read_text(encoding="utf-8") == "print('ok')\n"


@pytest.mark.asyncio
async def test_source_limit_and_exactly_one_input_are_enforced(tmp_path: Path) -> None:
    """Launch rejects ambiguous or oversized source before creating durable state."""
    manager, _backend, _client = _manager(tmp_path)
    context = _context(tmp_path)

    with pytest.raises(ScriptRunManagerError, match="exactly one"):
        await manager.run(context, source="print(1)", path="watch.py")
    with pytest.raises(ScriptRunManagerError, match="131072"):
        await manager.run(context, source="x" * (128 * 1024 + 1))

    assert manager.store.list_runs() == []


@pytest.mark.asyncio
async def test_concurrency_limit_is_scoped_to_owner_agent_and_worker(tmp_path: Path) -> None:
    """An owner cannot exceed active runs on one agent worker, while another owner can launch."""
    manager, _backend, _client = _manager(tmp_path)
    limits = ScriptRunLimits(max_concurrent_runs=1)
    await manager.run(_context(tmp_path), source="print('ok')\n", limits=limits)

    with pytest.raises(ScriptRunManagerError, match="concurrent"):
        await manager.run(_context(tmp_path), source="print('ok')\n", limits=limits)

    bob = await manager.run(
        _context(tmp_path, requester_id="@bob:example.test"),
        source="print('ok')\n",
        limits=limits,
    )
    assert bob.state is ScriptRunState.RUNNING


@pytest.mark.asyncio
async def test_slow_worker_launch_does_not_block_an_independent_reservation(tmp_path: Path) -> None:
    """Remote launch latency should not hold the process-wide capacity lock."""
    manager, _backend, client = _manager(tmp_path)
    client.launch_entered = asyncio.Event()
    client.second_launch_entered = asyncio.Event()
    client.launch_release = asyncio.Event()
    first = asyncio.create_task(manager.run(_context(tmp_path), source="print('ok')\n"))
    await client.launch_entered.wait()

    second = asyncio.create_task(
        manager.run(
            _context(tmp_path, requester_id="@bob:example.test"),
            source="print('ok')\n",
        ),
    )
    await asyncio.wait_for(client.second_launch_entered.wait(), timeout=5)

    try:
        assert len(client.requested_handles) == 2
    finally:
        client.launch_release.set()
        await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_owned_run_lookup_runs_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Frequent status reads must not execute SQLite work on the request loop."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n")
    client.next_status = WorkerScriptStatus(state="running", output="ready")
    main_thread = threading.get_ident()
    lookup_threads: list[int] = []
    original_get_run = manager.store.get_run

    def recording_get_run(run_id: str) -> ScriptRunRecord:
        lookup_threads.append(threading.get_ident())
        return original_get_run(run_id)

    monkeypatch.setattr(manager.store, "get_run", recording_get_run)

    status = await manager.status(context, run_id=run.run_id)

    assert status.output == "ready"
    assert lookup_threads
    assert main_thread not in lookup_threads


@pytest.mark.asyncio
async def test_reconcile_records_exit_and_removes_raw_token(tmp_path: Path) -> None:
    """Terminal reconciliation records process outcome and cleans capability material."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(context, source="print('ok')\n")
    client.next_status = WorkerScriptStatus(state="exited", output="done", exit_code=0)

    reconciled = await manager.reconcile(context, run_id=run.run_id)

    assert reconciled.state is ScriptRunState.EXITED
    assert manager.broker.cancelled_runs == [run.run_id]
    assert manager.broker.cancelled_states == [ScriptRunState.EXITED]
    assert not client.launch_paths[run.run_id][1].exists()


@pytest.mark.asyncio
async def test_reconcile_enforces_runtime_limit_through_revocation_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired run is revoked and broker-cancelled before its worker receives a signal."""
    manager, _backend, client = _manager(tmp_path)
    context = _context(tmp_path)
    run = await manager.run(
        context,
        source="print('ok')\n",
        limits=ScriptRunLimits(max_runtime_hours=1e-12),
    )
    monkeypatch.setattr(manager_module, "_runtime_expired", lambda _run: True)

    reconciled = await manager.reconcile(context, run_id=run.run_id)

    assert reconciled.state is ScriptRunState.CANCELLED
    assert reconciled.cancellation_reason == "Background script maximum runtime exceeded."
    assert manager.broker.cancelled_runs == [run.run_id]
    assert client.cancel_forces == [False]


@pytest.mark.asyncio
async def test_process_only_reconciliation_does_not_rescan_broker_after_trusted_revocation(tmp_path: Path) -> None:
    """Reload closes broker ownership once before process-only reconciliation."""
    manager, _backend, client = _manager(tmp_path)
    run = await manager.run(_context(tmp_path), source="print('ok')\n")
    client.next_status = WorkerScriptStatus(state="exited", exit_code=-15)

    await manager.revoke(run.run_id, reason="Owning agent was removed by configuration reload.")
    reconciled = await manager.reconcile_durable(run_id=run.run_id, broker_revoked=True)

    assert reconciled.state is ScriptRunState.CANCELLED
    assert manager.broker.cancelled_runs == [run.run_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
@pytest.mark.parametrize("execution_mode", ["off", "local", "disabled"])
async def test_explicit_local_mode_uses_existing_supervisor_and_marks_run_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    execution_mode: str,
) -> None:
    """Only an explicit disabled-sandbox mode may launch through the primary shell supervisor."""
    manager, _backend, _client = _manager(tmp_path, mode=execution_mode, backend=backend)
    context = _context(tmp_path, mode=execution_mode, backend=backend)
    observed: dict[str, object] = {}

    async def launch_local(
        socket_path: str,
        *,
        namespace: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        tail: int,
        timeout: float,  # noqa: ASYNC109
        handle: str | None = None,
    ) -> str:
        observed.update(
            socket_path=socket_path,
            namespace=namespace,
            argv=argv,
            env=env,
            cwd=cwd,
            tail=tail,
            timeout=timeout,
            handle=handle,
        )
        assert handle is not None
        starting = manager.store.list_runs(include_finished=False)[0]
        assert handle == f"shell:{starting.run_id.removeprefix('script-')}"
        return f"Started background process\nHandle: {handle}"

    monkeypatch.setattr(manager_module, "ensure_shell_supervisor", lambda: "/control/shell.sock")
    monkeypatch.setattr(manager_module, "run_command_via_supervisor", launch_local)

    run = await manager.run(context, source="print('ok')\n")

    assert run.local_unsafe is True
    assert run.worker_id is None
    assert run.worker_key is None
    assert observed["handle"] == f"shell:{run.run_id.removeprefix('script-')}"
    assert observed["socket_path"] == "/control/shell.sock"
    assert observed["namespace"] == f"script:local:{run.run_id}"


@pytest.mark.asyncio
async def test_local_launch_rechecks_durable_intent_immediately_before_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable cancellation committed during snapshot preparation prevents local spawn."""
    manager, _backend, _client = _manager(tmp_path, mode="local")
    context = _context(tmp_path, mode="local")
    original_write_snapshot = manager_module._write_snapshot
    launch_calls: list[str] = []

    def snapshot_then_cancel(
        workspace: Path,
        run_id: str,
        *,
        source: bytes,
        token: str,
    ) -> tuple[Path, Path]:
        paths = original_write_snapshot(workspace, run_id, source=source, token=token)
        manager.store.request_cancel(run_id, reason="cancelled during snapshot")
        return paths

    async def launch_local(*_args: object, **_kwargs: object) -> str:
        launch_calls.append("called")
        return "unexpected launch"

    monkeypatch.setattr(manager_module, "_write_snapshot", snapshot_then_cancel)
    monkeypatch.setattr(manager_module, "ensure_shell_supervisor", lambda: "/control/shell.sock")
    monkeypatch.setattr(manager_module, "run_command_via_supervisor", launch_local)

    launch_result = await manager.run(context, source="print('ok')\n")

    assert launch_result.state is ScriptRunState.CANCELLED
    assert launch_calls == []


@pytest.mark.asyncio
async def test_ambiguous_local_launch_failure_remains_retryable_until_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfirmed local launch owner stays nonterminal for another signal attempt."""
    manager, _backend, _client = _manager(tmp_path, mode="local")
    context = _context(tmp_path, mode="local")
    killed_handles: list[str] = []
    termination_confirmed = False

    async def failed_launch(*_args: object, **_kwargs: object) -> str:
        message = "launch response lost"
        raise RuntimeError(message)

    def kill_local(
        _socket_path: str,
        *,
        namespace: str,
        handle: str,
        force: bool,
    ) -> str:
        del namespace
        assert force is (len(killed_handles) == 0)
        killed_handles.append(handle)
        return "Force-killed process" if force else "Terminated process"

    def check_local(
        _socket_path: str,
        *,
        namespace: str,
        handle: str,
    ) -> str:
        del namespace, handle
        if termination_confirmed:
            return "Status: FINISHED (exit code -9)"
        return "Status: RUNNING"

    monkeypatch.setattr(manager_module, "ensure_shell_supervisor", lambda: "/control/shell.sock")
    monkeypatch.setattr(manager_module, "run_command_via_supervisor", failed_launch)
    monkeypatch.setattr(manager_module, "kill_command_via_supervisor", kill_local)
    monkeypatch.setattr(manager_module, "check_command_via_supervisor", check_local)

    with pytest.raises(RuntimeError, match="launch response lost"):
        await manager.run(context, source="print('ok')\n")

    stored = manager.store.list_runs()[0]
    assert stored.state is ScriptRunState.STARTING
    assert stored.cancel_requested_at is not None
    assert killed_handles == [f"shell:{stored.run_id.removeprefix('script-')}"]

    termination_confirmed = True
    reconciled = await manager.reconcile(context, run_id=stored.run_id)

    assert reconciled.state is ScriptRunState.CANCELLED
    assert killed_handles == [f"shell:{stored.run_id.removeprefix('script-')}"] * 2


@pytest.mark.asyncio
async def test_local_launch_adopts_cancellation_before_running_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local launch completion cannot overwrite a concurrently confirmed cancellation."""
    manager, _backend, _client = _manager(tmp_path, mode="local")
    context = _context(tmp_path, mode="local")
    launch_entered = asyncio.Event()
    launch_release = asyncio.Event()

    async def launch_local(
        _socket_path: str,
        *,
        namespace: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        tail: int,
        timeout: float,  # noqa: ASYNC109
        handle: str | None = None,
    ) -> str:
        del namespace, argv, env, cwd, tail, timeout
        assert handle is not None
        launch_entered.set()
        await launch_release.wait()
        return f"Started background process\nHandle: {handle}"

    def kill_local(
        _socket_path: str,
        *,
        namespace: str,
        handle: str,
        force: bool,
    ) -> str:
        del namespace, handle, force
        return "Force-killed process"

    def check_local(
        _socket_path: str,
        *,
        namespace: str,
        handle: str,
    ) -> str:
        del namespace, handle
        return "Status: FINISHED (exit code -9)"

    monkeypatch.setattr(manager_module, "ensure_shell_supervisor", lambda: "/control/shell.sock")
    monkeypatch.setattr(manager_module, "run_command_via_supervisor", launch_local)
    monkeypatch.setattr(manager_module, "kill_command_via_supervisor", kill_local)
    monkeypatch.setattr(manager_module, "check_command_via_supervisor", check_local)

    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    await launch_entered.wait()
    [starting] = manager.store.list_runs(include_finished=False)
    cancelled = await manager.cancel(context, run_id=starting.run_id, force=True)
    launch_release.set()

    launch_result = await launch
    assert cancelled.state is ScriptRunState.CANCELLED
    assert launch_result.state is ScriptRunState.CANCELLED
    assert manager.store.get_run(starting.run_id).state is ScriptRunState.CANCELLED


@pytest.mark.asyncio
async def test_local_launch_does_not_publish_running_after_unconfirmed_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local process with cancellation intent remains retryable instead of becoming running."""
    manager, _backend, _client = _manager(tmp_path, mode="local")
    context = _context(tmp_path, mode="local")
    launch_entered = asyncio.Event()
    launch_release = asyncio.Event()
    process_exited = False

    async def launch_local(
        _socket_path: str,
        *,
        namespace: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str | None,
        tail: int,
        timeout: float,  # noqa: ASYNC109
        handle: str | None = None,
    ) -> str:
        del namespace, argv, env, cwd, tail, timeout
        assert handle is not None
        launch_entered.set()
        await launch_release.wait()
        return f"Started background process\nHandle: {handle}"

    def kill_local(
        _socket_path: str,
        *,
        namespace: str,
        handle: str,
        force: bool,
    ) -> str:
        del namespace, handle
        return "Force-killed process" if force else "Terminated process"

    def check_local(
        _socket_path: str,
        *,
        namespace: str,
        handle: str,
    ) -> str:
        del namespace, handle
        if process_exited:
            return "Status: FINISHED (exit code -9)"
        return "Status: RUNNING"

    monkeypatch.setattr(manager_module, "ensure_shell_supervisor", lambda: "/control/shell.sock")
    monkeypatch.setattr(manager_module, "run_command_via_supervisor", launch_local)
    monkeypatch.setattr(manager_module, "kill_command_via_supervisor", kill_local)
    monkeypatch.setattr(manager_module, "check_command_via_supervisor", check_local)

    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    await launch_entered.wait()
    [starting] = manager.store.list_runs(include_finished=False)
    with pytest.raises(ScriptRunManagerError, match="not yet confirmed"):
        await manager.cancel(context, run_id=starting.run_id, force=True)
    launch_release.set()
    launch_result = await launch

    assert launch_result.state is ScriptRunState.STARTING
    assert launch_result.cancel_requested_at is not None

    process_exited = True
    reconciled = await manager.reconcile(context, run_id=starting.run_id)
    assert reconciled.state is ScriptRunState.CANCELLED
