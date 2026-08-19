"""Tests for process-local background-script runtime lifecycle coordination."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import mindroom.workers.runtime as workers_runtime_module
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.orchestration.config_updates import ConfigUpdatePlan, build_config_update_plan
from mindroom.orchestration.script_runtime import (
    ScriptRuntimeLifecycle,
    _release_worker_leases_before_deadline,
    _ScriptRuntimeUnavailableError,
    build_script_runtime,
    script_gateway_url,
)
from mindroom.script_runs.broker import ScriptToolBroker
from mindroom.script_runs.manager import ScriptRunManager
from mindroom.script_runs.models import ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.store import ScriptRunNotFoundError, ScriptRunStore
from mindroom.script_runs.worker_client import (
    ScriptWorkerError,
    WorkerScriptCancel,
    WorkerScriptStatus,
)
from mindroom.tool_approval import BackgroundScriptToolOrigin
from mindroom.tool_system.worker_routing import (
    build_agent_toolkit_worker_target,
    build_tool_execution_identity,
    serialize_tool_execution_identity,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.workers.backend import WorkerBackend


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
    generation_id: str = "backend-generation-a"
    released: bool = False
    on_release: Callable[[], None] | None = None

    def release(self) -> None:
        self.released = True
        if self.on_release is not None:
            self.on_release()


@dataclass
class _TerminatingWorkerClient:
    """Report one process as running until cancellation confirms its exit."""

    exited: bool = False

    async def status(
        self,
        _worker: WorkerHandle,
        *,
        run_id: str,
        supervisor_handle: str,
    ) -> WorkerScriptStatus:
        del run_id, supervisor_handle
        if self.exited:
            return WorkerScriptStatus(state="exited", exit_code=143)
        return WorkerScriptStatus(state="running")

    async def cancel(
        self,
        _worker: WorkerHandle,
        *,
        run_id: str,
        supervisor_handle: str,
        force: bool = False,
    ) -> WorkerScriptCancel:
        del run_id, supervisor_handle, force
        self.exited = True
        return WorkerScriptCancel(cancel_requested=True, already_finished=False, unknown_handle=False)


@dataclass
class _ApprovalSettlementResolver:
    """Keep broker ownership settlement observable through its durable receipts."""

    settled_runs: list[str] = field(default_factory=list)

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del reason
        self.settled_runs.append(run_id)


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


@pytest.mark.asyncio
async def test_dedicated_workers_require_an_explicit_reachable_gateway(tmp_path: Path) -> None:
    """A dedicated worker must not receive an unreachable primary loopback URL."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_WORKER_BACKEND": "docker",
        },
    )

    with pytest.raises(ValueError, match="MINDROOM_SCRIPT_GATEWAY_URL"):
        await script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
