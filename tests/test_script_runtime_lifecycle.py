"""Tests for process-local background-script runtime lifecycle coordination."""

from __future__ import annotations

import asyncio
import hashlib
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
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import mindroom.workers.runtime as workers_runtime_module
from mindroom.api.script_gateway import bind_script_tool_broker
from mindroom.api.script_gateway import router as script_gateway_router
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.message_target import MessageTarget
from mindroom.orchestration.config_updates import ConfigUpdatePlan, build_config_update_plan
from mindroom.orchestration.script_runtime import (
    ScriptRuntimeLifecycle,
    _LiveScriptRuntimeResolver,
    _release_worker_leases_before_deadline,
    _script_gateway_url,
    _ScriptRuntimeLifecycleError,
    _ScriptRuntimeUnavailableError,
    build_script_runtime,
)
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError, ScriptToolBroker
from mindroom.script_runs.manager import ScriptRunManager, ScriptRunManagerError
from mindroom.script_runs.models import (
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
    script_worker_key_for_run,
)
from mindroom.script_runs.store import (
    ScriptCallNotFoundError,
    ScriptRunNotFoundError,
    ScriptRunStore,
    ScriptRunStoreError,
)
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
    worker_root_path,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle, WorkerSpec
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup

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


async def _reconcile_once(runtime: ScriptRuntimeLifecycle) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(runtime._reconcile_pass(), timeout=runtime.pass_timeout_seconds)


async def _prune_once(runtime: ScriptRuntimeLifecycle, *, now: datetime | None = None) -> None:
    with suppress(TimeoutError):
        await asyncio.wait_for(runtime._prune_pass(now=now), timeout=runtime.pass_timeout_seconds)


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
        worker_backend_locator="locator-a",
        state=state,
    )


@dataclass
class _Backend:
    handles: list[WorkerHandle]
    cleanup_locator: str | None = "locator-a"
    backend_name: str = "test"
    actions: list[str] = field(default_factory=list)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        del include_idle, now
        return list(self.handles)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        del now
        self.actions.append(f"touch:{worker_key}")
        return next((handle for handle in self.handles if handle.worker_key == worker_key), None)

    def retire_worker(self, worker_key: str) -> None:
        self.actions.append(f"retire:{worker_key}")
        self.handles = [handle for handle in self.handles if handle.worker_key != worker_key]


@dataclass
class _Lease:
    manager: WorkerBackend
    released: bool = False
    release_event: threading.Event = field(default_factory=threading.Event)
    on_release: Callable[[], None] | None = None

    def release(self) -> None:
        self.released = True
        self.release_event.set()
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
    ) -> WorkerScriptStatus:
        del run_id
        if self.exited:
            return WorkerScriptStatus(state="exited", exit_code=143)
        return WorkerScriptStatus(state="running")

    async def cancel(
        self,
        _worker: WorkerHandle,
        *,
        run_id: str,
        force: bool = False,
    ) -> WorkerScriptCancel:
        del run_id, force
        self.exited = True
        return WorkerScriptCancel(cancel_requested=True, already_finished=False, unknown_handle=False)


@dataclass
class _TermThenKillWorkerClient:
    """Require both graceful and forced process signals before reporting exit."""

    cancel_forces: list[bool] = field(default_factory=list)
    exited: bool = False

    async def status(
        self,
        _worker: WorkerHandle,
        *,
        run_id: str,
    ) -> WorkerScriptStatus:
        del run_id
        if self.exited:
            return WorkerScriptStatus(state="exited", output="terminated output", exit_code=137)
        return WorkerScriptStatus(state="running")

    async def cancel(
        self,
        _worker: WorkerHandle,
        *,
        run_id: str,
        force: bool = False,
    ) -> WorkerScriptCancel:
        del run_id
        self.cancel_forces.append(force)
        self.exited = force
        return WorkerScriptCancel(cancel_requested=True, already_finished=False, unknown_handle=False)


@dataclass
class _FailingStatusWorkerClient:
    """Accept cancellation but fail the immediate process-status reconciliation."""

    async def status(
        self,
        _worker: WorkerHandle,
        *,
        run_id: str,
    ) -> WorkerScriptStatus:
        del run_id
        message = "worker status unavailable"
        raise ScriptWorkerError(message, failure_kind="worker")

    async def cancel(
        self,
        _worker: WorkerHandle,
        *,
        run_id: str,
        force: bool = False,
    ) -> WorkerScriptCancel:
        del run_id, force
        return WorkerScriptCancel(cancel_requested=True, already_finished=False, unknown_handle=False)


@dataclass
class _BlockingLaunchWorkerClient(_TerminatingWorkerClient):
    """Hold one admitted launch open until the replacement boundary observes it."""

    launch_entered: asyncio.Event = field(default_factory=asyncio.Event)
    release_launch: asyncio.Event = field(default_factory=asyncio.Event)

    async def launch(
        self,
        _worker: WorkerHandle,
        *,
        run_id: str,
        source_digest: str,
        gateway_url: str,
        private_agent_names: tuple[str, ...] | None = None,
    ) -> None:
        del run_id, source_digest, gateway_url, private_agent_names
        self.launch_entered.set()
        await self.release_launch.wait()


@dataclass
class _LaunchingBackend:
    """Provide the shared-state worker required by a real manager launch."""

    runtime_paths: RuntimePaths
    cleanup_locator: str | None = "locator-a"
    backend_name: str = "test"
    handles: list[WorkerHandle] = field(default_factory=list)

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: object | None = None,
    ) -> WorkerHandle:
        del now, progress_sink
        root = worker_root_path(self.runtime_paths.storage_root, spec.worker_key)
        (root / "workspace").mkdir(parents=True, exist_ok=True)
        handle = WorkerHandle(
            worker_id="worker-1",
            worker_key=spec.worker_key,
            endpoint="http://worker.test/api/sandbox-runner/execute",
            auth_token="worker-token",  # noqa: S106
            status="ready",
            backend_name="test",
            last_used_at=1.0,
            created_at=1.0,
            debug_metadata={"state_root": str(root), "api_root": "http://worker.test/api/sandbox-runner"},
        )
        self.handles = [handle]
        return handle

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        del include_idle, now
        return list(self.handles)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        del now
        return next((handle for handle in self.handles if handle.worker_key == worker_key), None)

    def retire_worker(self, worker_key: str) -> None:
        self.handles = [handle for handle in self.handles if handle.worker_key != worker_key]


@dataclass
class _ApprovalSettlementResolver:
    """Keep broker ownership settlement observable through its durable receipts."""

    settled_runs: list[str] = field(default_factory=list)

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del reason
        self.settled_runs.append(run_id)


@dataclass
class _FailingApprovalSettlementResolver:
    """Expose a broker-close failure after durable call ownership is retired."""

    settlement_attempts: list[str] = field(default_factory=list)

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del reason
        self.settlement_attempts.append(run_id)
        msg = "approval settlement unavailable"
        raise _ScriptRuntimeUnavailableError(msg)


@dataclass
class _HangingApprovalSettlementResolver:
    """Hold broker cleanup open until process reconciliation is observable."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del run_id, reason
        self.entered.set()
        await self.release.wait()


@dataclass
class _UnexpectedFailingApprovalSettlementResolver:
    """Raise outside the lifecycle's historical broker exception allowlist."""

    settlement_attempts: int = 0

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del run_id, reason
        self.settlement_attempts += 1
        msg = "unexpected approval settlement failure"
        raise RuntimeError(msg)


@dataclass
class _TransientApprovalSettlementResolver:
    """Fail the first broker obligation and settle its retry."""

    settlement_attempts: int = 0

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del run_id, reason
        self.settlement_attempts += 1
        if self.settlement_attempts == 1:
            msg = "transient approval settlement failure"
            raise RuntimeError(msg)


@dataclass
class _StartupAdmissionResolver:
    """Expose any inherited call that reaches execution during startup cleanup."""

    execution_attempts: int = 0

    def is_authorized(self, run: ScriptRunRecord, *, config: Config | None = None) -> bool:
        del run, config
        return True

    def resolve(self, run: ScriptRunRecord, *, correlation_id: str) -> None:
        del run, correlation_id
        self.execution_attempts += 1
        message = "inherited call reached execution"
        raise RuntimeError(message)

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del run_id, reason

    async def prune_approvals(self, run_id: str) -> bool:
        del run_id
        return True


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


