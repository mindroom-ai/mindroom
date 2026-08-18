"""Tests for process-local background-script runtime lifecycle coordination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.orchestration.config_updates import ConfigUpdatePlan, build_config_update_plan
from mindroom.orchestration.script_runtime import (
    ScriptRuntimeLifecycle,
    _ScriptRuntimeUnavailableError,
    build_script_runtime,
    script_gateway_url,
)
from mindroom.script_runs.models import ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.store import ScriptRunNotFoundError, ScriptRunStore
from mindroom.script_runs.worker_client import ScriptWorkerError
from mindroom.tool_approval import BackgroundScriptToolOrigin
from mindroom.tool_system.worker_routing import (
    build_agent_toolkit_worker_target,
    build_tool_execution_identity,
    serialize_tool_execution_identity,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env={"MINDROOM_SANDBOX_EXECUTION_MODE": "all"},
    )


def _config(*, private: bool = False) -> Config:
    agent: dict[str, object] = {"display_name": "Watcher", "tools": ["script", "calculator"]}
    if private:
        agent["private"] = {"per": "user_agent", "root": "private/watcher"}
    return Config(agents={"watcher": agent}, defaults={"tools": []})


def _plan(current: Config, updated: Config) -> ConfigUpdatePlan:
    configured = {"router", *updated.agents, *updated.teams}
    existing = {"router", *current.agents, *current.teams}
    return build_config_update_plan(
        current_config=current,
        new_config=updated,
        configured_entities=configured,
        existing_entities=existing,
        agent_bots={name: MagicMock() for name in existing},
    )


def _run(
    runtime_paths: RuntimePaths,
    *,
    run_id: str = "run-1",
    state: ScriptRunState = ScriptRunState.RUNNING,
) -> ScriptRunRecord:
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name="watcher",
        transport_agent_name="watcher",
        runtime_paths=runtime_paths,
        requester_id="@alice:example.test",
        room_id="!room:example.test",
        thread_id="$thread:example.test",
        resolved_thread_id="$thread:example.test",
        session_id="!room:example.test:$thread:example.test",
    )
    target = build_agent_toolkit_worker_target(
        "user_agent",
        "watcher",
        is_private=False,
        execution_identity=identity,
        runtime_paths=runtime_paths,
    )
    return ScriptRunRecord(
        run_id=run_id,
        agent_name="watcher",
        owner_user_id="@alice:example.test",
        room_id="!room:example.test",
        thread_root_event_id="$thread:example.test",
        execution_identity=serialize_tool_execution_identity(identity),
        source_digest="digest",
        grants=(ScriptToolGrant("calculator", "add"),),
        token_hash="capability",  # noqa: S106
        worker_key=target.worker_key,
        worker_id="worker-1",
        supervisor_handle="shell:0123456789abcdef0123456789abcdef",
        state=state,
    )


@dataclass
class _Backend:
    handles: list[WorkerHandle]
    actions: list[str] = field(default_factory=list)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        del include_idle, now
        return list(self.handles)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        del now
        self.actions.append(f"touch:{worker_key}")
        return next((handle for handle in self.handles if handle.worker_key == worker_key), None)


@dataclass
class _Lease:
    manager: _Backend
    released: bool = False

    def release(self) -> None:
        self.released = True


def _worker(run: ScriptRunRecord) -> WorkerHandle:
    assert run.worker_key is not None
    return WorkerHandle(
        worker_id="worker-1",
        worker_key=run.worker_key,
        endpoint="http://worker.test/api/sandbox-runner/execute",
        auth_token="worker-token",  # noqa: S106
        status="ready",
        backend_name="test",
        last_used_at=1.0,
        created_at=1.0,
    )


def _stored_run(
    store: ScriptRunStore,
    runtime_paths: RuntimePaths,
    *,
    run_id: str = "run-1",
) -> ScriptRunRecord:
    created = store.create_run(_run(runtime_paths, run_id=run_id, state=ScriptRunState.STARTING))
    return store.transition_run(
        created.run_id,
        state=ScriptRunState.RUNNING,
        worker_id="worker-1",
    )


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_retention_rejects_non_finite_values(tmp_path: Path, raw: str) -> None:
    """Retention must be finite so pruning has a meaningful cutoff."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_SCRIPT_RETENTION_SECONDS": raw,
        },
    )

    with pytest.raises(ValueError, match="positive number"):
        build_script_runtime(
            runtime_paths,
            config_provider=_config,
            bot_provider=lambda _name: None,
            worker_lease_provider=lambda: None,
            api_enabled=True,
        )