async def test_static_runner_workers_require_an_explicit_reachable_gateway(tmp_path: Path) -> None:
    """A separate static runner must not receive the primary process's loopback URL."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_WORKER_BACKEND": "static_runner",
            "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test",
        },
    )

    with pytest.raises(ValueError, match="MINDROOM_SCRIPT_GATEWAY_URL"):
        await script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment_name", "configured_url"),
    [
        ("MINDROOM_SCRIPT_GATEWAY_URL", "http://127.0.0.1:8765/api/script-gateway"),
        ("MINDROOM_PUBLIC_URL", "http://localhost:8765"),
    ],
)
async def test_worker_gateway_rejects_explicit_loopback_urls(
    tmp_path: Path,
    environment_name: str,
    configured_url: str,
) -> None:
    """An explicit callback URL must still be reachable outside the primary process."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_WORKER_BACKEND": "static_runner",
            "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test",
            environment_name: configured_url,
        },
    )

    with pytest.raises(ValueError, match="non-loopback"):
        await script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_url",
    [
        "http://127.1:8765/api/script-gateway",
        "http://2130706433:8765/api/script-gateway",
        "http://localtest.me:8765/api/script-gateway",
    ],
)
async def test_worker_gateway_rejects_every_hostname_that_resolves_to_loopback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_url: str,
) -> None:
    """Non-canonical IP spellings and DNS aliases cannot disguise primary loopback."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_WORKER_BACKEND": "static_runner",
            "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test",
            "MINDROOM_SCRIPT_GATEWAY_URL": configured_url,
        },
    )
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 8765))])

    with pytest.raises(ValueError, match="non-loopback"):
        await script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
async def test_explicit_gateway_must_be_a_valid_http_url(tmp_path: Path) -> None:
    """Malformed explicit gateway configuration fails before any worker launch."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_WORKER_BACKEND": "static_runner",
            "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test",
            "MINDROOM_SCRIPT_GATEWAY_URL": "not-a-url",
        },
    )

    with pytest.raises(ValueError, match=r"valid HTTP\(S\) URL"):
        await script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment_name", "configured_url"),
    [
        ("MINDROOM_SCRIPT_GATEWAY_URL", "https://gateway.test/api/script-gateway?token=x"),
        ("MINDROOM_SCRIPT_GATEWAY_URL", "https://gateway.test/api/script-gateway#fragment"),
        ("MINDROOM_PUBLIC_URL", "https://gateway.test/base?token=x"),
        ("MINDROOM_PUBLIC_URL", "https://gateway.test/base#fragment"),
    ],
)
async def test_gateway_base_rejects_query_and_fragment_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    configured_url: str,
) -> None:
    """SDK endpoint suffixes must be appended to an unambiguous URL path."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_WORKER_BACKEND": "static_runner",
            "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test",
            environment_name: configured_url,
        },
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("192.0.2.10", 443))],
    )

    with pytest.raises(ValueError, match=r"valid HTTP\(S\) URL"):
        await script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
async def test_worker_gateway_dns_resolution_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker gateway validation cannot resolve DNS on the request loop thread."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_WORKER_BACKEND": "static_runner",
            "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test",
            "MINDROOM_SCRIPT_GATEWAY_URL": "https://gateway.test/api/script-gateway",
        },
    )
    request_loop_thread = threading.get_ident()
    resolver_threads: list[int] = []

    def resolve_gateway(*_args: object, **_kwargs: object) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        resolver_threads.append(threading.get_ident())
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve_gateway)

    gateway_url = await script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104

    assert gateway_url == "https://gateway.test/api/script-gateway"
    assert len(resolver_threads) == 1
    assert resolver_threads[0] != request_loop_thread


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
async def test_removing_script_tool_revokes_and_cancels_running_scripts(tmp_path: Path) -> None:
    """Removing launch authority must also revoke capabilities already in use."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    manager = SimpleNamespace(
        request_revocation=MagicMock(side_effect=request_revocation),
        revoke=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(return_value=run),
    )
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "tools": ["calculator"]}},
        defaults={"tools": []},
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda: None,
    )

    await runtime.apply_update_plan(_plan(old_config, new_config))

    manager.request_revocation.assert_called_once_with(
        run_id=run.run_id,
        reason="Background script tool was removed by configuration reload.",
    )
    assert store.get_run(run.run_id).cancel_requested_at is not None
    manager.revoke.assert_awaited_once_with(
        run.run_id,
        reason="Background script tool was removed by configuration reload.",
    )
    manager.reconcile_durable.assert_awaited_once_with(run_id=run.run_id, broker_revoked=True)


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
    manager.reconcile_durable.assert_awaited_once_with(run_id="run-1", broker_revoked=True)
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
async def test_generation_replacement_interrupts_active_worker_script_before_releasing_old_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing worker identity revokes, closes, and confirms a process before lease replacement."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    store.claim_call(
        run_id=run.run_id,
        call_id="call-1",
        grant=ScriptToolGrant("calculator", "add"),
        arguments_digest="arguments-digest",
    )
    termination_client = _TerminatingWorkerClient()
    settlement_resolver = _ApprovalSettlementResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=settlement_resolver)
    release_observations: list[ScriptRunRecord] = []
    first = _Lease(
        _Backend([_worker(run)]),
        generation_id="backend-generation-a",
        on_release=lambda: release_observations.append(store.get_run(run.run_id)),
    )
    second = _Lease(_Backend([]), generation_id="backend-generation-b")
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=termination_client,  # type: ignore[arg-type]
        worker_backend=first.manager,
        gateway_url="http://primary.test/api/script-gateway",
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "updated", "tools": ["script", "calculator"]}},
        defaults={"tools": []},
    )
    identities: list[str | None] = []

    def worker_identity(_paths: RuntimePaths, config: Config | None) -> str | None:
        identity = "backend-generation-b" if config is new_config else "backend-generation-a"
        identities.append(identity)
        return identity

    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        worker_identity,
        raising=False,
    )
    leases = iter((first, second))
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda: next(leases),
    )

    await runtime.reconcile_once()
    await runtime.apply_update_plan(_plan(old_config, new_config))

    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is not None
    assert durable.cancellation_reason == "Worker configuration changed during configuration reload."
    assert durable.state is ScriptRunState.INTERRUPTED
    assert durable.exit_code == 143
    assert termination_client.exited is True
    assert [call.state.value for call in store.pending_calls(run.run_id)] == []
    assert settlement_resolver.settled_runs == [run.run_id]
    assert release_observations == [durable]
    assert first.released is True
    assert runtime._current_worker_lease is None
    assert identities == ["backend-generation-a", "backend-generation-b"]


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
async def test_committed_worker_lease_wins_over_an_inflight_precommit_refresh(tmp_path: Path) -> None:
    """A slow pre-commit acquisition cannot overwrite the committed current lease."""
    runtime_paths = _runtime_paths(tmp_path)
    first = _Lease(_Backend([]), generation_id="backend-generation-a")
    second = _Lease(_Backend([]), generation_id="backend-generation-b")
    provider_entered = threading.Event()
    release_provider = threading.Event()
    calls = 0

    def provider() -> _Lease:
        nonlocal calls
        calls += 1
        if calls == 1:
            provider_entered.set()
            assert release_provider.wait(timeout=5)
            return first
        return second

    manager = SimpleNamespace(
        worker_backend=None,
        reconcile_durable=AsyncMock(),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=provider,
        pass_timeout_seconds=1,
    )
    old_refresh = asyncio.create_task(runtime.reconcile_once())
    assert await asyncio.to_thread(provider_entered.wait, 1)
    committed_refresh = asyncio.create_task(runtime.install_committed_worker_generation())
    await asyncio.sleep(0)
    release_provider.set()

    await asyncio.gather(old_refresh, committed_refresh)

    assert runtime._worker_backend_for(None) is second.manager
    assert manager.worker_backend is second.manager


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
async def test_blocking_backend_provider_cannot_stall_the_event_loop_past_pass_deadline(tmp_path: Path) -> None:
    """Potentially blocking backend construction is off-loop and bounded by the pass."""
    heartbeat = asyncio.Event()

    def slow_provider() -> None:
        time.sleep(0.2)

    async def beat() -> None:
        await asyncio.sleep(0.01)
        heartbeat.set()

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=_runtime_paths(tmp_path),
        store=ScriptRunStore(_runtime_paths(tmp_path)),
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=AsyncMock()),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=slow_provider,
        pass_timeout_seconds=0.02,
    )
    heartbeat_task = asyncio.create_task(beat())
    started = asyncio.get_running_loop().time()

    await runtime.reconcile_once()

    assert asyncio.get_running_loop().time() - started < 0.1
    await asyncio.wait_for(heartbeat.wait(), timeout=0.05)
    await heartbeat_task