def _broker_lifecycle_stub() -> SimpleNamespace:
    return SimpleNamespace(
        _cleanup_tasks=set(),
        close_call_admission=MagicMock(),
        open_call_admission=MagicMock(),
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


def _stored_run_pinned_to_worker(
    store: ScriptRunStore,
    runtime_paths: RuntimePaths,
    *,
    run_id: str,
    worker_backend_locator: str = "locator-a",
) -> ScriptRunRecord:
    run = _run(runtime_paths, run_id=run_id, state=ScriptRunState.STARTING)
    assert run.worker_key is not None
    created = store.create_run(
        replace(
            run,
            worker_key=script_worker_key_for_run(run.worker_key, run_id),
            worker_backend_locator=worker_backend_locator,
        ),
    )
    return store.transition_run(
        created.run_id,
        state=ScriptRunState.RUNNING,
        worker_id="worker-1",
    )


def _worker_replacement_scenario(
    tmp_path: Path,
    *,
    run_id: str,
    approval_resolver: object,
) -> tuple[
    ScriptRuntimeLifecycle,
    ScriptRunStore,
    ScriptRunRecord,
    _Lease,
    _TermThenKillWorkerClient,
    Config,
    Config,
]:
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run_pinned_to_worker(store, runtime_paths, run_id=run_id)
    store.claim_call(
        run_id=run.run_id,
        call_id="call-1",
        grant=ScriptToolGrant("calculator", "add"),
        arguments_digest="arguments-digest",
    )
    broker = ScriptToolBroker(store=store, runtime_resolver=approval_resolver)  # type: ignore[arg-type]
    lease = _Lease(_Backend([_worker(run)]))
    worker_client = _TermThenKillWorkerClient()
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=worker_client,  # type: ignore[arg-type]
        worker_backend=lease.manager,
        gateway_url="http://primary.test/api/script-gateway",
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "updated", "tools": ["script"]}},
        defaults={"tools": []},
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._current_worker_lease = lease
    return runtime, store, run, lease, worker_client, old_config, new_config


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
            worker_lease_provider=lambda _locator: None,
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
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
@pytest.mark.parametrize("execution_mode", ["off", "local", "disabled"])
async def test_explicit_local_script_mode_uses_embedded_gateway_with_dedicated_backend(
    tmp_path: Path,
    backend: str,
    execution_mode: str,
) -> None:
    """An explicitly unsafe-local script mode takes precedence over backend selection."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={
            "MINDROOM_SANDBOX_EXECUTION_MODE": execution_mode,
            "MINDROOM_WORKER_BACKEND": backend,
        },
    )

    gateway_url = await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104

    assert gateway_url == "http://127.0.0.1:8765/api/script-gateway"


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["::", "::1"])
async def test_explicit_local_script_mode_uses_reachable_ipv6_loopback_gateway(
    tmp_path: Path,
    host: str,
) -> None:
    """An IPv6 listener publishes a bracketed loopback URL for its local script process."""
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env={"MINDROOM_SANDBOX_EXECUTION_MODE": "local"},
    )

    gateway_url = await _script_gateway_url(runtime_paths, host=host, port=8765)

    assert gateway_url == "http://[::1]:8765/api/script-gateway"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
@pytest.mark.parametrize("execution_mode", ["all", "sandbox_all", None])
async def test_effective_worker_script_mode_requires_reachable_gateway_with_dedicated_backend(
    tmp_path: Path,
    backend: str,
    execution_mode: str | None,
) -> None:
    """Explicit worker modes and implicit dedicated backends require a reachable gateway."""
    process_env = {"MINDROOM_WORKER_BACKEND": backend}
    if execution_mode is not None:
        process_env["MINDROOM_SANDBOX_EXECUTION_MODE"] = execution_mode
    runtime_paths = replace(
        _runtime_paths(tmp_path),
        process_env=process_env,
    )

    with pytest.raises(ValueError, match="MINDROOM_SCRIPT_GATEWAY_URL"):
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


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
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


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
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


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
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


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
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


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
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


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

    gateway_url = await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104

    assert gateway_url == "https://gateway.test/api/script-gateway"
    assert len(resolver_threads) == 1
    assert resolver_threads[0] != request_loop_thread


@pytest.mark.asyncio
async def test_lifecycle_activates_after_both_agent_registry_and_api_are_ready(tmp_path: Path) -> None:
    """Activation waits for both composition roots and shutdown clears the binding."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = SimpleNamespace(
        begin_shutdown=AsyncMock(),
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        gateway_url="",
        worker_backend=MagicMock(),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
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
async def test_startup_revokes_and_retires_inherited_running_process(tmp_path: Path) -> None:
    """A new application process never adopts an inherited running script."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run_pinned_to_worker(store, runtime_paths, run_id=f"script-{'9' * 32}")
    worker_client = _TerminatingWorkerClient()
    broker = ScriptToolBroker(store=store, runtime_resolver=_ApprovalSettlementResolver())
    backend = _Backend([_worker(run)])
    lease = _Lease(backend)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=worker_client,  # type: ignore[arg-type]
        worker_backend=backend,
        gateway_url="http://primary.test/api/script-gateway",
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: lease,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")

    try:
        await runtime.start()

        inherited = store.get_run(run.run_id)
        assert inherited.cancel_requested_at is not None
        assert inherited.state is ScriptRunState.INTERRUPTED
        assert worker_client.exited is True
        assert backend.actions[-1] == f"retire:{run.worker_key}"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_fails_closed_when_the_durable_backend_is_no_longer_configured(tmp_path: Path) -> None:
    """An offline backend change leaves the revoked run visible for operator cleanup."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run_pinned_to_worker(store, runtime_paths, run_id=f"script-{'7' * 32}")
    current_backend = _Backend([], cleanup_locator="locator-b")
    current_lease = _Lease(current_backend)
    requested_locators: list[str | None] = []

    def lease_provider(required_locator: str | None = None) -> _Lease | None:
        requested_locators.append(required_locator)
        if required_locator is None:
            return current_lease
        return None

    worker_client = _TerminatingWorkerClient()
    broker = ScriptToolBroker(store=store, runtime_resolver=_ApprovalSettlementResolver())
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=worker_client,  # type: ignore[arg-type]
        worker_backend=current_backend,
        gateway_url="http://primary.test/api/script-gateway",
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lease_provider,  # type: ignore[arg-type]
    )
    runtime.bind_api("http://primary.test/api/script-gateway")

    try:
        await runtime.start()

        durable = store.get_run(run.run_id)
        assert durable.cancel_requested_at is not None
        assert durable.state is ScriptRunState.RUNNING
        assert durable.finished_at is None
        assert worker_client.exited is False
        assert current_backend.actions == []
        assert requested_locators == ["locator-a"]
        assert broker._call_admission_open.is_set() is False
    finally:
        await runtime.shutdown()