def test_dedicated_workers_require_an_explicit_reachable_gateway(tmp_path: Path) -> None:
    """A dedicated worker must not receive an unreachable primary loopback URL."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_WORKER_BACKEND": "docker",
        },
    )

    with pytest.raises(ValueError, match="MINDROOM_SCRIPT_GATEWAY_URL"):
        script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
async def test_lifecycle_activates_after_both_agent_registry_and_api_are_ready(tmp_path: Path) -> None:
    """Activation waits for both composition roots and shutdown clears the binding."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = SimpleNamespace(gateway_url="", worker_backend=MagicMock())
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=lambda: None,
    )
    bound_managers: list[object | None] = []

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "mindroom.orchestration.script_runtime.bind_script_run_manager",
            bound_managers.append,
        )
        await runtime.start()
        assert bound_managers == []

        runtime.bind_api("http://primary.test/api/script-gateway/")
        assert runtime._startup_task is not None
        await asyncio.wait_for(runtime._startup_task, timeout=1)
        assert bound_managers == [manager]
        assert manager.gateway_url == "http://primary.test/api/script-gateway"

        await runtime.shutdown()

    assert bound_managers == [manager, None]


@pytest.mark.asyncio
async def test_removed_agent_revokes_and_cancels_running_scripts(tmp_path: Path) -> None:
    """Removal records revocation before unavailable live runtime resolution."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    manager = SimpleNamespace(
        request_revocation=MagicMock(side_effect=request_revocation),
        revoke=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(),
    )
    resolver = SimpleNamespace(resolve=MagicMock(side_effect=_ScriptRuntimeUnavailableError("bot is gone")))
    old_config = _config()
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=resolver,
        config_provider=lambda: old_config,
        worker_lease_provider=lambda: None,
    )
    plan = _plan(old_config, Config(defaults={"tools": []}))

    await runtime.apply_update_plan(plan)

    manager.request_revocation.assert_called_once_with(
        run_id="run-1",
        reason="Owning agent was removed by configuration reload.",
    )
    assert store.get_run(run.run_id).cancel_requested_at is not None
    manager.revoke.assert_awaited_once_with(
        run.run_id,
        reason="Owning agent was removed by configuration reload.",
    )


@pytest.mark.asyncio
async def test_isolation_change_interrupts_running_script_without_replacing_services(tmp_path: Path) -> None:
    """Isolation changes interrupt runs without replacing process-local services."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    context = SimpleNamespace(agent_name="watcher", requester_id=run.owner_user_id)
    manager = SimpleNamespace(
        request_revocation=MagicMock(return_value=run),
        revoke=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(return_value=run),
    )
    resolver = SimpleNamespace(resolve=MagicMock(return_value=context))
    old_config = _config()
    broker = MagicMock()
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=lambda: old_config,
        worker_lease_provider=lambda: None,
    )
    plan = _plan(old_config, _config(private=True))

    await runtime.apply_update_plan(plan)

    assert runtime.store is store
    assert runtime.broker is broker
    assert runtime.manager is manager
    manager.request_revocation.assert_called_once_with(
        run_id="run-1",
        reason="Agent isolation changed during configuration reload.",
    )
    manager.reconcile_durable.assert_awaited_once_with(run_id="run-1")
    manager.revoke.assert_awaited_once_with(
        run.run_id,
        reason="Agent isolation changed during configuration reload.",
    )


@pytest.mark.asyncio
async def test_ordinary_agent_restart_keeps_running_script_retryable(tmp_path: Path) -> None:
    """An ordinary agent restart leaves its durable script authority retryable."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    _stored_run(store, runtime_paths)
    old_config = _config()
    manager = SimpleNamespace(request_revocation=MagicMock(), reconcile=AsyncMock())
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda: None,
    )
    changed = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "A changed role", "tools": ["script"]}},
        defaults={"tools": []},
    )

    await runtime.apply_update_plan(_plan(old_config, changed))

    manager.request_revocation.assert_not_called()
    assert store.get_run("run-1").state is ScriptRunState.RUNNING


@pytest.mark.asyncio
async def test_reconciliation_touches_live_worker_before_status_check(tmp_path: Path) -> None:
    """Reconciliation refreshes worker leases before querying process truth."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    backend = _Backend([_worker(run)])

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        backend.actions.append(f"reconcile:{run_id}")
        return run

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=reconcile_durable),
        resolver=SimpleNamespace(
            resolve=MagicMock(side_effect=AssertionError("must not resolve live runtime")),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda: _Lease(backend),
    )

    await runtime.reconcile_once()

    assert backend.actions == [f"touch:{run.worker_key}", "reconcile:run-1"]