@pytest.mark.asyncio
async def test_timed_out_backend_acquisition_is_reused_instead_of_leaking_its_lease(tmp_path: Path) -> None:
    """A provider thread may finish after timeout, but its lease remains lifecycle-owned."""
    runtime_paths = _runtime_paths(tmp_path)
    release_provider = threading.Event()
    lease = _Lease(_Backend([]))
    calls = 0

    def slow_provider() -> _Lease:
        nonlocal calls
        calls += 1
        assert release_provider.wait(timeout=5)
        return lease

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=AsyncMock()),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=slow_provider,
        pass_timeout_seconds=0.02,
    )
    await runtime.reconcile_once()
    release_provider.set()
    runtime.pass_timeout_seconds = 1

    await runtime.reconcile_once()

    assert calls == 1
    assert runtime._worker_backend_for(None) is lease.manager


@pytest.mark.asyncio
async def test_cancelled_late_backend_build_cannot_publish_after_final_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Final shutdown fences a provider thread even after its asyncio owner is cancelled."""
    workers_runtime_module._reset_primary_worker_manager()
    runtime_paths = _runtime_paths(tmp_path)
    build_started = threading.Event()
    release_build = threading.Event()
    manager_shutdown = threading.Event()

    class _LateManager:
        shutdown_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            manager_shutdown.set()

    late_manager = _LateManager()

    def build_manager(*_args: object, **_kwargs: object) -> WorkerBackend:
        build_started.set()
        assert release_build.wait(timeout=5)
        return cast("WorkerBackend", late_manager)

    monkeypatch.setattr(
        workers_runtime_module,
        "_primary_worker_backend_config_signature",
        lambda *_args, **_kwargs: ("late-generation",),
    )
    monkeypatch.setattr(workers_runtime_module, "_build_primary_worker_manager", build_manager)
    manager = SimpleNamespace(
        gateway_url="",
        worker_backend=None,
        reconcile_durable=AsyncMock(),
        cleanup_snapshot=AsyncMock(return_value=True),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(prune_approvals=AsyncMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lambda: workers_runtime_module.lease_primary_worker_manager(
            runtime_paths,
            proxy_url=None,
            proxy_token=None,
            storage_root=runtime_paths.storage_root,
        ),
        pass_timeout_seconds=0.01,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")

    try:
        await runtime.start()
        assert await asyncio.to_thread(build_started.wait, 1)
        acquisition_task = runtime._pending_worker_lease_task
        assert acquisition_task is not None
        await runtime.shutdown(timeout_seconds=0.01)
        acquisition_task.cancel()
        with suppress(asyncio.CancelledError):
            await acquisition_task

        workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
        release_build.set()

        assert await asyncio.to_thread(manager_shutdown.wait, 1)
        assert workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY is None
        assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES == []
        assert late_manager.shutdown_calls == 1

        workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
        assert late_manager.shutdown_calls == 1
    finally:
        release_build.set()
        with workers_runtime_module._PRIMARY_WORKER_MANAGER_CONDITION:
            active_entry = workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY
            if active_entry is not None:
                active_entry.active_leases = 0
            for retired_entry in workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES:
                retired_entry.active_leases = 0
        workers_runtime_module._reset_primary_worker_manager()


@pytest.mark.asyncio
async def test_cancelled_published_worker_lease_handoff_releases_after_final_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation after publication cannot strand the executor-owned lease delivery."""
    workers_runtime_module._reset_primary_worker_manager()
    runtime_paths = _runtime_paths(tmp_path)
    lease_published = threading.Event()
    release_provider = threading.Event()
    manager_shutdown = threading.Event()

    class _PublishedManager:
        shutdown_calls = 0

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            manager_shutdown.set()

    published_manager = _PublishedManager()

    monkeypatch.setattr(
        workers_runtime_module,
        "_primary_worker_backend_config_signature",
        lambda *_args, **_kwargs: ("published-generation",),
    )
    monkeypatch.setattr(
        workers_runtime_module,
        "_build_primary_worker_manager",
        lambda *_args, **_kwargs: cast("WorkerBackend", published_manager),
    )

    def lease_provider() -> workers_runtime_module.PrimaryWorkerManagerLease:
        lease = workers_runtime_module.lease_primary_worker_manager(
            runtime_paths,
            proxy_url=None,
            proxy_token=None,
            storage_root=runtime_paths.storage_root,
        )
        lease_published.set()
        assert release_provider.wait(timeout=5)
        return lease

    manager = SimpleNamespace(
        gateway_url="",
        worker_backend=None,
        reconcile_durable=AsyncMock(),
        cleanup_snapshot=AsyncMock(return_value=True),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(prune_approvals=AsyncMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lease_provider,
        pass_timeout_seconds=0.01,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")

    try:
        await runtime.start()
        assert await asyncio.to_thread(lease_published.wait, 1)
        acquisition_task = runtime._pending_worker_lease_task
        assert acquisition_task is not None

        workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
        with workers_runtime_module._PRIMARY_WORKER_MANAGER_CONDITION:
            assert workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY is None
            assert len(workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES) == 1
            assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES[0].active_leases == 1

        await runtime.shutdown(timeout_seconds=0.01)
        acquisition_task.cancel()
        with suppress(asyncio.CancelledError):
            await acquisition_task
        release_provider.set()

        assert await asyncio.to_thread(manager_shutdown.wait, 1)
        assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES == []
        assert published_manager.shutdown_calls == 1

        workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
        assert published_manager.shutdown_calls == 1
    finally:
        release_provider.set()
        with workers_runtime_module._PRIMARY_WORKER_MANAGER_CONDITION:
            active_entry = workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY
            if active_entry is not None:
                active_entry.active_leases = 0
            for retired_entry in workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES:
                retired_entry.active_leases = 0
        workers_runtime_module._reset_primary_worker_manager()


@pytest.mark.asyncio
async def test_shutdown_uses_one_deadline_and_retains_late_lease_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reconciliation and lease release share one budget without losing release ownership."""
    release_lease = threading.Event()
    release_started = threading.Event()
    lease_released = threading.Event()
    lease = _Lease(_Backend([]))

    def blocking_release() -> None:
        release_started.set()
        assert release_lease.wait(timeout=1.0)
        lease.released = True
        lease_released.set()

    lease.release = blocking_release  # type: ignore[method-assign]
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=_runtime_paths(tmp_path),
        store=ScriptRunStore(_runtime_paths(tmp_path)),
        broker=SimpleNamespace(_cleanup_tasks=set()),
        manager=SimpleNamespace(worker_backend=None),
        resolver=SimpleNamespace(),
        config_provider=_config,
        worker_lease_provider=lambda: None,
    )
    runtime._activated_once = True
    runtime._current_worker_lease = lease

    reconciliation_started = asyncio.Event()

    async def blocking_complete_pass(_runtime: ScriptRuntimeLifecycle) -> None:
        reconciliation_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ScriptRuntimeLifecycle, "_complete_pass", blocking_complete_pass)
    shutdown = asyncio.create_task(runtime.shutdown(timeout_seconds=0.05))
    try:
        await asyncio.wait_for(reconciliation_started.wait(), timeout=1.0)
        await asyncio.wait_for(shutdown, timeout=1.0)
        assert release_started.is_set()
        assert lease_released.is_set() is False
    finally:
        release_lease.set()

    assert await asyncio.to_thread(lease_released.wait, 1.0)
    assert lease.released is True


@pytest.mark.asyncio
async def test_shutdown_before_activation_releases_committed_worker_lease(tmp_path: Path) -> None:
    """A committed generation must not survive shutdown just because activation never ran."""
    lease = _Lease(_Backend([]))
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=_runtime_paths(tmp_path),
        store=ScriptRunStore(_runtime_paths(tmp_path)),
        broker=SimpleNamespace(_cleanup_tasks=set()),
        manager=SimpleNamespace(worker_backend=lease.manager),
        resolver=SimpleNamespace(),
        config_provider=_config,
        worker_lease_provider=lambda: None,
    )
    runtime._current_worker_lease = lease

    await runtime.shutdown()

    assert lease.released is True
    assert runtime._current_worker_lease is None
    assert runtime.manager.worker_backend is None


@pytest.mark.asyncio
async def test_expired_shutdown_deadline_retains_lease_release_owner() -> None:
    """An exhausted shutdown budget returns without cancelling lease release."""
    release_lease = threading.Event()
    release_started = threading.Event()
    lease_released = threading.Event()
    lease = _Lease(_Backend([]))

    def blocking_release() -> None:
        release_started.set()
        assert release_lease.wait(timeout=1.0)
        lease.released = True
        lease_released.set()

    lease.release = blocking_release  # type: ignore[method-assign]
    await _release_worker_leases_before_deadline(
        [lease],
        deadline=asyncio.get_running_loop().time(),
        timeout_seconds=0.05,
    )
    assert await asyncio.to_thread(release_started.wait, 1.0)
    assert lease_released.is_set() is False

    release_lease.set()
    assert await asyncio.to_thread(lease_released.wait, 1.0)
    assert lease.released is True


@pytest.mark.asyncio
async def test_blocking_retired_lease_release_cannot_stall_reconciliation(tmp_path: Path) -> None:
    """Retired backend disposal remains off-loop and inside the reconciliation deadline."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    first = _Lease(_Backend([_worker(run)]), generation_id="backend-generation-a")
    second = _Lease(_Backend([]), generation_id="backend-generation-b")

    def slow_release() -> None:
        time.sleep(0.2)
        first.released = True

    first.release = slow_release  # type: ignore[method-assign]
    leases = iter((first, second))
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=AsyncMock(return_value=run)),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=_config,
        worker_lease_provider=lambda: next(leases),
        pass_timeout_seconds=0.02,
    )
    await runtime.reconcile_once()
    store.transition_run(run.run_id, state=ScriptRunState.EXITED, exit_code=0)
    started = asyncio.get_running_loop().time()

    await runtime.reconcile_once()

    assert asyncio.get_running_loop().time() - started < 0.1


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

    async def reconcile_durable(*, run_id: str, broker_revoked: bool = False) -> ScriptRunRecord:
        assert broker_revoked is True
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
        pass_timeout_seconds=0.2,
    )

    await runtime.apply_update_plan(_plan(current, Config(defaults={"tools": []})))

    assert store.get_run(first.run_id).cancel_requested_at is not None
    assert store.get_run(second.run_id).cancel_requested_at is not None
    assert set(broker_revocations) == {first.run_id, second.run_id}