def test_keepalive_touches_only_runs_owned_by_the_supplied_backend(tmp_path: Path) -> None:
    """Idle cleanup must not be defeated for a durable run owned by a different backend."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    owned = _stored_run_pinned_to_worker(
        store,
        runtime_paths,
        run_id=f"script-{'1' * 32}",
        worker_backend_locator="locator-a",
    )
    disowned = _stored_run_pinned_to_worker(
        store,
        runtime_paths,
        run_id=f"script-{'2' * 32}",
        worker_backend_locator="locator-b",
    )
    backend = _Backend([_worker(owned), _worker(disowned)], cleanup_locator="locator-a")
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=_broker_lifecycle_stub(),  # type: ignore[arg-type]
        manager=MagicMock(spec=ScriptRunManager),
        resolver=MagicMock(),
        config_provider=lambda: None,
        worker_lease_provider=lambda _locator: None,
    )

    runtime.touch_live_workers(backend)

    assert backend.actions == [f"touch:{owned.worker_key}"]


@pytest.mark.asyncio
async def test_no_api_startup_still_interrupts_and_retires_inherited_worker_run(tmp_path: Path) -> None:
    """Disabling HTTP exposure must not skip fail-closed inherited-run cleanup."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run_pinned_to_worker(store, runtime_paths, run_id=f"script-{'5' * 32}")
    backend = _Backend([_worker(run)])
    lease = _Lease(backend)
    worker_client = _TerminatingWorkerClient()
    broker = ScriptToolBroker(store=store, runtime_resolver=_ApprovalSettlementResolver())
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=worker_client,  # type: ignore[arg-type]
        worker_backend=None,
        gateway_url="",
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda locator: lease if locator == "locator-a" else None,
        api_enabled=False,
    )

    try:
        await runtime.start()

        durable = store.get_run(run.run_id)
        assert durable.state is ScriptRunState.INTERRUPTED
        assert backend.actions == [f"retire:{run.worker_key}"]
        assert manager.gateway_url == ""
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_revokes_inherited_authority_before_blocked_backend_acquisition(
    tmp_path: Path,
) -> None:
    """A reachable gateway cannot claim inherited work while startup waits for its backend."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    token = "secret-token"  # noqa: S105
    run_id = f"script-{'8' * 32}"
    template = _run(runtime_paths, run_id=run_id, state=ScriptRunState.STARTING)
    assert template.worker_key is not None
    created = store.create_run(
        replace(
            template,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            worker_key=script_worker_key_for_run(template.worker_key, run_id),
        ),
    )
    inherited = store.transition_run(created.run_id, state=ScriptRunState.RUNNING, worker_id="worker-1")
    resolver = _StartupAdmissionResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)  # type: ignore[arg-type]

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    manager = SimpleNamespace(
        begin_shutdown=AsyncMock(),
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        gateway_url="",
        worker_backend=None,
        request_revocation=request_revocation,
        revoke=AsyncMock(side_effect=lambda run_id, **_kwargs: store.get_run(run_id)),
        reconcile_revoked_process=AsyncMock(side_effect=lambda run_id: store.get_run(run_id)),
        reconcile_durable=AsyncMock(side_effect=lambda run_id, **_kwargs: store.get_run(run_id)),
    )
    acquisition_started = threading.Event()
    release_acquisition = threading.Event()

    def acquire_worker_backend(_locator: str | None) -> None:
        acquisition_started.set()
        assert release_acquisition.wait(timeout=5)

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,  # type: ignore[arg-type]
        config_provider=_config,
        worker_lease_provider=acquire_worker_backend,  # type: ignore[arg-type]
    )
    app = FastAPI()
    app.include_router(script_gateway_router)
    bind_script_tool_broker(app, broker)
    runtime.bind_api("http://primary.test/api/script-gateway")
    startup = asyncio.create_task(runtime.start())

    try:
        assert await asyncio.to_thread(acquisition_started.wait, 5)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/script-gateway/calls",
                json={
                    "run_id": inherited.run_id,
                    "call_id": "startup-call",
                    "toolkit_name": "calculator",
                    "function_name": "add",
                    "arguments": {"a": 1, "b": 2},
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 404
        with pytest.raises(ScriptCallNotFoundError):
            store.get_call(inherited.run_id, "startup-call")
        assert resolver.execution_attempts == 0
        assert store.get_run(inherited.run_id).cancel_requested_at is not None
    finally:
        release_acquisition.set()
        await asyncio.wait_for(startup, timeout=1)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_removed_agent_revokes_and_cancels_running_scripts(tmp_path: Path) -> None:
    """Removal records revocation before unavailable live runtime resolution."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    manager = SimpleNamespace(
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        request_revocation=MagicMock(side_effect=request_revocation),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
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
        worker_lease_provider=lambda _locator: None,
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
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        request_revocation=MagicMock(side_effect=request_revocation),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
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
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda _locator: None,
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
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        request_revocation=MagicMock(return_value=run),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
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
        worker_lease_provider=lambda _locator: None,
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
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda _locator: None,
    )
    changed = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "A changed role", "tools": ["script"]}},
        defaults={"tools": []},
    )

    await runtime.apply_update_plan(_plan(old_config, changed))

    manager.request_revocation.assert_not_called()
    assert store.get_run("run-1").state is ScriptRunState.RUNNING


@pytest.mark.asyncio
async def test_plugin_reload_interrupts_running_scripts_before_code_replacement(tmp_path: Path) -> None:
    """Plugin replacement uses a clear interruption boundary instead of stale tool objects."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    def finish_interrupted(*, run_id: str, broker_revoked: bool) -> ScriptRunRecord:
        del broker_revoked
        return store.transition_run(
            run_id,
            state=ScriptRunState.INTERRUPTED,
            error="Plugin tools changed during configuration reload.",
        )

    manager = SimpleNamespace(
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        request_revocation=MagicMock(side_effect=request_revocation),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(side_effect=finish_interrupted),
    )
    config = _config()
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: config,
        worker_lease_provider=lambda _locator: None,
    )

    await runtime.apply_update_plan(_plan(config, config), plugins_changed=True)

    manager.request_revocation.assert_called_once_with(
        run_id=run.run_id,
        reason="Plugin tools changed during configuration reload.",
    )
    manager.revoke.assert_awaited_once_with(
        run.run_id,
        reason="Plugin tools changed during configuration reload.",
    )
    manager.reconcile_durable.assert_awaited_once_with(run_id=run.run_id, broker_revoked=True)


@pytest.mark.asyncio
async def test_plugin_reload_fails_closed_while_any_script_remains_unfinished(tmp_path: Path) -> None:
    """Plugin code cannot be replaced while an old script can still call it."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    config = _config()
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(
            begin_startup_reconciliation=AsyncMock(),
            end_startup_reconciliation=AsyncMock(),
            request_revocation=MagicMock(
                side_effect=lambda run_id, *, reason: store.request_cancel(run_id, reason=reason),
            ),
            revoke=AsyncMock(return_value=run),
            reconcile_revoked_process=AsyncMock(return_value=run),
            reconcile_durable=AsyncMock(return_value=run),
        ),
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: config,
        worker_lease_provider=lambda _locator: None,
    )

    with pytest.raises(RuntimeError, match="Plugin reload did not interrupt every active background script"):
        await runtime.apply_update_plan(_plan(config, config), plugins_changed=True)


@pytest.mark.asyncio
async def test_reload_releases_only_its_fence_while_startup_cleanup_remains_pending(tmp_path: Path) -> None:
    """Reload completion releases its counted owner without releasing startup's owner."""
    runtime_paths = _runtime_paths(tmp_path)
    reconciliation_owners = 1

    async def begin_startup_reconciliation() -> None:
        nonlocal reconciliation_owners
        reconciliation_owners += 1

    async def end_startup_reconciliation() -> None:
        nonlocal reconciliation_owners
        reconciliation_owners -= 1

    manager = SimpleNamespace(
        begin_startup_reconciliation=begin_startup_reconciliation,
        end_startup_reconciliation=end_startup_reconciliation,
        worker_replacement_in_progress=False,
    )
    config = _config()
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._startup_cleanup_pending = True

    await runtime.apply_update_plan(_plan(config, config), plugins_changed=True)
    await runtime.complete_worker_replacement()

    assert reconciliation_owners == 1
    await manager.end_startup_reconciliation()
    assert reconciliation_owners == 0


def test_live_resolver_uses_current_reply_membership_authorization(tmp_path: Path) -> None:
    """Run authority includes the bot's current grant-room membership index."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config(
        agents={"watcher": {"display_name": "Watcher", "tools": ["script"], "rooms": ["trusted"]}},
        defaults={"tools": []},
        authorization={
            "default_room_access": True,
            "agent_reply_permissions": {
                "watcher": {"users": [], "joined_rooms": ["trusted"]},
            },
        },
    )
    memberships = MagicMock()
    memberships.is_allowed.return_value = False
    bot = SimpleNamespace(
        running=True,
        config=config,
        _runtime_view=SimpleNamespace(agent_reply_memberships=memberships),
    )
    resolver = _LiveScriptRuntimeResolver(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: cast("AgentBot", bot),
        worker_backend_provider=lambda _run: None,
    )

    assert resolver.is_authorized(_run(runtime_paths)) is False
    memberships.is_allowed.return_value = True
    assert resolver.is_authorized(_run(runtime_paths)) is True
    bot.running = False
    assert resolver.is_authorized(_run(runtime_paths)) is None


@pytest.mark.asyncio
async def test_maintenance_interrupts_run_after_live_authorization_loss(tmp_path: Path) -> None:
    """Membership loss revokes and interrupts a run during the next maintenance pass."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    async def reconcile_durable(*, run_id: str, broker_revoked: bool = False) -> ScriptRunRecord:
        assert broker_revoked is True
        return store.transition_run(run_id, state=ScriptRunState.INTERRUPTED)

    manager = SimpleNamespace(
        worker_backend=None,
        request_revocation=MagicMock(side_effect=request_revocation),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(side_effect=reconcile_durable),
    )
    resolver = SimpleNamespace(is_authorized=MagicMock(return_value=False))
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=resolver,
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )

    await _reconcile_once(runtime)

    manager.request_revocation.assert_called_once()
    manager.revoke.assert_awaited_once()
    assert store.get_run(run.run_id).state is ScriptRunState.INTERRUPTED