@pytest.mark.asyncio
async def test_backend_generation_lease_is_held_until_its_live_run_finishes(tmp_path: Path) -> None:
    """A replacement backend cannot shut down the generation hosting a live run."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    first = _Lease(_Backend([_worker(run)]))
    second = _Lease(_Backend([]))
    leases = iter((first, second, second))
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=AsyncMock(return_value=run)),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=lambda: next(leases),
    )

    await runtime.reconcile_once()
    await runtime.reconcile_once()

    assert first.released is False
    assert runtime._worker_backend_for(run) is first.manager

    store.transition_run(run.run_id, state=ScriptRunState.EXITED, exit_code=0)
    await runtime.reconcile_once()

    assert first.released is True


@pytest.mark.asyncio
async def test_reconciliation_leaves_worker_transport_ambiguity_retryable_and_continues(tmp_path: Path) -> None:
    """One unavailable worker does not prevent reconciliation of later runs."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    first = _stored_run(store, runtime_paths)
    second = _stored_run(store, runtime_paths, run_id="run-2")
    reconciled: list[str] = []

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        if run_id == first.run_id:
            message = "worker unavailable"
            raise ScriptWorkerError(message, failure_kind="worker")
        reconciled.append(run_id)
        return second

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=reconcile_durable),
        resolver=SimpleNamespace(
            resolve=MagicMock(side_effect=AssertionError("must not resolve live runtime")),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda: None,
    )

    await runtime.reconcile_once()

    assert store.get_run(first.run_id).state is ScriptRunState.RUNNING
    assert reconciled == [second.run_id]


@pytest.mark.asyncio
async def test_backend_failure_isolated_from_later_run_reconciliation(tmp_path: Path) -> None:
    """One typed backend failure does not abort the rest of a lifecycle pass."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    first = _stored_run(store, runtime_paths)
    second = _stored_run(store, runtime_paths, run_id="run-2")
    reconciled: list[str] = []

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        if run_id == first.run_id:
            message = "backend unavailable"
            raise WorkerBackendError(message)
        reconciled.append(run_id)
        return second

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=reconcile_durable),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=lambda: None,
    )

    await runtime.reconcile_once()

    assert reconciled == [second.run_id]


@pytest.mark.asyncio
async def test_backend_provider_failure_does_not_abort_run_reconciliation(tmp_path: Path) -> None:
    """A typed provider failure leaves durable run reconciliation independent."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    reconciled: list[str] = []

    def unavailable_provider() -> _Lease:
        message = "provider unavailable"
        raise WorkerBackendError(message)

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        reconciled.append(run_id)
        return run

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=reconcile_durable),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=unavailable_provider,
    )

    await runtime.reconcile_once()

    assert reconciled == [run.run_id]


@pytest.mark.asyncio
async def test_worker_touch_failure_does_not_abort_run_reconciliation(tmp_path: Path) -> None:
    """One failed keepalive is isolated before the process-truth sweep continues."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    backend = _Backend([_worker(run)])
    backend.touch_worker = MagicMock(side_effect=WorkerBackendError("touch unavailable"))
    reconciled: list[str] = []

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        reconciled.append(run_id)
        return run

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=reconcile_durable),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=lambda: _Lease(backend),
    )

    await runtime.reconcile_once()

    assert reconciled == [run.run_id]


@pytest.mark.asyncio
async def test_reconciliation_pass_has_one_overall_deadline(tmp_path: Path) -> None:
    """A stuck worker operation cannot serialize or indefinitely block startup."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    _stored_run(store, runtime_paths)
    never = asyncio.Event()

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        del run_id
        await never.wait()
        message = "unreachable"
        raise AssertionError(message)

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=reconcile_durable),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=lambda: None,
        pass_timeout_seconds=0.02,
    )
    started = asyncio.get_running_loop().time()

    await runtime.reconcile_once()

    assert asyncio.get_running_loop().time() - started < 0.2


