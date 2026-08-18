"""Tests for primary-owned background script lifecycle management."""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.message_target import MessageTarget
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRunManager, ScriptRunManagerError
from mindroom.script_runs.models import ScriptRunState, ScriptToolGrant
from mindroom.script_runs.store import ScriptRunStore
from mindroom.script_runs.worker_client import (
    WorkerScriptCancel,
    WorkerScriptLaunch,
    WorkerScriptStatus,
)
from mindroom.tool_system.worker_routing import agent_workspace_root_path, worker_root_path
from mindroom.workers.models import WorkerHandle, WorkerSpec
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _runtime_paths(tmp_path: Path, *, mode: str | None = "all") -> RuntimePaths:
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env=({"MINDROOM_SANDBOX_EXECUTION_MODE": mode} if mode is not None else {}),
    )


def _context(
    tmp_path: Path,
    *,
    requester_id: str = "@alice:example.test",
    mode: str | None = "all",
) -> ToolRuntimeContext:
    runtime_paths = _runtime_paths(tmp_path, mode=mode)
    config = Config(
        agents={
            "watcher": {
                "display_name": "Watcher",
                "worker_scope": "user_agent",
                "tools": ["script", "calculator"],
            },
        },
        defaults={"tools": []},
    )
    return make_test_tool_runtime_context(
        agent_name="watcher",
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


@dataclass
class _WorkerBackend:
    store: ScriptRunStore
    runtime_paths: RuntimePaths
    handles: dict[str, WorkerHandle] = field(default_factory=dict)
    saw_starting: bool = False

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
    next_status: WorkerScriptStatus = field(default_factory=lambda: WorkerScriptStatus(state="running"))

    async def launch(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        source_path: str,
        source_digest: str,
        token_path: str,
        gateway_url: str,
        private_agent_names: tuple[str, ...] | None = None,
        tail_lines: int = 200,
    ) -> WorkerScriptLaunch:
        del source_digest, gateway_url, private_agent_names, tail_lines
        starting = self.store.get_run(run_id)
        assert starting.state is ScriptRunState.STARTING
        assert starting.worker_id == worker.worker_id
        workspace = Path(worker.debug_metadata["state_root"]) / "workspace"
        source = workspace / source_path
        token = workspace / token_path
        assert source.read_text(encoding="utf-8") == "print('ok')\n"
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert stat.S_IMODE(token.stat().st_mode) == 0o600
        assert stat.S_IMODE(source.parent.stat().st_mode) == 0o700
        self.launch_paths[run_id] = (source, token)
        return WorkerScriptLaunch(supervisor_handle="shell:1234abcd")

    async def status(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        supervisor_handle: str,
    ) -> WorkerScriptStatus:
        del worker, run_id, supervisor_handle
        return self.next_status

    async def cancel(
        self,
        worker: WorkerHandle,
        *,
        run_id: str,
        supervisor_handle: str,
        force: bool = False,
    ) -> WorkerScriptCancel:
        del worker, supervisor_handle
        self.cancel_forces.append(force)
        self.cancel_observed_revocation = self.store.get_run(run_id).cancel_requested_at is not None
        return WorkerScriptCancel(cancel_requested=True, already_finished=False, unknown_handle=False)


def _manager(tmp_path: Path, *, mode: str | None = "all") -> tuple[ScriptRunManager, _WorkerBackend, _WorkerClient]:
    context = _context(tmp_path, mode=mode)
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
    )
    return manager, backend, client


@pytest.mark.asyncio
async def test_launch_persists_starting_before_worker_and_private_snapshot(tmp_path: Path) -> None:
    """Worker allocation sees durable intent, then the launch sees private snapshotted files."""
    manager, backend, client = _manager(tmp_path)

    run = await manager.run(
        _context(tmp_path),
        source="print('ok')\n",
        limits=ScriptRunLimits(max_concurrent_runs=2, max_tool_calls_per_minute=4, max_runtime_hours=1),
    )

    assert backend.saw_starting is True
    assert run.state is ScriptRunState.RUNNING
    assert run.worker_key is not None
    assert run.worker_id == "worker-1"
    assert run.supervisor_handle == "shell:1234abcd"
    assert run.max_tool_calls_per_minute == 4
    assert run.max_runtime_seconds == 3600
    assert client.launch_paths[run.run_id][0].is_file()


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


@pytest.mark.asyncio
async def test_worker_keys_are_requester_and_agent_scoped(tmp_path: Path) -> None:
    """Different owners cannot share a user-agent worker or its run directory."""
    manager, _backend, _client = _manager(tmp_path)

    alice = await manager.run(_context(tmp_path), source="print('ok')\n")
    bob = await manager.run(_context(tmp_path, requester_id="@bob:example.test"), source="print('ok')\n")

    assert alice.worker_key != bob.worker_key
    assert alice.worker_id != bob.worker_id


@pytest.mark.asyncio
async def test_configured_worker_backend_is_used_without_execution_mode_override(tmp_path: Path) -> None:
    """An enabled primary worker backend remains the default when no mode override is authored."""
    manager, _backend, _client = _manager(tmp_path, mode=None)

    run = await manager.run(_context(tmp_path, mode=None), source="print('ok')\n")

    assert run.state is ScriptRunState.RUNNING
    assert run.worker_id == "worker-1"
    assert run.local_unsafe is False


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
        manager_module._worker_workspace(context, worker)  # noqa: SLF001


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
async def test_explicit_local_mode_uses_existing_supervisor_and_marks_run_unsafe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an explicit disabled-sandbox mode may launch through the primary shell supervisor."""
    manager, _backend, _client = _manager(tmp_path, mode="local")
    context = _context(tmp_path, mode="local")
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
    ) -> str:
        observed.update(
            socket_path=socket_path,
            namespace=namespace,
            argv=argv,
            env=env,
            cwd=cwd,
            tail=tail,
            timeout=timeout,
        )
        return "Started background process\nHandle: shell:1234abcd"

    monkeypatch.setattr(manager_module, "ensure_shell_supervisor", lambda: "/control/shell.sock")
    monkeypatch.setattr(manager_module, "run_command_via_supervisor", launch_local)

    run = await manager.run(context, source="print('ok')\n")

    assert run.local_unsafe is True
    assert run.worker_id is None
    assert run.worker_key is None
    assert run.supervisor_handle == "shell:1234abcd"
    assert observed["socket_path"] == "/control/shell.sock"
    assert observed["namespace"] == f"script:local:{run.run_id}"