@pytest.mark.asyncio
async def test_maintenance_interrupts_run_when_its_agent_was_removed(tmp_path: Path) -> None:
    """A removed agent is durable revocation, not transient bot unavailability."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    async def reconcile_durable(*, run_id: str, broker_revoked: bool = False) -> ScriptRunRecord:
        assert broker_revoked is True
        return store.transition_run(run_id, state=ScriptRunState.INTERRUPTED)

    manager = SimpleNamespace(
        worker_backend=None,
        request_revocation=MagicMock(side_effect=request_revocation),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(side_effect=reconcile_durable),
    )
    resolver = SimpleNamespace(is_authorized=MagicMock(return_value=None))
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=resolver,
        config_provider=lambda: Config(agents={}, defaults={"tools": []}),
        worker_lease_provider=lambda _locator: None,
    )

    await _reconcile_once(runtime)

    manager.request_revocation.assert_called_once()
    assert store.get_run(run.run_id).state is ScriptRunState.INTERRUPTED


@pytest.mark.asyncio
async def test_bot_unavailability_keeps_run_retryable_while_broker_fails_closed(tmp_path: Path) -> None:
    """A transient bot restart is not denial, but it cannot authorize a broker call."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    token = "secret-token"  # noqa: S105
    run = _run(runtime_paths, state=ScriptRunState.STARTING)
    created = store.create_run(replace(run, token_hash=hashlib.sha256(token.encode()).hexdigest()))
    running = store.transition_run(created.run_id, state=ScriptRunState.RUNNING, worker_id="worker-1")
    resolver = SimpleNamespace(is_authorized=MagicMock(return_value=None))

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    manager = SimpleNamespace(
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        request_revocation=MagicMock(side_effect=request_revocation),
        revoke=AsyncMock(return_value=running),
        reconcile_durable=AsyncMock(return_value=running),
    )
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)  # type: ignore[arg-type]
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )

    await _reconcile_once(runtime)

    durable = store.get_run(running.run_id)
    assert durable.cancel_requested_at is None
    manager.request_revocation.assert_not_called()
    manager.reconcile_durable.assert_awaited_once_with(run_id=running.run_id)
    with pytest.raises(ScriptRuntimeUnavailableError, match="temporarily unavailable"):
        broker.authenticate(running.run_id, f"Bearer {token}")


@pytest.mark.parametrize("authorization_result", [False, None])
@pytest.mark.asyncio
async def test_authorization_config_change_interrupts_unconfirmed_owner_before_commit(
    tmp_path: Path,
    *,
    authorization_result: bool | None,
) -> None:
    """Config publication requires confirmed continued authority for every active run."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    current = Config(
        agents={"watcher": {"display_name": "Watcher", "tools": ["script"]}},
        defaults={"tools": []},
        authorization={"default_room_access": True},
    )
    updated = current.model_copy(
        update={"authorization": current.authorization.model_copy(update={"default_room_access": False})},
    )

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        return store.request_cancel(run_id, reason=reason)

    manager = SimpleNamespace(
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        request_revocation=MagicMock(side_effect=request_revocation),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(return_value=run),
    )
    resolver = SimpleNamespace(is_authorized=MagicMock(return_value=authorization_result))
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=resolver,
        config_provider=lambda: current,
        worker_lease_provider=lambda _locator: None,
    )

    await runtime.apply_update_plan(_plan(current, updated))

    resolver.is_authorized.assert_called_once_with(run, config=updated)
    assert store.get_run(run.run_id).cancel_requested_at is not None


@pytest.mark.asyncio
async def test_generation_replacement_interrupts_active_worker_script_before_releasing_old_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changing worker identity revokes, closes, and confirms a process before lease replacement."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run_pinned_to_worker(store, runtime_paths, run_id=f"script-{'a' * 32}")
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
    old_backend = _Backend([_worker(run)])
    first = _Lease(
        old_backend,
        on_release=lambda: (
            old_backend.actions.append("release"),
            release_observations.append(store.get_run(run.run_id)),
        ),
    )
    second = _Lease(_Backend([]))
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
    committed_config = old_config
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
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: committed_config,
        worker_lease_provider=lambda _locator: next(leases),
    )

    await _reconcile_once(runtime)
    await runtime.apply_update_plan(_plan(old_config, new_config))

    assert first.released is True
    assert runtime._current_worker_lease is None

    committed_config = new_config
    await runtime.complete_worker_replacement()

    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is not None
    assert durable.cancellation_reason == "Worker configuration changed during configuration reload."
    assert durable.state is ScriptRunState.INTERRUPTED
    assert durable.exit_code == 143
    assert termination_client.exited is True
    assert [call.state.value for call in store.pending_calls(run.run_id)] == []
    assert settlement_resolver.settled_runs == [run.run_id]
    assert release_observations == [durable]
    assert old_backend.actions[-2:] == [f"retire:{run.worker_key}", "release"]
    assert runtime._current_worker_lease is second
    assert manager._worker_replacement_in_progress is False
    assert identities == ["backend-generation-a", "backend-generation-b"]


@pytest.mark.asyncio
async def test_generation_replacement_aborts_before_every_run_has_durable_revocation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed durable write cannot let the caller commit an update with an unrevoked run."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    first = _stored_run(store, runtime_paths)
    second = _stored_run(store, runtime_paths, run_id="run-2")

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        if run_id == first.run_id:
            msg = "durable store unavailable"
            raise ScriptRunManagerError(msg)
        return store.request_cancel(run_id, reason=reason)

    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "updated", "tools": ["script", "calculator"]}},
        defaults={"tools": []},
    )
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        lambda _paths, config: "next" if config is new_config else "current",
    )
    manager = SimpleNamespace(
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        request_revocation=request_revocation,
        revoke=AsyncMock(),
        reconcile_durable=AsyncMock(),
        begin_worker_replacement=AsyncMock(),
        end_worker_replacement=AsyncMock(),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda _locator: None,
        pass_concurrency=1,
    )

    with pytest.raises(RuntimeError, match="Worker replacement did not durably revoke every active run"):
        await runtime.apply_update_plan(_plan(old_config, new_config))

    assert store.get_run(first.run_id).cancel_requested_at is None
    assert store.get_run(second.run_id).cancel_requested_at is not None


@pytest.mark.asyncio
async def test_generation_replacement_aborts_when_broker_ownership_cannot_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A broker-close failure cannot publish a replacement over a revoked live process."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run_pinned_to_worker(store, runtime_paths, run_id=f"script-{'d' * 32}")
    store.claim_call(
        run_id=run.run_id,
        call_id="call-1",
        grant=ScriptToolGrant("calculator", "add"),
        arguments_digest="arguments-digest",
    )
    resolver = _FailingApprovalSettlementResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    lease = _Lease(_Backend([_worker(run)]))
    worker_client = _TermThenKillWorkerClient()
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=worker_client,  # type: ignore[arg-type]
        worker_backend=lease.manager,
        gateway_url="http://primary.test/api/script-gateway",
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "updated", "tools": ["script"]}},
        defaults={"tools": []},
    )
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        lambda _paths, config: "new" if config is new_config else "old",
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._current_worker_lease = lease

    with pytest.raises(_ScriptRuntimeLifecycleError, match="broker ownership"):
        await runtime.apply_update_plan(_plan(old_config, new_config))

    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is not None
    assert durable.state is ScriptRunState.RUNNING
    assert durable.finished_at is not None
    assert durable.exit_code == 137
    assert durable.output == "terminated output"
    assert worker_client.cancel_forces == [False, True]
    assert store.pending_calls(run.run_id) == []
    assert resolver.settlement_attempts == [run.run_id, run.run_id]
    assert runtime._current_worker_lease is lease
    assert lease.released is False
    assert manager.worker_replacement_in_progress is False


@pytest.mark.asyncio
async def test_hung_broker_cleanup_cannot_delay_process_exit_recording(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A broker obligation may hang while TERM/KILL and durable process truth complete."""
    resolver = _HangingApprovalSettlementResolver()
    runtime, store, run, lease, worker_client, old_config, new_config = _worker_replacement_scenario(
        tmp_path,
        run_id=f"script-{'1' * 32}",
        approval_resolver=resolver,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        lambda _paths, config: "new" if config is new_config else "old",
    )
    exit_recorded = threading.Event()
    record_process_exit = store.record_process_exit

    def record_process_exit_then_notify(*args: object, **kwargs: object) -> ScriptRunRecord:
        recorded = record_process_exit(*args, **kwargs)  # type: ignore[arg-type]
        exit_recorded.set()
        return recorded

    monkeypatch.setattr(store, "record_process_exit", record_process_exit_then_notify)
    update = asyncio.create_task(runtime.apply_update_plan(_plan(old_config, new_config)))

    try:
        await asyncio.wait_for(resolver.entered.wait(), timeout=1)
        assert await asyncio.to_thread(exit_recorded.wait, 1)
        durable = store.get_run(run.run_id)
        assert durable.state is ScriptRunState.RUNNING
        assert durable.finished_at is not None
        assert durable.output == "terminated output"
        assert worker_client.cancel_forces == [False, True]
        assert lease.manager.actions == []
        assert update.done() is False
    finally:
        resolver.release.set()
        await asyncio.wait_for(update, timeout=1)