@pytest.mark.asyncio
async def test_reload_durable_revocation_is_inside_the_overall_deadline(tmp_path: Path) -> None:
    """Slow SQLite revocations do not escape the pass bound or permit early broker closure."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    _stored_run(store, runtime_paths)
    broker_revocations: list[str] = []

    def slow_request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        time.sleep(0.2)
        return store.request_cancel(run_id, reason=reason)

    async def revoke(run_id: str, *, reason: str) -> ScriptRunRecord:
        del reason
        broker_revocations.append(run_id)
        return store.get_run(run_id)

    current = _config()
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(
            request_revocation=slow_request_revocation,
            revoke=revoke,
            reconcile_durable=AsyncMock(),
        ),
        resolver=SimpleNamespace(resolve=MagicMock()),
        config_provider=lambda: current,
        worker_lease_provider=lambda: None,
        pass_timeout_seconds=0.02,
    )
    started = asyncio.get_running_loop().time()

    await runtime.apply_update_plan(_plan(current, Config(defaults={"tools": []})))

    assert asyncio.get_running_loop().time() - started < 0.1
    assert broker_revocations == []


@pytest.mark.asyncio
async def test_startup_pruning_is_inside_one_complete_pass_deadline(tmp_path: Path) -> None:
    """Startup returns after one deadline even when retention cleanup is unavailable."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    running = _stored_run(store, runtime_paths)
    terminal = store.transition_run(running.run_id, state=ScriptRunState.EXITED, exit_code=0)
    assert terminal.finished_at is not None
    never = asyncio.Event()

    async def prune_approvals(_run_id: str) -> bool:
        await never.wait()
        return False

    manager = SimpleNamespace(
        gateway_url="",
        worker_backend=None,
        reconcile_durable=AsyncMock(),
        cleanup_snapshot=AsyncMock(return_value=True),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(prune_approvals=prune_approvals),
        config_provider=_config,
        worker_lease_provider=lambda: None,
        retention_seconds=0.001,
        pass_timeout_seconds=0.02,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")
    started = asyncio.get_running_loop().time()

    await runtime.start()

    assert asyncio.get_running_loop().time() - started < 0.1
    await runtime.shutdown(timeout_seconds=0.02)


@pytest.mark.asyncio
async def test_pruning_has_an_overall_deadline(tmp_path: Path) -> None:
    """A stuck approval cleanup cannot leave an explicit retention pass unbounded."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    running = _stored_run(store, runtime_paths)
    terminal = store.transition_run(running.run_id, state=ScriptRunState.EXITED, exit_code=0)
    assert terminal.finished_at is not None
    never = asyncio.Event()

    async def prune_approvals(_run_id: str) -> bool:
        await never.wait()
        return False

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(cleanup_snapshot=AsyncMock(return_value=True)),
        resolver=SimpleNamespace(prune_approvals=prune_approvals),
        config_provider=_config,
        worker_lease_provider=lambda: None,
        retention_seconds=0.001,
        pass_timeout_seconds=0.02,
    )
    started = asyncio.get_running_loop().time()

    await runtime.prune_once()

    assert asyncio.get_running_loop().time() - started < 0.1


@pytest.mark.asyncio
async def test_maintenance_retries_after_an_unexpected_cycle_failure(tmp_path: Path) -> None:
    """One unexpected pass failure is logged and the next maintenance interval still runs."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    _stored_run(store, runtime_paths)
    recovered = asyncio.Event()
    calls = 0

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        nonlocal calls
        calls += 1
        if calls == 2:
            message = "unexpected maintenance failure"
            raise RuntimeError(message)
        if calls >= 3:
            recovered.set()
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
        resolver=SimpleNamespace(prune_approvals=AsyncMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lambda: None,
        reconcile_interval_seconds=0.01,
        pass_timeout_seconds=0.05,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")

    await runtime.start()
    await asyncio.wait_for(recovered.wait(), timeout=0.2)
    await runtime.shutdown()

    assert calls >= 3


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