@pytest.mark.asyncio
async def test_reload_revokes_all_runs_before_bounded_process_reconciliation(tmp_path: Path) -> None:
    """One stuck signal path cannot prevent durable revocation of another removed-owner run."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    first = _stored_run(store, runtime_paths)
    second = _stored_run(store, runtime_paths, run_id="run-2")
    never = asyncio.Event()
    broker_revocations: list[str] = []

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    async def revoke(run_id: str, *, reason: str) -> ScriptRunRecord:
        broker_revocations.append(run_id)
        return store.request_cancel(run_id, reason=reason)

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        assert set(broker_revocations) == {first.run_id, second.run_id}
        if run_id == first.run_id:
            await never.wait()
        return store.get_run(run_id)

    current = _config()
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(
            request_revocation=request_revocation,
            revoke=revoke,
            reconcile_durable=reconcile_durable,
        ),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=lambda: current,
        worker_lease_provider=lambda: None,
        pass_timeout_seconds=0.02,
    )

    await runtime.apply_update_plan(_plan(current, Config(defaults={"tools": []})))

    assert store.get_run(first.run_id).cancel_requested_at is not None
    assert store.get_run(second.run_id).cancel_requested_at is not None
    assert set(broker_revocations) == {first.run_id, second.run_id}


@pytest.mark.asyncio
async def test_started_lifecycle_periodically_enforces_run_limits(tmp_path: Path) -> None:
    """Max-runtime reconciliation continues without a caller requesting status."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    _stored_run(store, runtime_paths)
    reconciled_twice = asyncio.Event()
    calls = 0

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        nonlocal calls
        calls += 1
        if calls == 2:
            reconciled_twice.set()
        return store.get_run(run_id)

    manager = SimpleNamespace(
        gateway_url="",
        worker_backend=None,
        reconcile_durable=reconcile_durable,
        cleanup_snapshot=AsyncMock(return_value=True),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=lambda: None,
        reconcile_interval_seconds=0.01,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")

    await runtime.start()
    await asyncio.wait_for(reconciled_twice.wait(), timeout=0.2)
    await runtime.shutdown()

    assert calls >= 2


@pytest.mark.asyncio
async def test_terminal_run_is_pruned_only_after_retention_and_snapshot_cleanup(tmp_path: Path) -> None:
    """Retention prunes a terminal row only after its source snapshot is gone."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    running = _stored_run(store, runtime_paths)
    terminal = store.transition_run(running.run_id, state=ScriptRunState.EXITED, exit_code=0)
    assert terminal.finished_at is not None
    manager = SimpleNamespace(
        revoke=AsyncMock(),
        reconcile=AsyncMock(),
        cleanup_snapshot=AsyncMock(return_value=True),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(
            resolve=MagicMock(side_effect=AssertionError("must not resolve live runtime")),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda: None,
        retention_seconds=60.0,
    )
    finished_at = datetime.fromisoformat(terminal.finished_at)

    await runtime.prune_once(now=finished_at + timedelta(seconds=59))
    assert store.get_run("run-1").state is ScriptRunState.EXITED
    manager.cleanup_snapshot.assert_not_awaited()

    await runtime.prune_once(now=finished_at + timedelta(seconds=61))
    manager.cleanup_snapshot.assert_awaited_once_with(terminal)
    with pytest.raises(ScriptRunNotFoundError):
        store.get_run("run-1")


@pytest.mark.asyncio
async def test_live_resolver_rebuilds_exact_context_and_worker_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The resolver rebuilds exact runtime, worker, and approval authority."""
    runtime_paths = _runtime_paths(tmp_path)
    run = _run(runtime_paths)
    expected_context = SimpleNamespace(
        agent_name="watcher",
        requester_id=run.owner_user_id,
        room_id=run.room_id,
        resolved_thread_id=run.thread_root_event_id,
        config=_config(),
        current_config=_config(),
        runtime_paths=runtime_paths,
    )
    support = SimpleNamespace(build_context=MagicMock(return_value=expected_context))
    bot = SimpleNamespace(agent_name="watcher", running=True, _tool_runtime_support=support)
    backend = _Backend([_worker(run)])
    approvals = SimpleNamespace(
        request_background_approval=AsyncMock(
            return_value=BackgroundApprovalDecision(status="approved", reason="operator decision"),
        ),
    )
    runtime = build_script_runtime(
        runtime_paths,
        config_provider=_config,
        bot_provider=lambda _name: cast("AgentBot", bot),
        worker_lease_provider=lambda: _Lease(backend),
        api_enabled=True,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")
    await runtime.start()
    resolver = runtime.resolver
    resolver.approval_provider = lambda: approvals
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.resolve_tool_approval_approver",
        lambda *_args: run.owner_user_id,
    )

    context = resolver.resolve(run, correlation_id="run-1:call-1")
    authority = resolver.resolve_worker_authority(run, context=context)
    decision = await resolver.request_approval(
        origin=BackgroundScriptToolOrigin(
            run_id="run-1",
            call_id="call-1",
            requester_id=run.owner_user_id,
            toolkit_name="calculator",
            function_name="add",
        ),
        context=context,
        grant=ScriptToolGrant("calculator", "add"),
        arguments={"a": 1, "b": 2},
        timeout_seconds=30.0,
    )

    assert context is expected_context
    assert authority.worker_id == "worker-1"
    assert authority.worker_target.worker_scope is None
    assert decision.approved is True
    built_target = support.build_context.call_args.args[0]
    assert built_target.room_id == run.room_id
    assert built_target.resolved_thread_id == run.thread_root_event_id
    approvals.request_background_approval.assert_awaited_once()
    await runtime.shutdown()