@pytest.mark.asyncio
async def test_unexpected_broker_failure_still_records_process_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unexpected broker exception cannot bypass TERM/KILL and durable exit evidence."""
    resolver = _UnexpectedFailingApprovalSettlementResolver()
    runtime, store, run, lease, worker_client, old_config, new_config = _worker_replacement_scenario(
        tmp_path,
        run_id=f"script-{'2' * 32}",
        approval_resolver=resolver,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        lambda _paths, config: "new" if config is new_config else "old",
    )

    with pytest.raises(_ScriptRuntimeLifecycleError, match="broker ownership"):
        await runtime.apply_update_plan(_plan(old_config, new_config))

    durable = store.get_run(run.run_id)
    assert durable.finished_at is not None
    assert durable.output == "terminated output"
    assert worker_client.cancel_forces == [False, True]
    assert resolver.settlement_attempts == 2
    assert lease.released is False


@pytest.mark.asyncio
async def test_transient_broker_failure_uses_successful_retry_for_worker_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful current-state broker retry permits cleanup and worker replacement."""
    resolver = _TransientApprovalSettlementResolver()
    runtime, store, run, lease, worker_client, old_config, new_config = _worker_replacement_scenario(
        tmp_path,
        run_id=f"script-{'3' * 32}",
        approval_resolver=resolver,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        lambda _paths, config: "new" if config is new_config else "old",
    )

    await runtime.apply_update_plan(_plan(old_config, new_config))

    durable = store.get_run(run.run_id)
    assert durable.state is ScriptRunState.INTERRUPTED
    assert durable.output == "terminated output"
    assert worker_client.cancel_forces == [False, True]
    assert resolver.settlement_attempts == 2
    assert lease.released is True


@pytest.mark.asyncio
async def test_generation_replacement_aborts_when_process_reconciliation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An immediate worker-status failure cannot let the replacement commit."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths, run_id=f"script-{'b' * 32}")
    resolver = _ApprovalSettlementResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    lease = _Lease(_Backend([_worker(run)]))
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=_FailingStatusWorkerClient(),  # type: ignore[arg-type]
        worker_backend=lease.manager,
        gateway_url="http://primary.test/api/script-gateway",
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "updated", "tools": ["script"]}},
        defaults={"tools": []},
    )
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        lambda _paths, config: "new" if config is new_config else "old",
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._current_worker_lease = lease

    with pytest.raises(_ScriptRuntimeLifecycleError, match="process reconciliation"):
        await runtime.apply_update_plan(_plan(old_config, new_config))

    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is not None
    assert durable.state is ScriptRunState.RUNNING
    assert resolver.settled_runs == [run.run_id]
    assert runtime._current_worker_lease is lease
    assert lease.released is False
    assert manager.worker_replacement_in_progress is False


@pytest.mark.asyncio
async def test_generation_replacement_aborts_when_reconciliation_leaves_a_worker_run_unfinished(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful reconciliation call must still publish a terminal durable worker state."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run_pinned_to_worker(store, runtime_paths, run_id=f"script-{'4' * 32}")
    resolver = _ApprovalSettlementResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    lease = _Lease(_Backend([_worker(run)]))
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=_TerminatingWorkerClient(),  # type: ignore[arg-type]
        worker_backend=lease.manager,
        gateway_url="http://primary.test/api/script-gateway",
    )

    async def leave_unfinished(
        _manager: ScriptRunManager,
        *,
        run_id: str,
        broker_revoked: bool = False,
    ) -> ScriptRunRecord:
        del broker_revoked
        return store.get_run(run_id)

    monkeypatch.setattr(ScriptRunManager, "reconcile_durable", leave_unfinished)
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "updated", "tools": ["script"]}},
        defaults={"tools": []},
    )
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        lambda _paths, config: "new" if config is new_config else "old",
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._current_worker_lease = lease

    with pytest.raises(_ScriptRuntimeLifecycleError, match="terminal durable state"):
        await runtime.apply_update_plan(_plan(old_config, new_config))

    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is not None
    assert durable.state is ScriptRunState.RUNNING
    assert resolver.settled_runs == [run.run_id]
    assert runtime._current_worker_lease is lease
    assert lease.released is False
    assert manager.worker_replacement_in_progress is False


@pytest.mark.asyncio
async def test_generation_replacement_drains_an_admitted_launch_before_snapshotting_affected_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The lifecycle drains a real launch, then revokes the run it made durable."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "updated", "tools": ["script", "calculator"]}},
        defaults={"tools": []},
    )
    committed_config = old_config
    backend = _LaunchingBackend(runtime_paths)
    client = _BlockingLaunchWorkerClient()
    settlement_resolver = _ApprovalSettlementResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=settlement_resolver)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=client,  # type: ignore[arg-type]
        worker_backend=backend,
        gateway_url="http://primary.test/api/script-gateway",
        grant_resolver=lambda _context: (ScriptToolGrant("calculator", "add"),),
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    lease = _Lease(backend)
    backend_available = True
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        lambda _paths, config: "new" if config is new_config else "old",
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: committed_config,
        worker_lease_provider=lambda _locator: lease if backend_available else None,
    )
    context = make_test_tool_runtime_context(
        agent_name="watcher",
        target=MessageTarget.resolve(
            room_id="!room:example.test",
            thread_id="$thread:example.test",
            reply_to_event_id=None,
        ),
        requester_id="@alice:example.test",
        client=SimpleNamespace(),
        config=old_config,
        runtime_paths=runtime_paths,
        storage_path=runtime_paths.storage_root,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    await _reconcile_once(runtime)

    admitted_launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    await asyncio.wait_for(client.launch_entered.wait(), timeout=1)
    replacement = asyncio.create_task(runtime.apply_update_plan(_plan(old_config, new_config)))
    await asyncio.sleep(0)

    assert replacement.done() is False
    with pytest.raises(ScriptRunManagerError, match="runtime reconciliation is in progress"):
        await manager.run(context, source="print('blocked')\n")

    client.release_launch.set()
    run = await admitted_launch
    await replacement

    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is not None
    assert durable.state is ScriptRunState.INTERRUPTED
    assert settlement_resolver.settled_runs == [run.run_id]
    assert manager._worker_replacement_in_progress is True

    backend_available = False
    await runtime.complete_worker_replacement()
    assert manager._worker_replacement_in_progress is False

    with pytest.raises(ScriptRunManagerError, match="worker backend is unavailable"):
        await manager.run(context, source="print('unavailable')\n")
    assert [stored.run_id for stored in store.list_runs()] == [run.run_id]


@pytest.mark.asyncio
async def test_script_tool_removal_drains_a_launch_admitted_before_durable_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A revoking update cannot miss a launch admitted just before its durable row exists."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "tools": ["calculator"]}},
        defaults={"tools": []},
    )
    backend = _LaunchingBackend(runtime_paths)
    client = _BlockingLaunchWorkerClient()
    client.release_launch.set()
    settlement_resolver = _ApprovalSettlementResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=settlement_resolver)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=client,  # type: ignore[arg-type]
        worker_backend=backend,
        gateway_url="http://primary.test/api/script-gateway",
        grant_resolver=lambda _context: (ScriptToolGrant("calculator", "add"),),
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: old_config,
        worker_lease_provider=lambda _locator: None,
    )
    context = make_test_tool_runtime_context(
        agent_name="watcher",
        target=MessageTarget.resolve(
            room_id="!room:example.test",
            thread_id="$thread:example.test",
            reply_to_event_id=None,
        ),
        requester_id="@alice:example.test",
        client=SimpleNamespace(),
        config=old_config,
        runtime_paths=runtime_paths,
        storage_path=runtime_paths.storage_root,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    create_started = threading.Event()
    release_create = threading.Event()
    original_create = store.create_run

    def blocked_create(run: ScriptRunRecord) -> None:
        create_started.set()
        assert release_create.wait(timeout=5)
        original_create(run)

    monkeypatch.setattr(store, "create_run", blocked_create)
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    assert await asyncio.to_thread(create_started.wait, 1)
    update = asyncio.create_task(runtime.apply_update_plan(_plan(old_config, new_config)))
    await asyncio.sleep(0.05)
    update_finished_before_launch = update.done()
    release_create.set()

    run = await launch
    await update
    durable = store.get_run(run.run_id)

    assert update_finished_before_launch is False
    assert durable.cancel_requested_at is not None
    assert durable.state is ScriptRunState.INTERRUPTED
    await runtime.complete_worker_replacement()


@pytest.mark.asyncio
async def test_cancelled_completion_reopens_admission_before_a_blocked_lease_release(
    tmp_path: Path,
) -> None:
    """Cancellation cannot strand the gate behind disposal of an unpublished old lease."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    lease = _Lease(_Backend([]))
    release_started = threading.Event()
    allow_release = threading.Event()
    release_finished = threading.Event()

    def blocking_release() -> None:
        release_started.set()
        assert allow_release.wait(timeout=5)
        lease.released = True
        release_finished.set()

    lease.release = blocking_release  # type: ignore[method-assign]
    manager = ScriptRunManager(
        store=store,
        broker=MagicMock(),
        worker_client=MagicMock(),
        worker_backend=lease.manager,
        gateway_url="http://primary.test/api/script-gateway",
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._current_worker_lease = lease
    await manager.begin_worker_replacement()

    completion = asyncio.create_task(runtime.complete_worker_replacement())
    try:
        assert await asyncio.to_thread(release_started.wait, 1)
        completion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await completion

        assert manager.worker_replacement_in_progress is False
        assert manager.worker_backend is None
        assert runtime._current_worker_lease is None
        with pytest.raises(ScriptRunManagerError, match="worker backend is unavailable"):
            await manager.run(
                make_test_tool_runtime_context(
                    agent_name="watcher",
                    target=MessageTarget.resolve(
                        room_id="!room:example.test",
                        thread_id="$thread:example.test",
                        reply_to_event_id=None,
                    ),
                    requester_id="@alice:example.test",
                    client=SimpleNamespace(),
                    config=_config(),
                    runtime_paths=runtime_paths,
                    storage_path=runtime_paths.storage_root,
                    relations=make_relation_lookup(),
                    conversation_reader=make_conversation_reader_mock(),
                ),
                source="print('unavailable')\n",
            )
        assert store.list_runs() == []
    finally:
        allow_release.set()

    assert await asyncio.to_thread(release_finished.wait, 1)
    assert lease.released is True


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
            is_authorized=MagicMock(return_value=True),
            resolve=MagicMock(side_effect=AssertionError("must not resolve live runtime")),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: _Lease(backend),
    )

    await _reconcile_once(runtime)

    assert backend.actions == [f"touch:{run.worker_key}", "reconcile:run-1"]


@pytest.mark.asyncio
async def test_worker_replacement_completion_uses_the_config_visible_at_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fallback completion reacquires the old or new backend without crossing identities."""
    runtime_paths = _runtime_paths(tmp_path)
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "updated", "tools": ["script", "calculator"]}},
        defaults={"tools": []},
    )
    committed_config = old_config
    acquired_for: list[Config] = []
    old_lease = _Lease(_Backend([]))
    restored_old_lease = _Lease(_Backend([]))
    new_lease = _Lease(_Backend([]))
    leases = iter((old_lease, restored_old_lease, new_lease))

    def provider(_locator: str | None) -> _Lease:
        acquired_for.append(committed_config)
        return next(leases)

    manager = ScriptRunManager(
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        worker_client=MagicMock(),
        worker_backend=None,
        gateway_url="http://primary.test/api/script-gateway",
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=manager.store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: committed_config,
        worker_lease_provider=provider,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.configured_primary_worker_manager_identity",
        lambda _paths, config: "new" if config is new_config else "old",
    )
    await _reconcile_once(runtime)
    await runtime.apply_update_plan(_plan(old_config, new_config))

    await runtime.complete_worker_replacement()
    assert acquired_for == [old_config, old_config]
    assert runtime._current_worker_lease is restored_old_lease

    await runtime.complete_worker_replacement()
    assert acquired_for == [old_config, old_config]

    await runtime.apply_update_plan(_plan(old_config, new_config))
    committed_config = new_config
    await runtime.complete_worker_replacement()
    assert acquired_for == [old_config, old_config, new_config]
    assert runtime._current_worker_lease is new_lease


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
            is_authorized=MagicMock(return_value=True),
            resolve=MagicMock(side_effect=AssertionError("must not resolve live runtime")),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )

    await _reconcile_once(runtime)

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
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )

    await _reconcile_once(runtime)

    assert reconciled == [second.run_id]


@pytest.mark.asyncio
async def test_backend_provider_failure_does_not_abort_run_reconciliation(tmp_path: Path) -> None:
    """A typed provider failure leaves durable run reconciliation independent."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    reconciled: list[str] = []

    def unavailable_provider(_locator: str | None) -> _Lease:
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
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=unavailable_provider,
    )

    await _reconcile_once(runtime)

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
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lambda _locator: _Lease(backend),
    )

    await _reconcile_once(runtime)

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
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
        pass_timeout_seconds=0.1,
    )
    started = asyncio.get_running_loop().time()

    await _reconcile_once(runtime)

    assert asyncio.get_running_loop().time() - started < 0.2


@pytest.mark.asyncio
async def test_blocking_backend_provider_cannot_stall_the_event_loop_past_pass_deadline(tmp_path: Path) -> None:
    """Potentially blocking backend construction is off-loop and bounded by the pass."""
    heartbeat = asyncio.Event()

    def slow_provider(_locator: str | None) -> None:
        time.sleep(0.2)

    async def beat() -> None:
        await asyncio.sleep(0.01)
        heartbeat.set()

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=_runtime_paths(tmp_path),
        store=ScriptRunStore(_runtime_paths(tmp_path)),
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=AsyncMock()),
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=slow_provider,
        pass_timeout_seconds=0.02,
    )
    heartbeat_task = asyncio.create_task(beat())
    started = asyncio.get_running_loop().time()

    await _reconcile_once(runtime)

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

    def slow_provider(_locator: str | None) -> _Lease:
        nonlocal calls
        calls += 1
        assert release_provider.wait(timeout=5)
        return lease

    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=SimpleNamespace(reconcile_durable=AsyncMock()),
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=slow_provider,
        pass_timeout_seconds=0.02,
    )
    await _reconcile_once(runtime)
    release_provider.set()
    runtime.pass_timeout_seconds = 1

    await _reconcile_once(runtime)

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
        begin_shutdown=AsyncMock(),
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        gateway_url="",
        worker_backend=None,
        reconcile_durable=AsyncMock(),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: workers_runtime_module.lease_primary_worker_manager(
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

    def lease_provider(_locator: str | None) -> workers_runtime_module.PrimaryWorkerManagerLease:
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
        begin_shutdown=AsyncMock(),
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        gateway_url="",
        worker_backend=None,
        reconcile_durable=AsyncMock(),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths),
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
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
        broker=_broker_lifecycle_stub(),
        manager=SimpleNamespace(
            begin_shutdown=AsyncMock(),
            begin_startup_reconciliation=AsyncMock(),
            end_startup_reconciliation=AsyncMock(),
            worker_backend=None,
        ),
        resolver=SimpleNamespace(),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._activated_once = True
    runtime._current_worker_lease = lease

    reconciliation_started = asyncio.Event()

    async def blocking_shutdown_pass(
        _runtime: ScriptRuntimeLifecycle,
        _runs: object,
    ) -> None:
        reconciliation_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ScriptRuntimeLifecycle, "_interrupt_and_prune_for_shutdown", blocking_shutdown_pass)
    shutdown = asyncio.create_task(runtime.shutdown(timeout_seconds=0.05))
    try:
        await asyncio.wait_for(reconciliation_started.wait(), timeout=1.0)
        await asyncio.wait_for(shutdown, timeout=1.0)
        assert await asyncio.to_thread(release_started.wait, 1.0)
        assert lease_released.is_set() is False
    finally:
        release_lease.set()

    assert await asyncio.to_thread(lease_released.wait, 1.0)
    assert lease.released is True


@pytest.mark.asyncio
async def test_shutdown_closes_launch_admission_before_snapshotting_runs(tmp_path: Path) -> None:
    """The shutdown snapshot cannot race any launch that crossed the manager admission boundary."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    admission_closed = False
    original_list_runs = store.list_runs

    async def begin_shutdown() -> None:
        nonlocal admission_closed
        admission_closed = True

    def list_after_admission_close(*args: object, **kwargs: object) -> list[ScriptRunRecord]:
        assert admission_closed is True
        return original_list_runs(*args, **kwargs)

    store.list_runs = list_after_admission_close  # type: ignore[method-assign]
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=_broker_lifecycle_stub(),
        manager=SimpleNamespace(
            begin_shutdown=begin_shutdown,
            worker_backend=None,
        ),
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )

    await runtime.shutdown(timeout_seconds=0.1)

    assert admission_closed is True


@pytest.mark.asyncio
async def test_shutdown_durably_revokes_every_run_before_cleanup_deadline_can_release_lease(
    tmp_path: Path,
) -> None:
    """Queued revocations become durable before the bounded cleanup phase can release ownership."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    runs = [_stored_run(store, runtime_paths, run_id=f"script-{digit * 32}") for digit in ("1", "2", "3")]
    first_revocation_started = threading.Event()
    release_revocations = threading.Event()
    lease_released = threading.Event()
    release_observations: list[set[str]] = []

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        if run_id == runs[0].run_id:
            first_revocation_started.set()
            assert release_revocations.wait(timeout=1.0)
        return store.request_cancel(run_id, reason=reason)

    def observe_release() -> None:
        release_observations.append(
            {run.run_id for run in runs if store.get_run(run.run_id).cancel_requested_at is not None},
        )
        lease_released.set()

    lease = _Lease(_Backend([_worker(run) for run in runs]), on_release=observe_release)
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=_broker_lifecycle_stub(),
        manager=SimpleNamespace(
            begin_shutdown=AsyncMock(),
            begin_startup_reconciliation=AsyncMock(),
            end_startup_reconciliation=AsyncMock(),
            worker_backend=lease.manager,
            request_revocation=request_revocation,
            revoke=AsyncMock(),
            reconcile_durable=AsyncMock(),
        ),
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
        pass_concurrency=1,
    )
    runtime._activated_once = True
    runtime._current_worker_lease = lease

    shutdown = asyncio.create_task(runtime.shutdown(timeout_seconds=0.1))
    try:
        assert await asyncio.to_thread(first_revocation_started.wait, 1.0)
        assert await asyncio.to_thread(lease_released.wait, 0.2) is False
        release_revocations.set()
        await asyncio.wait_for(shutdown, timeout=1.0)
        assert await asyncio.to_thread(lease_released.wait, 1.0)
    finally:
        release_revocations.set()
        if not shutdown.done():
            await asyncio.wait_for(shutdown, timeout=1.0)

    expected_run_ids = {run.run_id for run in runs}
    assert release_observations == [expected_run_ids]
    assert {run.run_id for run in runs if store.get_run(run.run_id).cancel_requested_at is not None} == expected_run_ids


@pytest.mark.asyncio
async def test_shutdown_cancellation_finishes_durable_revocation_then_releases_lease(tmp_path: Path) -> None:
    """Caller cancellation drains accepted writes before releasing process-local ownership."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    runs = [_stored_run(store, runtime_paths, run_id=f"script-{digit * 32}") for digit in ("4", "5")]
    first_revocation_started = threading.Event()
    release_revocations = threading.Event()

    def request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        if run_id == runs[0].run_id:
            first_revocation_started.set()
            assert release_revocations.wait(timeout=1.0)
        return store.request_cancel(run_id, reason=reason)

    lease = _Lease(_Backend([_worker(run) for run in runs]))
    manager = SimpleNamespace(
        begin_shutdown=AsyncMock(),
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        worker_backend=lease.manager,
        request_revocation=request_revocation,
        revoke=AsyncMock(),
        reconcile_durable=AsyncMock(),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=_broker_lifecycle_stub(),
        manager=manager,
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
        pass_concurrency=1,
    )
    runtime._activated_once = True
    runtime._current_worker_lease = lease

    shutdown = asyncio.create_task(runtime.shutdown(timeout_seconds=0.1))
    try:
        assert await asyncio.to_thread(first_revocation_started.wait, 1.0)
        shutdown.cancel()
        done, _pending = await asyncio.wait({shutdown}, timeout=0.1)
        assert done == set()
        release_revocations.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
    finally:
        release_revocations.set()

    assert all(store.get_run(run.run_id).cancel_requested_at is not None for run in runs)
    assert await asyncio.to_thread(lease.release_event.wait, 1.0)
    assert lease.released is True
    assert runtime._current_worker_lease is None
    assert manager.worker_backend is None
    manager.begin_shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_durable_store_failure_still_releases_worker_lease(tmp_path: Path) -> None:
    """A durable shutdown error must not strand process-local worker-manager ownership."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths, run_id=f"script-{'6' * 32}")

    def request_revocation(*, run_id: str, reason: str) -> ScriptRunRecord:
        del run_id, reason
        msg = "durable store unavailable"
        raise ScriptRunStoreError(msg)

    lease = _Lease(_Backend([_worker(run)]))
    manager = SimpleNamespace(
        begin_shutdown=AsyncMock(),
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        worker_backend=lease.manager,
        request_revocation=request_revocation,
        revoke=AsyncMock(),
        reconcile_durable=AsyncMock(),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=_broker_lifecycle_stub(),
        manager=manager,
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._activated_once = True
    runtime._current_worker_lease = lease

    with pytest.raises(_ScriptRuntimeLifecycleError, match="durably revoke every active run"):
        await runtime.shutdown(timeout_seconds=0.1)

    assert store.get_run(run.run_id).cancel_requested_at is None
    assert lease.released is True
    assert runtime._current_worker_lease is None
    assert manager.worker_backend is None
    manager.begin_shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("activated", [True, False])
async def test_shutdown_interrupts_active_run_before_releasing_worker_lease(
    tmp_path: Path,
    activated: bool,
) -> None:
    """Full shutdown revokes broker and process ownership before publishing interruption."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run_pinned_to_worker(store, runtime_paths, run_id=f"script-{'c' * 32}")
    store.claim_call(
        run_id=run.run_id,
        call_id="call-1",
        grant=ScriptToolGrant("calculator", "add"),
        arguments_digest="arguments-digest",
    )
    actions: list[str] = []

    @dataclass
    class _Resolver(_ApprovalSettlementResolver):
        async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
            actions.append("settle-approval")
            await super().settle_run_approvals(run_id, reason=reason)

    @dataclass
    class _Client(_TerminatingWorkerClient):
        async def status(self, _worker: WorkerHandle, *, run_id: str) -> WorkerScriptStatus:
            actions.append("status")
            return await super().status(_worker, run_id=run_id)

        async def cancel(
            self,
            _worker: WorkerHandle,
            *,
            run_id: str,
            force: bool = False,
        ) -> WorkerScriptCancel:
            assert store.get_run(run_id).cancel_requested_at is not None
            actions.append("signal")
            return await super().cancel(_worker, run_id=run_id, force=force)

    resolver = _Resolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    broker_call_started = asyncio.Event()

    async def active_broker_call() -> None:
        broker_call_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            actions.append("cancel-broker-call")

    broker_task = asyncio.create_task(active_broker_call())
    broker._tasks[(run.run_id, "call-1")] = broker_task  # type: ignore[assignment]
    await broker_call_started.wait()
    backend = _Backend([_worker(run)])

    def observe_release() -> None:
        durable = store.get_run(run.run_id)
        assert durable.state is ScriptRunState.INTERRUPTED
        actions.append("release")

    lease = _Lease(backend, on_release=observe_release)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=_Client(),  # type: ignore[arg-type]
        worker_backend=backend,
        gateway_url="http://primary.test/api/script-gateway",
        cancellation_grace_seconds=0,
        cancellation_poll_interval_seconds=0,
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._activated_once = activated
    runtime._current_worker_lease = lease

    await runtime.shutdown()

    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is not None
    assert durable.state is ScriptRunState.INTERRUPTED
    assert store.get_call(run.run_id, "call-1").state.value == "indeterminate"
    assert resolver.settled_runs == [run.run_id]
    assert actions.index("cancel-broker-call") < actions.index("settle-approval")
    assert actions.index("settle-approval") < actions.index("release")
    assert actions.index("signal") < actions.index("release")
    assert actions[-1] == "release"


@pytest.mark.asyncio
async def test_shutdown_deadline_leaves_revoked_run_nonterminal(tmp_path: Path) -> None:
    """An exhausted shutdown budget cannot claim interruption before confirmed exit."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths, run_id=f"script-{'d' * 32}")
    status_started = asyncio.Event()

    @dataclass
    class _Client(_TerminatingWorkerClient):
        async def status(self, _worker: WorkerHandle, *, run_id: str) -> WorkerScriptStatus:
            del run_id
            status_started.set()
            await asyncio.Event().wait()
            msg = "unreachable"
            raise AssertionError(msg)

    resolver = _ApprovalSettlementResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    backend = _Backend([_worker(run)])
    lease = _Lease(backend)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=_Client(),  # type: ignore[arg-type]
        worker_backend=backend,
        gateway_url="http://primary.test/api/script-gateway",
        cancellation_grace_seconds=1,
        cancellation_poll_interval_seconds=0,
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=SimpleNamespace(),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
    )
    runtime._activated_once = True
    runtime._current_worker_lease = lease

    await runtime.shutdown(timeout_seconds=1.0)

    assert status_started.is_set()
    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is not None
    assert durable.state is ScriptRunState.RUNNING

    assert await asyncio.to_thread(lease.release_event.wait, 1)
    assert lease.released is True


@pytest.mark.asyncio
async def test_shutdown_before_activation_releases_committed_worker_lease(tmp_path: Path) -> None:
    """A committed generation must not survive shutdown just because activation never ran."""
    lease = _Lease(_Backend([]))
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=_runtime_paths(tmp_path),
        store=ScriptRunStore(_runtime_paths(tmp_path)),
        broker=_broker_lifecycle_stub(),
        manager=SimpleNamespace(
            begin_shutdown=AsyncMock(),
            begin_startup_reconciliation=AsyncMock(),
            end_startup_reconciliation=AsyncMock(),
            worker_backend=lease.manager,
        ),
        resolver=SimpleNamespace(),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
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
    first = _Lease(_Backend([_worker(run)]))
    second = _Lease(_Backend([]))

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
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lambda _locator: next(leases),
        pass_timeout_seconds=0.02,
    )
    await _reconcile_once(runtime)
    store.transition_run(run.run_id, state=ScriptRunState.EXITED, exit_code=0)
    started = asyncio.get_running_loop().time()

    await _reconcile_once(runtime)

    assert asyncio.get_running_loop().time() - started < 0.1


@pytest.mark.asyncio
async def test_reload_timeout_aborts_after_durably_revoking_all_removed_owner_runs(tmp_path: Path) -> None:
    """A stuck process confirmation aborts the reload after durable revocation has completed."""
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

    async def reconcile_revoked_process(*, run_id: str) -> ScriptRunRecord:
        if run_id == first.run_id:
            await never.wait()
        return store.get_run(run_id)

    async def reconcile_durable(*, run_id: str, broker_revoked: bool = False) -> ScriptRunRecord:
        assert broker_revoked is True
        return store.get_run(run_id)

    current = _config()
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(
            begin_startup_reconciliation=AsyncMock(),
            end_startup_reconciliation=AsyncMock(),
            request_revocation=request_revocation,
            revoke=revoke,
            reconcile_revoked_process=reconcile_revoked_process,
            reconcile_durable=reconcile_durable,
        ),
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: current,
        worker_lease_provider=lambda _locator: None,
        pass_timeout_seconds=0.2,
    )

    with pytest.raises(RuntimeError, match="Background script reload did not durably revoke every active run"):
        await runtime.apply_update_plan(_plan(current, Config(defaults={"tools": []})))

    assert store.get_run(first.run_id).cancel_requested_at is not None
    assert store.get_run(second.run_id).cancel_requested_at is not None
    assert set(broker_revocations) == {first.run_id, second.run_id}


@pytest.mark.asyncio
async def test_reload_timeout_aborts_when_durable_revocation_exceeds_the_overall_deadline(tmp_path: Path) -> None:
    """A deadline cannot return while an accepted durable revocation is still running."""
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
            begin_startup_reconciliation=AsyncMock(),
            end_startup_reconciliation=AsyncMock(),
            request_revocation=slow_request_revocation,
            revoke=revoke,
            reconcile_durable=AsyncMock(),
        ),
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=lambda: current,
        worker_lease_provider=lambda _locator: None,
        pass_timeout_seconds=0.02,
    )
    started = asyncio.get_running_loop().time()

    with pytest.raises(RuntimeError, match="Background script reload did not durably revoke every active run"):
        await runtime.apply_update_plan(_plan(current, Config(defaults={"tools": []})))

    assert asyncio.get_running_loop().time() - started >= 0.15
    assert store.get_run("run-1").cancel_requested_at is not None
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
        begin_shutdown=AsyncMock(),
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        gateway_url="",
        worker_backend=None,
        reconcile_durable=AsyncMock(),
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(is_authorized=MagicMock(return_value=True), prune_approvals=prune_approvals),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
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
        manager=SimpleNamespace(),
        resolver=SimpleNamespace(is_authorized=MagicMock(return_value=True), prune_approvals=prune_approvals),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
        retention_seconds=0.001,
        pass_timeout_seconds=0.02,
    )
    started = asyncio.get_running_loop().time()

    await _prune_once(runtime)

    assert asyncio.get_running_loop().time() - started < 0.1


@pytest.mark.asyncio
async def test_maintenance_retries_after_an_unexpected_cycle_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unexpected pass failure is logged and the next maintenance interval still runs."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
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
        begin_shutdown=AsyncMock(),
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        gateway_url="",
        worker_backend=None,
        reconcile_durable=reconcile_durable,
        request_revocation=store.request_cancel,
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
        reconcile_interval_seconds=0.01,
        pass_timeout_seconds=0.05,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")

    await runtime.start()
    _stored_run(store, runtime_paths)
    await asyncio.wait_for(recovered.wait(), timeout=1.0)
    monkeypatch.setattr(ScriptRuntimeLifecycle, "_interrupt_and_prune_for_shutdown", AsyncMock())
    await runtime.shutdown()

    assert calls >= 3


@pytest.mark.asyncio
async def test_started_lifecycle_periodically_enforces_run_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Max-runtime reconciliation continues without a caller requesting status."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    reconciled_twice = asyncio.Event()
    calls = 0

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        nonlocal calls
        calls += 1
        if calls == 2:
            reconciled_twice.set()
        return store.get_run(run_id)

    manager = SimpleNamespace(
        begin_shutdown=AsyncMock(),
        begin_startup_reconciliation=AsyncMock(),
        end_startup_reconciliation=AsyncMock(),
        gateway_url="",
        worker_backend=None,
        reconcile_durable=reconcile_durable,
        request_revocation=store.request_cancel,
    )
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=manager,
        resolver=SimpleNamespace(resolve=MagicMock(), is_authorized=MagicMock(return_value=True)),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
        reconcile_interval_seconds=0.01,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")

    await runtime.start()
    _stored_run(store, runtime_paths)
    await asyncio.wait_for(reconciled_twice.wait(), timeout=1.0)
    monkeypatch.setattr(ScriptRuntimeLifecycle, "_interrupt_and_prune_for_shutdown", AsyncMock())
    await runtime.shutdown()

    assert calls >= 2


@pytest.mark.asyncio
async def test_terminal_run_is_pruned_only_after_retention_and_approval_cleanup(tmp_path: Path) -> None:
    """Retention prunes durable receipts only after the cutoff and approval cleanup."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run_id = f"script-{'e' * 32}"
    running = _stored_run_pinned_to_worker(store, runtime_paths, run_id=run_id)
    store.record_process_exit(
        running.run_id,
        exit_code=0,
        error=None,
        output="",
        cancellation_reason="Background script process exited.",
    )
    terminal = store.finalize_cleaned_run(running.run_id, state=ScriptRunState.EXITED)
    assert terminal.finished_at is not None
    runtime = ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=store,
        broker=MagicMock(),
        manager=SimpleNamespace(),
        resolver=SimpleNamespace(
            is_authorized=MagicMock(return_value=True),
            resolve=MagicMock(side_effect=AssertionError("must not resolve live runtime")),
            prune_approvals=AsyncMock(return_value=True),
        ),
        config_provider=_config,
        worker_lease_provider=lambda _locator: None,
        retention_seconds=60.0,
    )
    finished_at = datetime.fromisoformat(terminal.finished_at)

    await _prune_once(runtime, now=finished_at + timedelta(seconds=59))
    assert store.get_run(run_id).state is ScriptRunState.EXITED

    await _prune_once(runtime, now=finished_at + timedelta(seconds=61))
    with pytest.raises(ScriptRunNotFoundError):
        store.get_run(run_id)


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
        worker_lease_provider=lambda _locator: _Lease(backend),
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
