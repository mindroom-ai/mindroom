"""Tests for process-local background-script runtime lifecycle coordination."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import socket
import threading
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from structlog.testing import capture_logs

import mindroom.workers.runtime as workers_runtime_module
from mindroom.api.script_gateway import bind_script_tool_broker
from mindroom.api.script_gateway import router as script_gateway_router
from mindroom.approval.auto_approve_windows import start_auto_approve
from mindroom.approval.tool_approval import (
    DEFAULT_ROUTER_MANAGED_ROOM_REASON,
    NO_APPROVER_REASON,
    BackgroundScriptToolOrigin,
    ToolApprovalDecision,
)
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.event_journal import BackgroundApprovalDecision
from mindroom.message_target import MessageTarget
from mindroom.orchestration.config_updates import ConfigUpdatePlan, build_config_update_plan
from mindroom.orchestration.runtime import cancel_task
from mindroom.orchestration.script_runtime import (
    _DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ScriptRuntimeLifecycle,
    _LiveScriptRuntimeResolver,
    _PendingWorkerLease,
    _release_worker_leases_before_deadline,
    _script_gateway_url,
    _ScriptRuntimeLifecycleError,
    _ScriptRuntimeUnavailableError,
    build_script_runtime,
    optional_script_gateway_url,
)
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError, ScriptToolBroker
from mindroom.script_runs.manager import ScriptRunManager, ScriptRunManagerError
from mindroom.script_runs.models import (
    ScriptCallState,
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
    script_worker_key_for_run,
)
from mindroom.script_runs.reasons import WORKER_CONFIGURATION_CHANGED
from mindroom.script_runs.recovery import (
    run_matches_recovery_signature,
    script_process_authority,
    script_recovery_signature,
)
from mindroom.script_runs.store import (
    ScriptCallNotFoundError,
    ScriptRunNotFoundError,
    ScriptRunStore,
    ScriptRunStoreError,
)
from mindroom.script_runs.wire import (
    ScriptWorkerError,
    WorkerScriptCancel,
    WorkerScriptStatus,
)
from mindroom.tool_system.toolkit_connections import RetainedToolkitCleanups
from mindroom.tool_system.worker_routing import (
    build_agent_toolkit_worker_target,
    build_tool_execution_identity,
    serialize_tool_execution_identity,
    worker_root_path,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import ConfiguredWorkerBackend, WorkerHandle, WorkerSpec
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    SETTLE_TIMEOUT_SECONDS,
    bound_worker_launch_config,
    initialize_approval_store_for_test,
    loop_runs_while_blocked,
    make_conversation_reader_mock,
    make_relation_lookup,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workers.backend import WorkerBackend


def _manager_stub(**fields: object) -> SimpleNamespace:
    """Return a manager stand-in with *fields* whose fences are no-ops and whose backend lookup is the manager's own."""
    stub = SimpleNamespace(
        **{
            "worker_backend": None,
            "begin_shutdown": AsyncMock(),
            "begin_startup_reconciliation": AsyncMock(),
            "end_startup_reconciliation": AsyncMock(),
            **fields,
        },
    )
    stub.worker_backend_for = partial(ScriptRunManager.worker_backend_for, stub)
    return stub


_STATIC_RUNNER_ENV = {
    "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
    "MINDROOM_WORKER_BACKEND": "static_runner",
    "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox-runner.test",
}


def _runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    """Return runtime paths under *tmp_path* whose process environment is *env*, else worker sandboxing for all tools."""
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
        process_env=env or {"MINDROOM_SANDBOX_EXECUTION_MODE": "all"},
    )


def _config(*, private: bool = False) -> Config:
    agent: dict[str, object] = {"display_name": "Watcher", "tools": ["script", "calculator"]}
    if private:
        agent["private"] = {"per": "user_agent", "root": "private/watcher"}
    return Config(agents={"watcher": agent}, defaults={"tools": []})


def _lifecycle(
    runtime_paths: RuntimePaths,
    *,
    manager: object,
    store: ScriptRunStore | None = None,
    broker: object = None,
    resolver: object = None,
    config_provider: Callable[[], Config | None] = _config,
    worker_lease_provider: Callable[[str | None], object] = lambda _locator: None,
    **options: float,
) -> ScriptRuntimeLifecycle:
    """Build a lifecycle around *manager*, sharing a real manager's store and broker unless given others."""
    if isinstance(manager, ScriptRunManager):
        store = manager.store if store is None else store
        broker = manager.broker if broker is None else broker
    return ScriptRuntimeLifecycle(
        runtime_paths=runtime_paths,
        store=ScriptRunStore(runtime_paths) if store is None else store,
        broker=_mock_broker() if broker is None else broker,
        manager=manager,
        resolver=(
            SimpleNamespace(is_authorized=MagicMock(return_value=True), prune_approvals=AsyncMock(return_value=True))
            if resolver is None
            else resolver
        ),
        config_provider=config_provider,
        worker_lease_provider=worker_lease_provider,
        **options,
    )


@pytest.fixture(autouse=True)
def _applied_config() -> Iterator[None]:
    """Bind the applied config script workers launch from, as `mindroom run` does."""
    with bound_worker_launch_config(_config()):
        yield


def _plan(current: Config, updated: Config) -> ConfigUpdatePlan:
    existing = {"router", *current.agents, *current.teams}
    return build_config_update_plan(
        current_config=current,
        new_config=updated,
        bots={name: MagicMock() for name in existing},
    )


# A pass deadline that the work before a test's blocking step meets even on a loaded host, far below the outer wait.
_LOADED_HOST_PASS_TIMEOUT_SECONDS = 1.0


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
        raise ScriptWorkerError(message)

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
class _LaunchingWorkerClient(_TerminatingWorkerClient):
    """Accept every launch at once, so a test blocks a launch where its own fake does."""

    async def launch(
        self,
        _worker: WorkerHandle,
        *,
        run_id: str,
        source_digest: str,
        gateway_url: str,
        max_runtime_seconds: int,
        state_scope_worker_key: str | None = None,
        private_agent_names: tuple[str, ...] | None = None,
        config_snapshot: dict[str, object] | None = None,
    ) -> None:
        del run_id, source_digest, gateway_url, state_scope_worker_key, private_agent_names, config_snapshot
        assert max_runtime_seconds > 0


@dataclass
class _LaunchingBackend(_Backend):
    """Provide the shared-state worker required by a real manager launch."""

    handles: list[WorkerHandle] = field(default_factory=list)
    runtime_paths: RuntimePaths = field(kw_only=True)

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


@dataclass
class _ApprovalSettlementResolver:
    """Keep broker ownership settlement observable through its durable receipts."""

    settled_runs: list[str] = field(default_factory=list)

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        del reason
        self.settled_runs.append(run_id)


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
    authorized: bool | None = True

    def is_authorized(self, run: ScriptRunRecord, *, config: Config | None = None) -> bool | None:
        del run, config
        return self.authorized

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


def _mock_broker() -> MagicMock:
    """A broker whose calls the test does not drive, with a real cleanup registry for shutdown to drain."""
    return MagicMock(_cleanups=RetainedToolkitCleanups(failure_event="script_tool_cleanup_failed"))


def _broker_lifecycle_stub() -> SimpleNamespace:
    return SimpleNamespace(
        _cleanups=RetainedToolkitCleanups(failure_event="script_tool_cleanup_failed"),
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
    worker_backend_locator: str | None = "locator-a",
    recovery_signature: str | None = None,
    state: ScriptRunState = ScriptRunState.RUNNING,
    started_at: str | None = None,
) -> ScriptRunRecord:
    run = _run(runtime_paths, run_id=run_id, state=ScriptRunState.STARTING)
    assert run.worker_key is not None
    created = store.create_run(
        replace(
            run,
            worker_key=script_worker_key_for_run(run.worker_key, run_id),
            worker_backend_locator=worker_backend_locator,
            recovery_signature=recovery_signature,
            started_at=started_at,
        ),
    )
    return store.transition_run(
        created.run_id,
        state=state,
        worker_id="worker-1",
    )


def _process_authority_change_scenario(
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
        defaults={"tools": [], "worker_scope": "user_agent"},
    )
    runtime = _lifecycle(runtime_paths, store=store, broker=broker, manager=manager, config_provider=lambda: old_config)
    runtime._publish_worker_lease(lease)
    return runtime, store, run, lease, worker_client, old_config, new_config


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "not-a-number"])
def test_retention_rejects_non_numeric_and_non_finite_values(tmp_path: Path, raw: str) -> None:
    """Retention must be a finite number so pruning has a meaningful cutoff."""
    runtime_paths = _runtime_paths(
        tmp_path,
        MINDROOM_SANDBOX_EXECUTION_MODE="all",
        MINDROOM_SCRIPT_RETENTION_SECONDS=raw,
    )

    with pytest.raises(ValueError, match="positive number"):
        _built_runtime(runtime_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
@pytest.mark.parametrize("execution_mode", ["off", "local", "disabled"])
async def test_explicit_local_script_mode_uses_embedded_gateway_with_dedicated_backend(
    tmp_path: Path,
    backend: str,
    execution_mode: str,
) -> None:
    """An explicitly unsafe-local script mode takes precedence over backend selection."""
    runtime_paths = _runtime_paths(
        tmp_path,
        MINDROOM_SANDBOX_EXECUTION_MODE=execution_mode,
        MINDROOM_WORKER_BACKEND=backend,
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
    runtime_paths = _runtime_paths(tmp_path, MINDROOM_SANDBOX_EXECUTION_MODE="local")

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
    runtime_paths = _runtime_paths(tmp_path, **process_env)

    with pytest.raises(ValueError, match="MINDROOM_SCRIPT_GATEWAY_URL"):
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
async def test_static_runner_workers_require_an_explicit_reachable_gateway(tmp_path: Path) -> None:
    """A separate static runner must not receive the primary process's loopback URL."""
    runtime_paths = _runtime_paths(tmp_path, **_STATIC_RUNNER_ENV)

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
    runtime_paths = _runtime_paths(tmp_path, **_STATIC_RUNNER_ENV, **{environment_name: configured_url})

    with pytest.raises(ValueError, match="non-loopback"):
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
@pytest.mark.parametrize("public_url", [None, "http://localhost:8765/"], ids=["unset", "unreachable"])
async def test_optional_worker_gateway_turns_off_for_a_missing_or_unreachable_public_url(
    tmp_path: Path,
    public_url: str | None,
) -> None:
    """Without an explicit gateway, background scripts turn off instead of failing startup, and an unusable public URL says so."""
    process_env = {} if public_url is None else {"MINDROOM_PUBLIC_URL": public_url}
    runtime_paths = _runtime_paths(tmp_path, **_STATIC_RUNNER_ENV, **process_env)

    with capture_logs() as logs:
        gateway_url = await optional_script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104

    assert gateway_url == ""
    warned = [entry for entry in logs if entry["event"] == "background_script_gateway_disabled_invalid_public_url"]
    assert len(warned) == (public_url is not None)


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
    runtime_paths = _runtime_paths(tmp_path, **_STATIC_RUNNER_ENV, MINDROOM_SCRIPT_GATEWAY_URL=configured_url)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 8765))])

    with pytest.raises(ValueError, match="non-loopback"):
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
async def test_explicit_gateway_must_be_a_valid_http_url(tmp_path: Path) -> None:
    """Malformed explicit gateway configuration fails before any worker launch."""
    runtime_paths = _runtime_paths(tmp_path, **_STATIC_RUNNER_ENV, MINDROOM_SCRIPT_GATEWAY_URL="not-a-url")

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
    runtime_paths = _runtime_paths(tmp_path, **_STATIC_RUNNER_ENV, **{environment_name: configured_url})
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
    runtime_paths = _runtime_paths(
        tmp_path,
        **_STATIC_RUNNER_ENV,
        MINDROOM_SCRIPT_GATEWAY_URL="https://gateway.test/api/script-gateway",
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
    """Script tools are served only between activation with the API and unbinding or shutdown."""
    runtime = _served_lifecycle(tmp_path)

    await runtime.start()
    assert runtime.tool_manager is None

    runtime.bind_api("http://primary.test/api/script-gateway/")
    assert runtime._startup_task is not None
    await asyncio.wait_for(runtime._startup_task, timeout=SETTLE_TIMEOUT_SECONDS)
    assert runtime.tool_manager is runtime.manager
    assert runtime.manager.gateway_url == "http://primary.test/api/script-gateway"

    await runtime.unbind_api()
    assert runtime.tool_manager is None
    runtime.bind_api("http://primary.test/api/script-gateway/")
    assert runtime._startup_task is not None
    await asyncio.wait_for(runtime._startup_task, timeout=SETTLE_TIMEOUT_SECONDS)
    assert runtime.tool_manager is runtime.manager

    await runtime.shutdown()

    assert runtime.tool_manager is None


@pytest.mark.asyncio
async def test_rebinding_while_startup_cleanup_is_pending_keeps_one_launch_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A rebind before blocked startup cleanup finishes cannot leave launches fenced after cleanup succeeds."""
    runtime_paths = _runtime_paths(tmp_path)
    manager = ScriptRunManager(
        store=ScriptRunStore(runtime_paths),
        broker=_mock_broker(),
        worker_client=MagicMock(),
        worker_backend=None,
        gateway_url="",
    )
    runtime = _lifecycle(runtime_paths, broker=manager.broker, manager=manager)
    original_cleanup = ScriptRuntimeLifecycle._startup_cleanup_pass
    cleanup_attempts = 0

    async def cleanup_blocked_once(target: ScriptRuntimeLifecycle) -> None:
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            message = "inherited run still pending"
            raise ScriptRunStoreError(message)
        await original_cleanup(target)

    monkeypatch.setattr(ScriptRuntimeLifecycle, "_startup_cleanup_pass", cleanup_blocked_once)
    await runtime.start()
    runtime.bind_api("http://primary.test/api/script-gateway")
    assert runtime._startup_task is not None
    await asyncio.wait_for(runtime._startup_task, timeout=SETTLE_TIMEOUT_SECONDS)
    assert runtime._startup_cleanup_pending is True

    await runtime.unbind_api()
    runtime.bind_api("http://primary.test/api/script-gateway")
    assert runtime._startup_task is not None
    await asyncio.wait_for(runtime._startup_task, timeout=SETTLE_TIMEOUT_SECONDS)

    assert cleanup_attempts == 2
    assert runtime._startup_cleanup_pending is False
    assert manager._startup_reconciliation_owners == 0
    await manager._admit_launch()
    await manager._release_launch_admission()
    await runtime.shutdown()


def _served_lifecycle(tmp_path: Path, *, api_enabled: bool = True) -> ScriptRuntimeLifecycle:
    runtime_paths = _runtime_paths(tmp_path)
    return _lifecycle(
        runtime_paths,
        manager=_manager_stub(gateway_url=""),
        api_enabled=api_enabled,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["shutdown", "unbind_api"])
async def test_stopping_stops_serving_script_tools_before_it_cancels_background_work(
    tmp_path: Path,
    stop: str,
) -> None:
    """A tool call that arrives while shutdown or an API unbind waits for background tasks finds the runtime not ready."""
    runtime = _served_lifecycle(tmp_path)
    await runtime.start()
    runtime.bind_api("http://primary.test/api/script-gateway")
    assert runtime._startup_task is not None
    await asyncio.wait_for(runtime._startup_task, timeout=SETTLE_TIMEOUT_SECONDS)
    assert runtime.tool_manager is runtime.manager
    cancelling = asyncio.Event()
    release = asyncio.Event()

    async def finish_slowly_when_cancelled() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            raise

    maintenance_task, runtime._maintenance_task = (
        runtime._maintenance_task,
        asyncio.create_task(
            finish_slowly_when_cancelled(),
        ),
    )
    await cancel_task(maintenance_task)
    stopping = asyncio.create_task(runtime.shutdown() if stop == "shutdown" else runtime.unbind_api())
    await asyncio.wait_for(cancelling.wait(), timeout=SETTLE_TIMEOUT_SECONDS)

    assert runtime.tool_manager is None

    release.set()
    await stopping
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_a_runtime_without_the_api_never_serves_script_tools(tmp_path: Path) -> None:
    """Without the gateway API no script could call back, so the tool stays unavailable after activation."""
    runtime = _served_lifecycle(tmp_path, api_enabled=False)

    await runtime.start()

    assert runtime._started is True
    assert runtime.tool_manager is None
    await runtime.shutdown()


@dataclass
class _RecoveringBackend(_Backend):
    backend_name: str = "kubernetes"
    signature: str = "worker-authority-v1"

    def script_recovery_signature(self) -> str:
        return self.signature

    def script_resource_recovery_authority(self, resource_profile: str | None) -> dict[str, object]:
        return {"profile": resource_profile, "requests": {}, "limits": {}}


@dataclass
class _DigestOnlyRecoveringBackend(_Backend):
    backend_name: str = "kubernetes"

    def script_recovery_signature(self) -> str:
        return "worker-authority-v1"


def _recovery_scenario(
    tmp_path: Path,
    *,
    state: ScriptRunState = ScriptRunState.RUNNING,
    started_at: str | None = None,
) -> tuple[ScriptRuntimeLifecycle, ScriptRunRecord, _RecoveringBackend, _TerminatingWorkerClient]:
    paths = _runtime_paths(
        tmp_path,
        MINDROOM_SANDBOX_EXECUTION_MODE="all",
        MINDROOM_SCRIPT_GATEWAY_ISOLATED="true",
        MINDROOM_SCRIPT_GATEWAY_URL="http://primary.test/api/script-gateway",
    )
    config = _config()
    store = ScriptRunStore(paths)
    backend = _RecoveringBackend([])
    gateway_url = "http://primary.test/api/script-gateway"
    signature = script_recovery_signature(
        backend=backend,
        config=config,
        agent_name="watcher",
        gateway_url=gateway_url,
    )
    assert signature is not None
    run = _stored_run_pinned_to_worker(
        store,
        paths,
        run_id=f"script-{'a' * 32}",
        recovery_signature=signature,
        state=state,
        started_at=started_at,
    )
    backend.handles = [_worker(run)]
    resolver = _StartupAdmissionResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    client = _TerminatingWorkerClient()
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=client,
        # The lifecycle publishes the backend once it holds a lease for it.
        worker_backend=None,
        gateway_url=gateway_url,
        cancellation_grace_seconds=0,
    )
    runtime = _lifecycle(
        paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=lambda: config,
        worker_lease_provider=lambda locator: _Lease(backend) if locator in {None, "locator-a"} else None,
    )
    runtime.bind_api(gateway_url)
    return runtime, run, backend, client


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [ScriptRunState.STARTING, ScriptRunState.RUNNING])
async def test_startup_adopts_compatible_script_without_recreating_worker(
    tmp_path: Path,
    state: ScriptRunState,
) -> None:
    """A live worker keeps its process and authority when the primary is reconstructed."""
    runtime, run, backend, client = _recovery_scenario(tmp_path, state=state)
    try:
        await runtime.start()
        durable = runtime.store.get_run(run.run_id)
        assert durable.state is ScriptRunState.RUNNING
        assert durable.cancel_requested_at is None
        assert durable.worker_id == run.worker_id
        assert client.exited is False
        assert runtime.broker._call_admission_open.is_set()
        assert set(backend.actions) == {f"touch:{run.worker_key}"}
    finally:
        await runtime.shutdown()
    assert client.exited is False
    assert runtime.store.get_run(run.run_id).cancel_requested_at is None


@pytest.mark.asyncio
async def test_incomplete_kubernetes_recovery_backend_cannot_mint_or_adopt_authority(tmp_path: Path) -> None:
    """A digest without selected-resource authority cannot claim recoverable ownership."""
    paths = _runtime_paths(
        tmp_path,
        MINDROOM_SANDBOX_EXECUTION_MODE="all",
        MINDROOM_SCRIPT_GATEWAY_ISOLATED="true",
        MINDROOM_SCRIPT_GATEWAY_URL="http://primary.test/api/script-gateway",
    )
    config = _config()
    backend = _DigestOnlyRecoveringBackend([])
    gateway_url = "http://primary.test/api/script-gateway"
    minted_signature = script_recovery_signature(
        backend=backend,
        config=config,
        agent_name="watcher",
        gateway_url=gateway_url,
    )
    store = ScriptRunStore(paths)
    run = _stored_run_pinned_to_worker(
        store,
        paths,
        run_id=f"script-{'c' * 32}",
        recovery_signature=minted_signature or f"v2:{'0' * 64}",
    )
    backend.handles = [_worker(run)]
    client = _TerminatingWorkerClient()
    resolver = _StartupAdmissionResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=client,
        worker_backend=backend,
        gateway_url=gateway_url,
        cancellation_grace_seconds=0,
    )
    runtime = _lifecycle(
        paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        config_provider=lambda: config,
        worker_lease_provider=lambda _locator: _Lease(backend),
    )
    runtime.bind_api(gateway_url)

    try:
        await runtime.start()
        assert minted_signature is None
        assert store.get_run(run.run_id).state is ScriptRunState.INTERRUPTED
        assert client.exited is True
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_active_runs_classify_unfinished_runs_with_the_startup_adoption_rule(tmp_path: Path) -> None:
    """Activity lists every unfinished run read-only and marks only runs restart startup would adopt."""
    runtime, adoptable, _backend, client = _recovery_scenario(tmp_path)
    paths = runtime.runtime_paths
    legacy = _stored_run_pinned_to_worker(runtime.store, paths, run_id=f"script-{'b' * 32}")
    cancelling = _stored_run_pinned_to_worker(
        runtime.store,
        paths,
        run_id=f"script-{'c' * 32}",
        recovery_signature=adoptable.recovery_signature,
    )
    runtime.store.request_cancel(cancelling.run_id, reason="Cancelled by owner.")
    stale = _stored_run_pinned_to_worker(
        runtime.store,
        paths,
        run_id=f"script-{'e' * 32}",
        recovery_signature=f"v2:{'0' * 64}",
    )
    finished = _stored_run_pinned_to_worker(runtime.store, paths, run_id=f"script-{'d' * 32}")
    runtime.store.transition_run(finished.run_id, state=ScriptRunState.EXITED, exit_code=0)
    await runtime._refresh_worker_backend()
    before = {run.run_id: run for run in runtime.store.list_runs()}

    active = await runtime.active_runs()

    assert {run.run_id: run.recoverable for run in active} == {
        adoptable.run_id: True,
        legacy.run_id: False,
        cancelling.run_id: False,
        stale.run_id: False,
    }
    assert {(run.responder, run.requester_id) for run in active} == {("watcher", "@alice:example.test")}
    assert {run.run_id: run for run in runtime.store.list_runs()} == before
    assert client.exited is False


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, None])
async def test_active_runs_treat_unconfirmed_owner_authorization_as_interruptible(
    tmp_path: Path,
    authorized: bool | None,
) -> None:
    """Startup only adopts a run whose owner is confirmed, so activity must not call it recoverable."""
    runtime, run, _backend, client = _recovery_scenario(tmp_path)
    await runtime._refresh_worker_backend()
    runtime.resolver.authorized = authorized

    active = await runtime.active_runs()

    assert [(info.run_id, info.recoverable) for info in active] == [(run.run_id, False)]
    assert client.exited is False


@pytest.mark.asyncio
async def test_active_runs_treat_an_unresolved_worker_backend_as_interruptible(tmp_path: Path) -> None:
    """Without the run's owning backend the recovery signature cannot be verified."""
    runtime, run, _backend, _client = _recovery_scenario(tmp_path)

    active = await runtime.active_runs()

    assert [(info.run_id, info.recoverable) for info in active] == [(run.run_id, False)]


def test_process_authority_exists_only_for_a_configured_agent_with_the_script_tool() -> None:
    """A run of a removed agent, or of an agent without the script tool, has no process authority to recover."""
    config = Config(
        agents={
            "watcher": {"display_name": "Watcher", "tools": ["script"]},
            "helper": {"display_name": "Helper", "tools": []},
        },
        defaults={"tools": []},
    )

    assert script_process_authority(config, "watcher") is not None
    assert script_process_authority(config, "helper") is None
    assert script_process_authority(config, "removed") is None


def test_recovery_signature_of_a_fixed_authority_is_stable_across_releases() -> None:
    """A primary upgrade adopts a still-running worker only if the same authority signs to the same digest."""

    class _Backend:
        def script_recovery_signature(self) -> str:
            return "stable-worker-authority"

        def script_resource_recovery_authority(self, resource_profile: str | None) -> dict[str, object]:
            return {
                "profile": resource_profile,
                "requests": {"cpu": "250m", "memory": "512Mi"},
                "limits": {"cpu": "1", "memory": "2Gi"},
            }

    signature = script_recovery_signature(
        backend=_Backend(),  # type: ignore[arg-type]
        config=Config(agents={"watcher": {"display_name": "Watcher", "tools": ["script"]}}, defaults={"tools": []}),
        agent_name="watcher",
        gateway_url="http://primary.test/api/script-gateway/",
        resource_profile="standard",
    )

    assert signature == "v2:e623e91def532ed4b84fcc39ab9ef0512597e7d2d8a7bb393a4ad37dd5b17e23"


def test_recovery_requires_a_persisted_signature(tmp_path: Path) -> None:
    """An unavailable historical digest cannot match a missing durable authority record."""
    runtime, run, backend, _client = _recovery_scenario(tmp_path)
    assert not run_matches_recovery_signature(
        run=replace(run, recovery_signature=None),
        backend=backend,
        config=_config(),
        gateway_url=runtime.manager.gateway_url,
    )


@pytest.mark.asyncio
async def test_startup_adopts_script_after_compatible_delegation_change(tmp_path: Path) -> None:
    """An unrelated delegation edit leaves the run's process authority unchanged."""
    runtime, run, _backend, client = _recovery_scenario(tmp_path)
    runtime.config_provider = lambda: Config(
        agents={
            "watcher": {
                "display_name": "Watcher",
                "tools": ["script", "calculator"],
                "delegate_to": ["watcher"],
            },
        },
        defaults={"tools": []},
    )

    try:
        await runtime.start()
        durable = runtime.store.get_run(run.run_id)
        assert durable.state is ScriptRunState.RUNNING
        assert durable.cancel_requested_at is None
        assert client.exited is False
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_preserves_worker_and_settles_orphaned_calls(tmp_path: Path) -> None:
    """Detaching the primary does not revoke the run or replay accepted calls."""
    runtime, run, backend, client = _recovery_scenario(tmp_path)
    runtime._publish_worker_lease(_Lease(backend))
    runtime.store.claim_call(
        run_id=run.run_id,
        call_id="accepted-before-restart",
        grant=ScriptToolGrant("calculator", "add"),
        arguments_digest="arguments-digest",
    )

    await runtime.shutdown()

    durable = runtime.store.get_run(run.run_id)
    assert durable.state is ScriptRunState.RUNNING
    assert durable.cancel_requested_at is None
    assert client.exited is False
    assert backend.handles == [_worker(run)]
    assert runtime.store.get_call(run.run_id, "accepted-before-restart").state is ScriptCallState.INDETERMINATE


@pytest.mark.asyncio
async def test_startup_interrupts_recovery_contract_mismatch(tmp_path: Path) -> None:
    """An offline worker authority change cannot preserve the old process grant."""
    runtime, run, backend, client = _recovery_scenario(tmp_path)
    backend.signature = "changed-worker-authority"
    try:
        await runtime.start()
        assert runtime.store.get_run(run.run_id).state is ScriptRunState.INTERRUPTED
        assert client.exited is True
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_interrupts_a_run_from_script_protocol_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run launched under protocol 1 is interrupted instead of adopted, because its worker lacks the status route."""
    with monkeypatch.context() as protocol_1:
        protocol_1.setattr("mindroom.script_runs.recovery.SCRIPT_PROTOCOL_VERSION", 1)
        runtime, run, backend, client = _recovery_scenario(tmp_path)

    async def missing_status_route(_worker: WorkerHandle, *, run_id: str) -> WorkerScriptStatus:
        del run_id
        message = "Not Found"
        raise ScriptWorkerError(message)

    monkeypatch.setattr(client, "status", missing_status_route)
    try:
        await runtime.start()
        assert runtime.store.get_run(run.run_id).state is ScriptRunState.INTERRUPTED
        assert f"retire:{run.worker_key}" in backend.actions
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_records_missing_recoverable_worker_as_interrupted(tmp_path: Path) -> None:
    """Worker loss never causes script source to be automatically relaunched."""
    runtime, run, backend, _client = _recovery_scenario(tmp_path)
    backend.handles.clear()
    try:
        await runtime.start()
        assert runtime.store.get_run(run.run_id).state is ScriptRunState.INTERRUPTED
        assert backend.handles == []
        assert runtime.broker._call_admission_open.is_set()
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "private_scope",
        "worker_scope",
        "knowledge_mount",
        "worker_credentials",
        "script_tool",
        "gateway",
        "gateway_isolation",
        "explicit_gateway",
    ],
)
async def test_startup_rejects_changed_script_recovery_scope(tmp_path: Path, change: str) -> None:
    """A surviving process cannot retain authority across incompatible offline changes."""
    runtime, run, _backend, client = _recovery_scenario(tmp_path)
    if change == "private_scope":
        runtime.config_provider = lambda: _config(private=True)
    elif change == "worker_scope":
        runtime.config_provider = lambda: Config(
            agents={"watcher": {"display_name": "Watcher", "tools": ["script", "calculator"]}},
            defaults={"tools": [], "worker_scope": "user_agent"},
        )
    elif change == "knowledge_mount":
        runtime.config_provider = lambda: Config(
            agents={
                "watcher": {
                    "display_name": "Watcher",
                    "tools": ["script", "calculator"],
                    "knowledge_bases": ["documents"],
                },
            },
            knowledge_bases={"documents": {"path": "knowledge/documents"}},
            defaults={"tools": []},
        )
    elif change == "worker_credentials":
        runtime.config_provider = lambda: Config(
            agents={"watcher": {"display_name": "Watcher", "tools": ["script", "calculator"]}},
            defaults={"tools": [], "worker_grantable_credentials": ["github"]},
        )
    elif change == "script_tool":
        config = _config()
        config.agents["watcher"].tools = ["calculator"]
        runtime.config_provider = lambda: config
    elif change == "gateway_isolation":
        runtime.runtime_paths = _runtime_paths(tmp_path)
    elif change == "explicit_gateway":
        runtime.runtime_paths = _runtime_paths(
            tmp_path,
            MINDROOM_SANDBOX_EXECUTION_MODE="all",
            MINDROOM_SCRIPT_GATEWAY_ISOLATED="true",
            MINDROOM_PUBLIC_URL="http://primary.test",
        )
    else:
        runtime.bind_api("http://different-primary.test/api/script-gateway")
    try:
        await runtime.start()
        assert runtime.store.get_run(run.run_id).state is ScriptRunState.INTERRUPTED
        assert client.exited is True
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_startup_keeps_unreachable_recoverable_worker_pending(tmp_path: Path) -> None:
    """A temporary status outage must not revoke a surviving process or open the gateway."""
    runtime, run, backend, client = _recovery_scenario(tmp_path)
    runtime.manager.worker_client = _FailingStatusWorkerClient()
    try:
        await runtime.start()
        assert runtime.store.get_run(run.run_id).cancel_requested_at is None
        assert len(backend.handles) == 1
        assert runtime.broker._call_admission_open.is_set() is False
        runtime.manager.worker_client = client
        await runtime._complete_pass()
        assert runtime.broker._call_admission_open.is_set()
        assert runtime.store.get_run(run.run_id).state is ScriptRunState.RUNNING
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, None])
async def test_recovery_requires_live_owner_authorization(tmp_path: Path, authorized: bool | None) -> None:
    """Unresolved membership waits for a live bot; confirmed removal interrupts the process."""
    runtime, run, _backend, client = _recovery_scenario(tmp_path)
    resolver = _StartupAdmissionResolver(authorized=authorized)
    runtime.resolver = resolver
    try:
        await runtime.start()
        durable = runtime.store.get_run(run.run_id)
        if authorized is False:
            assert durable.state is ScriptRunState.INTERRUPTED
            assert client.exited is True
        else:
            assert durable.cancel_requested_at is None
            assert runtime.broker._call_admission_open.is_set() is False
            resolver.authorized = True
            await runtime._complete_pass()
            assert runtime.broker._call_admission_open.is_set()
            assert client.exited is False
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_startup_never_preserves_cancelled_or_expired_script(tmp_path: Path, expired: bool) -> None:
    """A recovery marker cannot override durable cancellation or the original runtime budget."""
    started_at = (datetime.now(UTC) - timedelta(days=2)).isoformat() if expired else None
    runtime, run, _backend, client = _recovery_scenario(tmp_path, started_at=started_at)
    if not expired:
        runtime.store.request_cancel(run.run_id, reason="Cancelled by owner.")
    try:
        await runtime.start()
        durable = runtime.store.get_run(run.run_id)
        assert durable.cancel_requested_at is not None
        assert durable.finished_at is not None
        assert client.exited is True
    finally:
        await runtime.shutdown()


def test_keepalive_failure_aborts_cleanup_for_active_scripts(tmp_path: Path) -> None:
    """Worker maintenance cannot proceed with stale idle timestamps after keepalive fails."""
    runtime, _run_record, backend, _client = _recovery_scenario(tmp_path)
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(backend, "touch_worker", MagicMock(side_effect=WorkerBackendError("temporary API failure")))
        with pytest.raises(WorkerBackendError, match="temporary API failure"):
            runtime.touch_live_workers(backend)


@pytest.mark.asyncio
async def test_startup_revokes_and_retires_legacy_running_process(tmp_path: Path) -> None:
    """A legacy run without a recovery contract retains interruption semantics."""
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
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
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
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        worker_lease_provider=lease_provider,
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
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        broker=_broker_lifecycle_stub(),
        manager=MagicMock(spec=ScriptRunManager),
        resolver=MagicMock(),
        config_provider=lambda: None,
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
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
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

    manager = _manager_stub(
        gateway_url="",
        request_revocation=store.request_cancel,
        revoke=AsyncMock(side_effect=lambda run_id, **_kwargs: store.get_run(run_id)),
        reconcile_revoked_process=AsyncMock(side_effect=lambda run_id: store.get_run(run_id)),
        reconcile_durable=AsyncMock(side_effect=lambda run_id, **_kwargs: store.get_run(run_id)),
    )
    acquisition_started = threading.Event()
    release_acquisition = threading.Event()

    def acquire_worker_backend(_locator: str | None) -> None:
        acquisition_started.set()
        assert release_acquisition.wait(timeout=SETTLE_TIMEOUT_SECONDS)

    runtime = _lifecycle(
        runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        resolver=resolver,
        worker_lease_provider=acquire_worker_backend,
    )
    app = FastAPI()
    app.include_router(script_gateway_router)
    bind_script_tool_broker(app, broker)
    runtime.bind_api("http://primary.test/api/script-gateway")
    startup = asyncio.create_task(runtime.start())

    try:
        assert await asyncio.to_thread(acquisition_started.wait, SETTLE_TIMEOUT_SECONDS)
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
        await asyncio.wait_for(startup, timeout=SETTLE_TIMEOUT_SECONDS)
        await runtime.shutdown()


def _interrupting_reconcile(store: ScriptRunStore) -> AsyncMock:
    """Finalize a revoked run as interrupted, as the manager does once its process and broker ownership are gone."""

    def finish(*, run_id: str, broker_revoked: bool) -> ScriptRunRecord:
        del broker_revoked
        return store.transition_run(run_id, state=ScriptRunState.INTERRUPTED, error="Interrupted by reload.")

    return AsyncMock(side_effect=finish)


@pytest.mark.asyncio
async def test_removed_agent_revokes_and_cancels_running_scripts(tmp_path: Path) -> None:
    """Removing the owning agent durably revokes its running script, then interrupts it."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)

    manager = _manager_stub(
        request_revocation=MagicMock(side_effect=store.request_cancel),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=_interrupting_reconcile(store),
    )
    old_config = _config()
    runtime = _lifecycle(runtime_paths, store=store, manager=manager, config_provider=lambda: old_config)
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

    manager = _manager_stub(
        request_revocation=MagicMock(side_effect=store.request_cancel),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=_interrupting_reconcile(store),
    )
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "tools": ["calculator"]}},
        defaults={"tools": []},
    )
    runtime = _lifecycle(runtime_paths, store=store, manager=manager, config_provider=lambda: old_config)

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
async def test_isolation_change_interrupts_running_script(tmp_path: Path) -> None:
    """An agent isolation change revokes and interrupts the agent's running script."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    manager = _manager_stub(
        request_revocation=MagicMock(return_value=run),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=_interrupting_reconcile(store),
    )
    old_config = _config()
    runtime = _lifecycle(runtime_paths, store=store, manager=manager, config_provider=lambda: old_config)

    await runtime.apply_update_plan(_plan(old_config, _config(private=True)))

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
    manager = _manager_stub(request_revocation=MagicMock())
    runtime = _lifecycle(runtime_paths, store=store, manager=manager, config_provider=lambda: old_config)
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

    def finish_interrupted(*, run_id: str, broker_revoked: bool) -> ScriptRunRecord:
        del broker_revoked
        return store.transition_run(
            run_id,
            state=ScriptRunState.INTERRUPTED,
            error="Plugin tools changed during configuration reload.",
        )

    manager = _manager_stub(
        request_revocation=MagicMock(side_effect=store.request_cancel),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(side_effect=finish_interrupted),
    )
    config = _config()
    runtime = _lifecycle(runtime_paths, store=store, manager=manager, config_provider=lambda: config)

    await runtime.interrupt_for_plugin_reload()

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
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(
            request_revocation=MagicMock(side_effect=store.request_cancel),
            revoke=AsyncMock(return_value=run),
            reconcile_revoked_process=AsyncMock(return_value=run),
            reconcile_durable=AsyncMock(return_value=run),
        ),
        config_provider=lambda: config,
    )

    with pytest.raises(RuntimeError, match="Plugin reload did not interrupt every active background script"):
        await runtime.interrupt_for_plugin_reload()


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

    manager = _manager_stub(
        begin_startup_reconciliation=begin_startup_reconciliation,
        end_startup_reconciliation=end_startup_reconciliation,
    )
    config = _config()
    runtime = _lifecycle(runtime_paths, manager=manager, config_provider=lambda: config)
    runtime._startup_cleanup_pending = True

    await runtime.interrupt_for_plugin_reload()
    await runtime.finish_reload()

    assert reconciliation_owners == 1
    await manager.end_startup_reconciliation()
    assert reconciliation_owners == 0


@pytest.mark.usefixtures("enforce_turn_authorization")
def test_live_resolver_uses_current_reply_membership_authorization(tmp_path: Path) -> None:
    """Run authority includes the bot's current grant-room membership index."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config(
        agents={
            "watcher": {
                "display_name": "Watcher",
                "tools": ["script"],
                "rooms": ["trusted"],
                "access": {"members_of_rooms": ["trusted"]},
            },
        },
        defaults={"tools": []},
    )
    memberships = MagicMock()
    memberships.is_allowed.return_value = False
    bot = SimpleNamespace(
        running=True,
        config=config,
        orchestrator=SimpleNamespace(agent_reply_memberships=memberships),
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
@pytest.mark.parametrize(
    ("authorized", "config"),
    [
        pytest.param(False, _config(), id="membership-lost"),
        pytest.param(None, Config(agents={}, defaults={"tools": []}), id="agent-removed"),
    ],
)
async def test_maintenance_interrupts_run_after_its_owner_loses_authority(
    tmp_path: Path,
    authorized: bool | None,
    config: Config,
) -> None:
    """Lost membership or a removed agent is durable revocation, even while the agent's bot is unavailable."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)

    async def reconcile_durable(*, run_id: str, broker_revoked: bool = False) -> ScriptRunRecord:
        assert broker_revoked is True
        return store.transition_run(run_id, state=ScriptRunState.INTERRUPTED)

    manager = _manager_stub(
        request_revocation=MagicMock(side_effect=store.request_cancel),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(side_effect=reconcile_durable),
    )
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=manager,
        resolver=SimpleNamespace(is_authorized=MagicMock(return_value=authorized)),
        config_provider=lambda: config,
    )

    await _reconcile_once(runtime)

    manager.request_revocation.assert_called_once()
    manager.revoke.assert_awaited_once()
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

    manager = _manager_stub(
        request_revocation=MagicMock(side_effect=store.request_cancel),
        revoke=AsyncMock(return_value=running),
        reconcile_durable=AsyncMock(return_value=running),
    )
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)  # type: ignore[arg-type]
    runtime = _lifecycle(runtime_paths, store=store, broker=broker, manager=manager, resolver=resolver)

    await _reconcile_once(runtime)

    durable = store.get_run(running.run_id)
    assert durable.cancel_requested_at is None
    manager.request_revocation.assert_not_called()
    manager.reconcile_durable.assert_awaited_once_with(run_id=running.run_id)
    with pytest.raises(ScriptRuntimeUnavailableError, match="temporarily unavailable"):
        broker.authenticate(running.run_id, f"Bearer {token}")


@pytest.mark.parametrize("authorization_result", [False, None])
@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_authorization_config_change_interrupts_unconfirmed_owner_before_commit(
    tmp_path: Path,
    *,
    authorization_result: bool | None,
) -> None:
    """Config publication requires confirmed continued authority for every active run.

    A revoked owner's run whose process is not stopped yet does not hold the reload back: the broker refuses its calls.
    """
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    current = Config(
        agents={
            "watcher": {
                "display_name": "Watcher",
                "tools": ["script"],
                "access": {"users": ["@alice:example.test"]},
            },
        },
        defaults={"tools": []},
    )
    updated = current.model_copy(
        update={
            "agents": {
                "watcher": current.agents["watcher"].model_copy(
                    update={"access": current.agents["watcher"].access.model_copy(update={"users": []})},
                ),
            },
        },
    )

    manager = _manager_stub(
        request_revocation=MagicMock(side_effect=store.request_cancel),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(return_value=run),
    )
    resolver = SimpleNamespace(is_authorized=MagicMock(return_value=authorization_result))
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=manager,
        resolver=resolver,
        config_provider=lambda: current,
    )

    await runtime.apply_update_plan(_plan(current, updated))

    resolver.is_authorized.assert_called_once_with(run, config=updated)
    assert store.get_run(run.run_id).cancel_requested_at is not None


@pytest.mark.asyncio
async def test_reload_keeps_an_active_worker_script_on_its_worker(tmp_path: Path) -> None:
    """A reload that leaves the script's process authority alone neither interrupts it nor replaces its worker lease."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run_pinned_to_worker(store, runtime_paths, run_id=f"script-{'a' * 32}")
    termination_client = _TerminatingWorkerClient()
    broker = ScriptToolBroker(store=store, runtime_resolver=_ApprovalSettlementResolver())
    backend = _Backend([_worker(run)])
    lease = _Lease(backend)
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=termination_client,  # type: ignore[arg-type]
        worker_backend=lease.manager,
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
    leases = iter((lease,))
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        broker=broker,
        manager=manager,
        config_provider=lambda: committed_config,
        worker_lease_provider=lambda _locator: next(leases),
    )

    await _reconcile_once(runtime)
    await runtime.apply_update_plan(_plan(old_config, new_config))
    committed_config = new_config
    await runtime.finish_reload()

    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is None
    assert durable.state is ScriptRunState.RUNNING
    assert termination_client.exited is False
    assert [action for action in backend.actions if not action.startswith("touch:")] == []
    assert lease.released is False
    assert runtime._current_worker_lease is lease


@pytest.mark.asyncio
async def test_reload_aborts_before_every_run_has_durable_revocation(tmp_path: Path) -> None:
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
        defaults={"tools": [], "worker_scope": "user_agent"},
    )
    manager = _manager_stub(
        request_revocation=request_revocation,
        revoke=AsyncMock(),
        reconcile_durable=AsyncMock(),
        gateway_url="http://primary.test/api/script-gateway",
    )
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=manager,
        config_provider=lambda: old_config,
        pass_concurrency=1,
    )

    with pytest.raises(RuntimeError, match="Background script cleanup did not durably revoke every active run"):
        await runtime.apply_update_plan(_plan(old_config, new_config))

    assert store.get_run(first.run_id).cancel_requested_at is None
    assert store.get_run(second.run_id).cancel_requested_at is not None


@pytest.mark.asyncio
async def test_hung_broker_cleanup_cannot_delay_process_exit_recording(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A broker obligation may hang while TERM/KILL and durable process truth complete."""
    resolver = _HangingApprovalSettlementResolver()
    runtime, store, run, lease, worker_client, old_config, new_config = _process_authority_change_scenario(
        tmp_path,
        run_id=f"script-{'1' * 32}",
        approval_resolver=resolver,
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
        await asyncio.wait_for(resolver.entered.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
        # Below the update's pass timeout, which would end the hung broker obligation on its own.
        assert await asyncio.to_thread(exit_recorded.wait, runtime.pass_timeout_seconds / 2)
        durable = store.get_run(run.run_id)
        assert durable.state is ScriptRunState.RUNNING
        assert durable.finished_at is not None
        assert durable.output == "terminated output"
        assert worker_client.cancel_forces == [False, True]
        assert lease.manager.actions == []
        assert update.done() is False
    finally:
        resolver.release.set()
        await asyncio.wait_for(update, timeout=SETTLE_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_a_reload_that_cannot_stop_a_script_whose_authority_it_changed_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker the primary cannot reach keeps the script running with what the reload revokes, so it does not publish."""
    runtime, store, run, lease, worker_client, old_config, new_config = _process_authority_change_scenario(
        tmp_path,
        run_id=f"script-{'4' * 32}",
        approval_resolver=_ApprovalSettlementResolver(),
    )

    async def unreachable(*_args: object, **_kwargs: object) -> object:
        message = "worker API unavailable"
        raise ScriptWorkerError(message)

    def cannot_retire(_worker_key: str) -> None:
        message = "cluster API unavailable"
        raise WorkerBackendError(message)

    monkeypatch.setattr(worker_client, "cancel", unreachable)
    monkeypatch.setattr(worker_client, "status", unreachable)
    monkeypatch.setattr(lease.manager, "retire_worker", cannot_retire)

    with pytest.raises(_ScriptRuntimeLifecycleError, match="did not stop every background script whose worker access"):
        await runtime.apply_update_plan(_plan(old_config, new_config))

    assert store.get_run(run.run_id).state is ScriptRunState.RUNNING


@pytest.mark.asyncio
async def test_transient_broker_failure_uses_successful_retry_for_process_authority_change(tmp_path: Path) -> None:
    """A successful current-state broker retry permits cleanup after a process-authority change."""
    resolver = _TransientApprovalSettlementResolver()
    runtime, store, run, lease, worker_client, old_config, new_config = _process_authority_change_scenario(
        tmp_path,
        run_id=f"script-{'3' * 32}",
        approval_resolver=resolver,
    )

    await runtime.apply_update_plan(_plan(old_config, new_config))

    durable = store.get_run(run.run_id)
    assert durable.state is ScriptRunState.INTERRUPTED
    assert durable.cancellation_reason == WORKER_CONFIGURATION_CHANGED
    assert durable.output == "terminated output"
    assert worker_client.cancel_forces == [False, True]
    assert resolver.settlement_attempts == 2
    assert lease.released is False


@pytest.mark.asyncio
async def test_reload_fails_while_a_revoked_run_stays_unfinalized(tmp_path: Path) -> None:
    """A run whose broker ownership stays open is not finalized, so the reload fails instead of publishing over it."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    run = _stored_run(store, runtime_paths)
    config = _config()

    manager = _manager_stub(
        request_revocation=MagicMock(side_effect=store.request_cancel),
        revoke=AsyncMock(
            side_effect=[
                ScriptRunManagerError("broker unavailable"),
                ScriptRunManagerError("broker still unavailable"),
            ],
        ),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=AsyncMock(return_value=run),
    )
    runtime = _lifecycle(runtime_paths, store=store, manager=manager, config_provider=lambda: config)

    with (
        capture_logs() as logs,
        pytest.raises(_ScriptRuntimeLifecycleError, match="did not stop every background script whose worker access"),
    ):
        await runtime.apply_update_plan(_plan(config, Config(agents={}, defaults={"tools": []})))

    assert store.get_run(run.run_id).cancel_requested_at is not None
    assert manager.revoke.await_count == 2
    assert [(log["event"], str(log["exc_info"])) for log in logs if log["event"].startswith("script_reload_")] == [
        ("script_reload_broker_revocation_pending", "broker still unavailable"),
        ("script_reload_initial_broker_revocation_failed", "broker unavailable"),
    ]
    manager.reconcile_revoked_process.assert_awaited_once_with(run_id=run.run_id)
    manager.reconcile_durable.assert_not_awaited()


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
    backend = _LaunchingBackend(runtime_paths=runtime_paths)
    client = _LaunchingWorkerClient()
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
    runtime = _lifecycle(runtime_paths, store=store, broker=broker, manager=manager, config_provider=lambda: old_config)
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
        assert release_create.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        original_create(run)

    monkeypatch.setattr(store, "create_run", blocked_create)
    launch = asyncio.create_task(manager.run(context, source="print('ok')\n"))
    assert await asyncio.to_thread(create_started.wait, SETTLE_TIMEOUT_SECONDS)
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
    await runtime.finish_reload()


def _fenced_reload_runtime(
    tmp_path: Path,
) -> tuple[ScriptRuntimeLifecycle, ScriptRunManager, MagicMock]:
    """Return a script runtime whose reload took the launch fence and closed script calls."""
    runtime_paths = _runtime_paths(tmp_path)
    broker = MagicMock()
    manager = ScriptRunManager(
        store=ScriptRunStore(runtime_paths),
        broker=broker,
        worker_client=MagicMock(),
        worker_backend=_Lease(_Backend([])).manager,
        gateway_url="http://primary.test/api/script-gateway",
    )
    runtime = _lifecycle(runtime_paths, broker=broker, manager=manager)
    runtime.bind_api("http://primary.test/api/script-gateway")
    return runtime, manager, broker


@pytest.mark.asyncio
async def test_reopened_script_calls_keep_new_launches_fenced_until_the_reload_finishes(tmp_path: Path) -> None:
    """Running scripts call tools again during a reload's tail, but a new launch waits for the published config."""
    runtime, manager, broker = _fenced_reload_runtime(tmp_path)
    await manager.begin_startup_reconciliation()
    runtime._reload_launch_fence_started = True

    await runtime.reopen_script_calls()

    broker.open_call_admission.assert_called_once_with()
    with pytest.raises(ScriptRunManagerError, match="reconciliation is in progress"):
        await manager._admit_launch()

    await runtime.finish_reload()

    assert manager._startup_reconciliation_owners == 0
    await manager._admit_launch()
    await manager._release_launch_admission()


@pytest.mark.asyncio
async def test_finish_reload_cancelled_while_waiting_for_the_refresh_lock_still_ends_the_fence(
    tmp_path: Path,
) -> None:
    """The reload's one final release cannot be lost to a cancellation that arrives before it holds the lock."""
    runtime, manager, broker = _fenced_reload_runtime(tmp_path)
    await manager.begin_startup_reconciliation()
    runtime._reload_launch_fence_started = True
    await runtime._worker_refresh_lock.acquire()

    completion = asyncio.create_task(runtime.finish_reload())
    for _ in range(3):
        await asyncio.sleep(0)
    completion.cancel()
    for _ in range(3):
        await asyncio.sleep(0)
    runtime._worker_refresh_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await completion

    assert runtime._reload_launch_fence_started is False
    assert manager._startup_reconciliation_owners == 0
    broker.open_call_admission.assert_called_once_with()


@pytest.mark.asyncio
async def test_cancelled_finish_reload_still_reopens_admission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation after the fence drain cannot strand call admission or the fence."""
    runtime, manager, broker = _fenced_reload_runtime(tmp_path)
    lease = _Lease(_Backend([]))
    runtime._publish_worker_lease(lease)
    await manager.begin_startup_reconciliation()
    runtime._reload_launch_fence_started = True
    fence_ended = asyncio.Event()
    finish_end_call = asyncio.Event()
    original_end = ScriptRunManager.end_startup_reconciliation

    async def observed_end(target: ScriptRunManager) -> None:
        await original_end(target)
        fence_ended.set()
        await finish_end_call.wait()

    monkeypatch.setattr(ScriptRunManager, "end_startup_reconciliation", observed_end)

    completion = asyncio.create_task(runtime.finish_reload())
    await asyncio.wait_for(fence_ended.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    completion.cancel()
    finish_end_call.set()
    with pytest.raises(asyncio.CancelledError):
        await completion

    assert runtime._current_worker_lease is lease
    assert runtime._reload_launch_fence_started is False
    assert manager._startup_reconciliation_owners == 0
    broker.open_call_admission.assert_called_once_with()
    assert lease.released is False


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

    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(reconcile_durable=reconcile_durable),
        worker_lease_provider=lambda _locator: _Lease(backend),
    )

    await _reconcile_once(runtime)

    assert backend.actions == [f"touch:{run.worker_key}", "reconcile:run-1"]


@pytest.mark.asyncio
async def test_failed_nested_preparation_keeps_the_earlier_reload_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed newer plan leaves the fence an earlier unfinished reload owns."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    manager = ScriptRunManager(
        store=store,
        broker=_mock_broker(),
        worker_client=MagicMock(),
        worker_backend=None,
        gateway_url="http://primary.test/api/script-gateway",
    )
    await manager.begin_startup_reconciliation()
    runtime = _lifecycle(runtime_paths, store=store, manager=manager)
    runtime._reload_launch_fence_started = True
    old_config = _config()
    new_config = Config(
        agents={"watcher": {"display_name": "Watcher", "role": "new", "tools": ["script", "calculator"]}},
        defaults={"tools": [], "worker_scope": "user_agent"},
    )
    monkeypatch.setattr(store, "list_runs", MagicMock(side_effect=ScriptRunStoreError("store unavailable")))

    with pytest.raises(ScriptRunStoreError, match="store unavailable"):
        await runtime.apply_update_plan(_plan(old_config, new_config))

    assert runtime._reload_launch_fence_started is True
    assert manager._startup_reconciliation_owners == 1


@pytest.mark.asyncio
async def test_one_unreachable_run_stays_retryable_without_blocking_later_reconciliation(tmp_path: Path) -> None:
    """A failed reconciliation of one run leaves it retryable and the rest of the pass continues."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    first = _stored_run(store, runtime_paths)
    second = _stored_run(store, runtime_paths, run_id="run-2")
    reconciled: list[str] = []

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        if run_id == first.run_id:
            message = "worker unavailable"
            raise ScriptRunManagerError(message)
        reconciled.append(run_id)
        return second

    runtime = _lifecycle(runtime_paths, store=store, manager=_manager_stub(reconcile_durable=reconcile_durable))

    await _reconcile_once(runtime)

    assert store.get_run(first.run_id).state is ScriptRunState.RUNNING
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

    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(reconcile_durable=reconcile_durable),
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

    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(reconcile_durable=reconcile_durable),
        worker_lease_provider=lambda _locator: _Lease(backend),
    )

    await _reconcile_once(runtime)

    assert reconciled == [run.run_id]


@pytest.mark.asyncio
async def test_reconciliation_pass_has_one_overall_deadline(tmp_path: Path) -> None:
    """A stuck worker operation cannot indefinitely block a maintenance pass."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    _stored_run(store, runtime_paths)
    entered = asyncio.Event()

    async def reconcile_durable(*, run_id: str) -> ScriptRunRecord:
        del run_id
        entered.set()
        await asyncio.Event().wait()
        message = "unreachable"
        raise AssertionError(message)

    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(reconcile_durable=reconcile_durable),
        pass_timeout_seconds=_LOADED_HOST_PASS_TIMEOUT_SECONDS,
    )

    # Far above the pass deadline, so only the pass's own deadline lets this return in time.
    await asyncio.wait_for(runtime._run_complete_pass(), timeout=SETTLE_TIMEOUT_SECONDS)

    assert entered.is_set()


@pytest.mark.asyncio
async def test_blocking_backend_provider_cannot_stall_the_event_loop_past_pass_deadline(tmp_path: Path) -> None:
    """Potentially blocking backend construction is off-loop and bounded by the pass."""
    loop = asyncio.get_running_loop()
    release_provider = threading.Event()
    provider_returned = threading.Event()
    provider_saw_loop_running: list[bool] = []

    def blocking_provider(_locator: str | None) -> None:
        provider_saw_loop_running.append(loop_runs_while_blocked(loop))
        # Only an off-loop provider holds the pass, since nothing could release one that blocks the loop.
        if provider_saw_loop_running[-1]:
            release_provider.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        provider_returned.set()

    runtime = _lifecycle(
        _runtime_paths(tmp_path),
        manager=_manager_stub(reconcile_durable=AsyncMock()),
        worker_lease_provider=blocking_provider,
        pass_timeout_seconds=0.02,
    )

    try:
        await _reconcile_once(runtime)
        # The pass deadline ended the pass while the provider still held it.
        assert not provider_returned.is_set()
    finally:
        release_provider.set()
    assert await asyncio.to_thread(provider_returned.wait, SETTLE_TIMEOUT_SECONDS)
    assert provider_saw_loop_running == [True]


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
        assert release_provider.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        return lease

    runtime = _lifecycle(
        runtime_paths,
        manager=_manager_stub(reconcile_durable=AsyncMock()),
        worker_lease_provider=slow_provider,
        pass_timeout_seconds=0.02,
    )
    await _reconcile_once(runtime)
    release_provider.set()
    runtime.pass_timeout_seconds = 1

    await _reconcile_once(runtime)

    assert calls == 1
    assert runtime.manager.worker_backend is lease.manager


@dataclass
class _ShutdownCountingManager:
    """A primary worker manager that reports each shutdown to the test's event loop."""

    loop: asyncio.AbstractEventLoop
    shut_down: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown_calls: int = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.loop.call_soon_threadsafe(self.shut_down.set)


def _lease_primary_worker_manager(runtime_paths: RuntimePaths) -> workers_runtime_module.PrimaryWorkerManagerLease:
    return workers_runtime_module.lease_primary_worker_manager(runtime_paths, proxy_url=None, proxy_token=None)


def _primary_worker_manager_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    runtime_paths: RuntimePaths,
    *,
    build: Callable[[object], WorkerBackend],
    worker_lease_provider: Callable[[str | None], object],
) -> ScriptRuntimeLifecycle:
    """Return a lifecycle leasing the real primary worker manager, which *build* constructs."""
    workers_runtime_module.reset_primary_worker_manager()
    monkeypatch.setattr(
        workers_runtime_module,
        "_configure_primary_worker_backend",
        lambda *_args, **_kwargs: ConfiguredWorkerBackend(
            signature=("test-generation",),
            cleanup_signature=("test-generation",),
            build=build,
        ),
    )
    return _lifecycle(
        runtime_paths,
        manager=_manager_stub(gateway_url="", reconcile_durable=AsyncMock()),
        worker_lease_provider=worker_lease_provider,
    )


async def _tear_down_primary_worker_manager(
    runtime: ScriptRuntimeLifecycle,
    acquisition: _PendingWorkerLease | None,
    *,
    release: threading.Event,
    manager: _ShutdownCountingManager,
    leased: asyncio.Event,
) -> None:
    """Shut everything down, release the blocked provider, and wait for the leased manager's shutdown."""
    pending_acquisition = acquisition or runtime._pending_worker_lease
    await runtime.shutdown(timeout_seconds=0.01)
    workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
    release.set()
    if pending_acquisition is not None:
        await asyncio.gather(pending_acquisition.task, return_exceptions=True)
    if leased.is_set():
        await manager.shut_down.wait()
    workers_runtime_module.reset_primary_worker_manager()


@pytest.mark.asyncio
async def test_cancelled_late_backend_build_cannot_publish_after_final_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Final shutdown fences a provider thread even after its asyncio owner is cancelled."""
    runtime_paths = _runtime_paths(tmp_path)
    loop = asyncio.get_running_loop()
    build_started = asyncio.Event()
    release_build = threading.Event()
    late_manager = _ShutdownCountingManager(loop)

    def build_manager(*_args: object, **_kwargs: object) -> WorkerBackend:
        loop.call_soon_threadsafe(build_started.set)
        assert release_build.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        return cast("WorkerBackend", late_manager)

    runtime = _primary_worker_manager_lifecycle(
        monkeypatch,
        runtime_paths,
        build=build_manager,
        worker_lease_provider=lambda _locator: _lease_primary_worker_manager(runtime_paths),
    )
    acquisition = None

    try:
        await runtime.start()
        runtime.bind_api("http://primary.test/api/script-gateway")
        await build_started.wait()
        acquisition = runtime._pending_worker_lease
        assert acquisition is not None
        await runtime.shutdown(timeout_seconds=0.01)
        acquisition.task.cancel()
        with suppress(asyncio.CancelledError):
            await acquisition.task

        workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
        release_build.set()

        await late_manager.shut_down.wait()
        assert workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY is None
        assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES == []
        assert late_manager.shutdown_calls == 1

        workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
        assert late_manager.shutdown_calls == 1
    finally:
        await _tear_down_primary_worker_manager(
            runtime,
            acquisition,
            release=release_build,
            manager=late_manager,
            leased=build_started,
        )


@pytest.mark.asyncio
async def test_cancelled_published_worker_lease_handoff_releases_after_final_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation after publication cannot strand the executor-owned lease delivery."""
    runtime_paths = _runtime_paths(tmp_path)
    loop = asyncio.get_running_loop()
    lease_published = asyncio.Event()
    release_provider = threading.Event()
    published_manager = _ShutdownCountingManager(loop)

    def lease_provider(_locator: str | None) -> workers_runtime_module.PrimaryWorkerManagerLease:
        lease = _lease_primary_worker_manager(runtime_paths)
        loop.call_soon_threadsafe(lease_published.set)
        assert release_provider.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        return lease

    runtime = _primary_worker_manager_lifecycle(
        monkeypatch,
        runtime_paths,
        build=lambda _current_launch: cast("WorkerBackend", published_manager),
        worker_lease_provider=lease_provider,
    )
    acquisition = None

    try:
        await runtime.start()
        runtime.bind_api("http://primary.test/api/script-gateway")
        await lease_published.wait()
        acquisition = runtime._pending_worker_lease
        assert acquisition is not None

        workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
        with workers_runtime_module._PRIMARY_WORKER_MANAGER_CONDITION:
            assert workers_runtime_module._PRIMARY_WORKER_MANAGER_ENTRY is None
            assert len(workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES) == 1
            assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES[0].active_leases == 1

        await runtime.shutdown(timeout_seconds=0.01)
        acquisition.task.cancel()
        with suppress(asyncio.CancelledError):
            await acquisition.task
        release_provider.set()

        await published_manager.shut_down.wait()
        assert workers_runtime_module._RETIRED_PRIMARY_WORKER_MANAGER_ENTRIES == []
        assert published_manager.shutdown_calls == 1

        workers_runtime_module.shutdown_primary_worker_manager(timeout_seconds=0.0)
        assert published_manager.shutdown_calls == 1
    finally:
        await _tear_down_primary_worker_manager(
            runtime,
            acquisition,
            release=release_provider,
            manager=published_manager,
            leased=lease_published,
        )


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
        # No bound: the finally below always releases it, and a release timeout would end the blocked release and
        # let a shutdown that wrongly waits for it finish.
        release_lease.wait()
        lease.released = True
        lease_released.set()

    lease.release = blocking_release  # type: ignore[method-assign]
    runtime = _lifecycle(
        _runtime_paths(tmp_path),
        broker=_broker_lifecycle_stub(),
        manager=_manager_stub(),
    )
    runtime._publish_worker_lease(lease)

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
        await asyncio.wait_for(reconciliation_started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
        # Below shutdown's default budget, so a shutdown that drops its explicit budget fails.
        await asyncio.wait_for(shutdown, timeout=_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS / 2)
        assert await asyncio.to_thread(release_started.wait, SETTLE_TIMEOUT_SECONDS)
        assert lease_released.is_set() is False
    finally:
        release_lease.set()

    assert await asyncio.to_thread(lease_released.wait, SETTLE_TIMEOUT_SECONDS)
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
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        broker=_broker_lifecycle_stub(),
        manager=_manager_stub(
            begin_shutdown=begin_shutdown,
        ),
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
            assert release_revocations.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        return store.request_cancel(run_id, reason=reason)

    def observe_release() -> None:
        release_observations.append(
            {run.run_id for run in runs if store.get_run(run.run_id).cancel_requested_at is not None},
        )
        lease_released.set()

    lease = _Lease(_Backend([_worker(run) for run in runs]), on_release=observe_release)
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        broker=_broker_lifecycle_stub(),
        manager=_manager_stub(
            worker_backend=lease.manager,
            request_revocation=request_revocation,
            revoke=AsyncMock(),
            reconcile_durable=AsyncMock(),
        ),
        pass_concurrency=1,
    )
    runtime._publish_worker_lease(lease)

    shutdown = asyncio.create_task(runtime.shutdown(timeout_seconds=0.1))
    try:
        assert await asyncio.to_thread(first_revocation_started.wait, SETTLE_TIMEOUT_SECONDS)
        assert await asyncio.to_thread(lease_released.wait, 0.2) is False
        release_revocations.set()
        await asyncio.wait_for(shutdown, timeout=SETTLE_TIMEOUT_SECONDS)
        assert await asyncio.to_thread(lease_released.wait, SETTLE_TIMEOUT_SECONDS)
    finally:
        release_revocations.set()
        if not shutdown.done():
            await asyncio.wait_for(shutdown, timeout=SETTLE_TIMEOUT_SECONDS)

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
            assert release_revocations.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        return store.request_cancel(run_id, reason=reason)

    lease = _Lease(_Backend([_worker(run) for run in runs]))
    manager = _manager_stub(
        worker_backend=lease.manager,
        request_revocation=request_revocation,
        revoke=AsyncMock(),
        reconcile_durable=AsyncMock(),
    )
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        broker=_broker_lifecycle_stub(),
        manager=manager,
        pass_concurrency=1,
    )
    runtime._publish_worker_lease(lease)

    shutdown = asyncio.create_task(runtime.shutdown(timeout_seconds=0.1))
    try:
        assert await asyncio.to_thread(first_revocation_started.wait, SETTLE_TIMEOUT_SECONDS)
        shutdown.cancel()
        done, _pending = await asyncio.wait({shutdown}, timeout=0.1)
        assert done == set()
        release_revocations.set()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
    finally:
        release_revocations.set()

    assert all(store.get_run(run.run_id).cancel_requested_at is not None for run in runs)
    assert await asyncio.to_thread(lease.release_event.wait, SETTLE_TIMEOUT_SECONDS)
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
    manager = _manager_stub(
        worker_backend=lease.manager,
        request_revocation=request_revocation,
        revoke=AsyncMock(),
        reconcile_durable=AsyncMock(),
    )
    runtime = _lifecycle(runtime_paths, store=store, broker=_broker_lifecycle_stub(), manager=manager)
    runtime._publish_worker_lease(lease)

    with pytest.raises(_ScriptRuntimeLifecycleError, match="durably revoke every active run"):
        await runtime.shutdown(timeout_seconds=0.1)

    assert store.get_run(run.run_id).cancel_requested_at is None
    assert lease.released is True
    assert runtime._current_worker_lease is None
    assert manager.worker_backend is None
    manager.begin_shutdown.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_interrupts_active_run_before_releasing_worker_lease(tmp_path: Path) -> None:
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
    runtime = _lifecycle(runtime_paths, store=store, broker=broker, manager=manager)
    runtime._publish_worker_lease(lease)

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
    runtime = _lifecycle(runtime_paths, store=store, broker=broker, manager=manager)
    runtime._publish_worker_lease(lease)

    # The status call never returns, so shutdown runs to its deadline; this bound sits below shutdown's default
    # budget, so a shutdown that drops its explicit budget fails.
    await asyncio.wait_for(runtime.shutdown(timeout_seconds=1.0), timeout=_DEFAULT_SHUTDOWN_TIMEOUT_SECONDS / 2)

    assert status_started.is_set()
    durable = store.get_run(run.run_id)
    assert durable.cancel_requested_at is not None
    assert durable.state is ScriptRunState.RUNNING

    assert await asyncio.to_thread(lease.release_event.wait, SETTLE_TIMEOUT_SECONDS)
    assert lease.released is True


@pytest.mark.asyncio
async def test_shutdown_interrupts_a_local_run_without_acquiring_a_worker_backend(tmp_path: Path) -> None:
    """An unsafe-local run is revoked and interrupted at shutdown without any worker backend."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    created = store.create_run(
        replace(
            _run(runtime_paths, state=ScriptRunState.STARTING),
            local_unsafe=True,
            worker_key=None,
            worker_id=None,
            worker_backend_locator=None,
        ),
    )
    run = store.transition_run(created.run_id, state=ScriptRunState.RUNNING)
    lease_provider = MagicMock(return_value=None)
    manager = _manager_stub(
        request_revocation=MagicMock(side_effect=store.request_cancel),
        revoke=AsyncMock(return_value=run),
        reconcile_revoked_process=AsyncMock(return_value=run),
        reconcile_durable=_interrupting_reconcile(store),
    )
    runtime = _lifecycle(runtime_paths, store=store, manager=manager, worker_lease_provider=lease_provider)

    await runtime.shutdown()

    assert store.get_run(run.run_id).state is ScriptRunState.INTERRUPTED
    manager.reconcile_durable.assert_awaited_once_with(run_id=run.run_id, broker_revoked=True)
    lease_provider.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_releases_a_delivered_lease_no_refresh_acknowledged(tmp_path: Path) -> None:
    """A lease the provider delivered after its waiter gave up is still released at shutdown."""
    lease = _Lease(_Backend([]))
    runtime = _lifecycle(
        _runtime_paths(tmp_path),
        manager=_manager_stub(),
        worker_lease_provider=lambda _locator: lease,
    )
    acquisition = runtime._get_or_create_worker_lease_acquisition(None)
    await asyncio.wait_for(acquisition.task, timeout=SETTLE_TIMEOUT_SECONDS)

    await runtime.shutdown()

    assert await asyncio.to_thread(lease.release_event.wait, SETTLE_TIMEOUT_SECONDS)
    assert runtime._pending_worker_lease is None


@pytest.mark.asyncio
async def test_shutdown_reports_an_acquisition_that_failed_after_its_waiter_gave_up(tmp_path: Path) -> None:
    """A provider failure nobody awaited is logged when shutdown discards its acquisition."""

    def failing_provider(_locator: str | None) -> None:
        message = "provider failed"
        raise WorkerBackendError(message)

    runtime = _lifecycle(_runtime_paths(tmp_path), manager=_manager_stub(), worker_lease_provider=failing_provider)
    acquisition = runtime._get_or_create_worker_lease_acquisition(None)
    await asyncio.gather(acquisition.task, return_exceptions=True)

    with capture_logs() as logs:
        await runtime.shutdown()

    assert [str(log["exc_info"]) for log in logs if log["event"] == "script_worker_backend_pending_acquire_failed"] == [
        "provider failed",
    ]
    assert runtime._pending_worker_lease is None


@pytest.mark.asyncio
async def test_shutdown_before_activation_releases_committed_worker_lease(tmp_path: Path) -> None:
    """A committed generation must not survive shutdown just because activation never ran."""
    lease = _Lease(_Backend([]))
    runtime = _lifecycle(
        _runtime_paths(tmp_path),
        broker=_broker_lifecycle_stub(),
        manager=_manager_stub(
            worker_backend=lease.manager,
        ),
    )
    runtime._publish_worker_lease(lease)

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
        # No bound: the finally below always releases it, and a release timeout would end the blocked release and
        # let a release helper that wrongly waits for it return.
        release_lease.wait()
        lease.released = True
        lease_released.set()

    lease.release = blocking_release  # type: ignore[method-assign]
    try:
        await asyncio.wait_for(
            _release_worker_leases_before_deadline(
                [lease],
                deadline=asyncio.get_running_loop().time(),
                timeout_seconds=0.05,
            ),
            timeout=SETTLE_TIMEOUT_SECONDS,
        )
        assert await asyncio.to_thread(release_started.wait, SETTLE_TIMEOUT_SECONDS)
        assert lease_released.is_set() is False
    finally:
        release_lease.set()

    assert await asyncio.to_thread(lease_released.wait, SETTLE_TIMEOUT_SECONDS)
    assert lease.released is True


@pytest.mark.asyncio
async def test_blocking_retired_lease_release_cannot_stall_reconciliation(tmp_path: Path) -> None:
    """Retired backend disposal remains off-loop and inside the pass deadline."""
    runtime_paths = _runtime_paths(tmp_path)
    release_lease = threading.Event()
    retired = _Lease(_Backend([]))
    release_started = threading.Event()

    def blocking_release() -> None:
        release_started.set()
        assert release_lease.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        retired.released = True

    retired.release = blocking_release  # type: ignore[method-assign]
    runtime = _lifecycle(
        runtime_paths,
        manager=_manager_stub(reconcile_durable=AsyncMock()),
        worker_lease_provider=lambda _locator: _Lease(_Backend([])),
        pass_timeout_seconds=_LOADED_HOST_PASS_TIMEOUT_SECONDS,
    )
    runtime._publish_worker_lease(retired)
    # The pass that finishes startup cleanup retires the lease no adopted run needs.
    runtime._startup_cleanup_pending = True
    try:
        # Far above the pass deadline, so only the pass's own deadline lets this return in time.
        await asyncio.wait_for(runtime._run_complete_pass(), timeout=SETTLE_TIMEOUT_SECONDS)
        assert await asyncio.to_thread(release_started.wait, SETTLE_TIMEOUT_SECONDS)
        assert retired.released is False
    finally:
        release_lease.set()


@pytest.mark.asyncio
async def test_cancelled_reload_preserves_completed_durable_and_broker_revocation(tmp_path: Path) -> None:
    """Cancellation at stuck process reconciliation preserves prior revocation and clears the launch fence."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    first = _stored_run(store, runtime_paths)
    second = _stored_run(store, runtime_paths, run_id="run-2")
    never = asyncio.Event()
    broker_revoked = asyncio.Event()
    process_reconciliation_started = asyncio.Event()
    broker_revocations: list[str] = []
    end_startup_reconciliation = AsyncMock()

    async def revoke(run_id: str, *, reason: str) -> ScriptRunRecord:
        assert store.get_run(first.run_id).cancel_requested_at is not None
        assert store.get_run(second.run_id).cancel_requested_at is not None
        revoked = store.request_cancel(run_id, reason=reason)
        broker_revocations.append(run_id)
        if set(broker_revocations) == {first.run_id, second.run_id}:
            broker_revoked.set()
        return revoked

    async def reconcile_revoked_process(*, run_id: str) -> ScriptRunRecord:
        if run_id == first.run_id:
            process_reconciliation_started.set()
            await never.wait()
        return store.get_run(run_id)

    async def reconcile_durable(*, run_id: str, broker_revoked: bool = False) -> ScriptRunRecord:
        assert broker_revoked is True
        return store.get_run(run_id)

    current = _config()
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(
            end_startup_reconciliation=end_startup_reconciliation,
            request_revocation=store.request_cancel,
            revoke=revoke,
            reconcile_revoked_process=reconcile_revoked_process,
            reconcile_durable=reconcile_durable,
        ),
        config_provider=lambda: current,
    )

    reload_task = asyncio.create_task(runtime.apply_update_plan(_plan(current, Config(defaults={"tools": []}))))
    try:
        await broker_revoked.wait()
        await process_reconciliation_started.wait()
        assert runtime._reload_launch_fence_started is True
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

        assert store.get_run(first.run_id).cancel_requested_at is not None
        assert store.get_run(second.run_id).cancel_requested_at is not None
        assert set(broker_revocations) == {first.run_id, second.run_id}
        assert runtime._reload_launch_fence_started is False
        end_startup_reconciliation.assert_awaited_once()
    finally:
        reload_task.cancel()
        await asyncio.gather(reload_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reload_timeout_aborts_when_durable_revocation_exceeds_the_overall_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline cannot return while an accepted durable revocation is still running."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    _stored_run(store, runtime_paths)
    broker_revocations: list[str] = []
    accepted = threading.Event()
    release = threading.Event()

    def held_request_revocation(run_id: str, *, reason: str) -> ScriptRunRecord:
        accepted.set()
        release.wait(timeout=SETTLE_TIMEOUT_SECONDS)
        return store.request_cancel(run_id, reason=reason)

    async def revoke(run_id: str, *, reason: str) -> ScriptRunRecord:
        del reason
        broker_revocations.append(run_id)
        return store.get_run(run_id)

    current = _config()
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(
            request_revocation=held_request_revocation,
            revoke=revoke,
            reconcile_durable=AsyncMock(),
        ),
        config_provider=lambda: current,
        pass_timeout_seconds=0.05,
    )
    boundaries: list[asyncio.Task[None]] = []
    wait_for = asyncio.wait_for

    async def deadline_from_acceptance(awaitable: Awaitable[object], timeout: float | None) -> object:  # noqa: ASYNC109
        if not (inspect.iscoroutine(awaitable) and awaitable.cr_code.co_name == "apply_update_boundary"):
            return await wait_for(awaitable, timeout)
        boundary = asyncio.ensure_future(awaitable)
        boundaries.append(boundary)
        # The reload's pass deadline starts only once the revocation is accepted, however slow the runner.
        assert await asyncio.to_thread(accepted.wait, SETTLE_TIMEOUT_SECONDS), "the reload never accepted a revocation"
        return await wait_for(boundary, timeout)

    monkeypatch.setattr(asyncio, "wait_for", deadline_from_acceptance)

    async def release_after_the_deadline() -> None:
        # The expired deadline is cancelling the reload while the accepted revocation still holds it.
        try:
            async with asyncio.timeout(SETTLE_TIMEOUT_SECONDS):
                while not (boundaries and boundaries[0].cancelling()):  # noqa: ASYNC110
                    await asyncio.sleep(0.005)
        except TimeoutError:
            if not boundaries:
                pytest.fail("the reload never reached apply_update_boundary through asyncio.wait_for")
            raise
        # Give the cancellation loop turns to propagate; only the shield keeps the reload waiting through them.
        for _ in range(20):
            await asyncio.sleep(0)
        assert not boundaries[0].done(), "the reload stopped waiting for its accepted revocation"
        release.set()

    releaser = asyncio.create_task(release_after_the_deadline())
    try:
        with pytest.raises(RuntimeError, match="Background script reload did not durably revoke every active run"):
            await runtime.apply_update_plan(_plan(current, Config(defaults={"tools": []})))
        # The accepted revocation finished before the reload returned.
        assert store.get_run("run-1").cancel_requested_at is not None
    finally:
        release.set()
        await releaser

    assert broker_revocations == []


@pytest.mark.asyncio
async def test_startup_pruning_is_inside_one_complete_pass_deadline(tmp_path: Path) -> None:
    """The pass that finishes startup cleanup returns at its deadline even when retention cleanup is unavailable."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    running = _stored_run(store, runtime_paths)
    terminal = store.transition_run(running.run_id, state=ScriptRunState.EXITED, exit_code=0)
    assert terminal.finished_at is not None
    pruning = asyncio.Event()

    async def prune_approvals(_run_id: str) -> bool:
        pruning.set()
        await asyncio.Event().wait()
        return False

    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(gateway_url="", reconcile_durable=AsyncMock()),
        resolver=SimpleNamespace(is_authorized=MagicMock(return_value=True), prune_approvals=prune_approvals),
        retention_seconds=0.001,
        pass_timeout_seconds=_LOADED_HOST_PASS_TIMEOUT_SECONDS,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")
    runtime._startup_cleanup_pending = True

    # Far above the pass deadline, so only the pass's own deadline lets this return in time.
    await asyncio.wait_for(runtime._run_complete_pass(), timeout=SETTLE_TIMEOUT_SECONDS)

    assert runtime._startup_cleanup_pending is False
    assert pruning.is_set()


@pytest.mark.asyncio
async def test_pruning_has_an_overall_deadline(tmp_path: Path) -> None:
    """A stuck approval cleanup cannot leave a maintenance pass's retention cleanup unbounded."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    running = _stored_run(store, runtime_paths)
    terminal = store.transition_run(running.run_id, state=ScriptRunState.EXITED, exit_code=0)
    assert terminal.finished_at is not None
    pruning = asyncio.Event()

    async def prune_approvals(_run_id: str) -> bool:
        pruning.set()
        await asyncio.Event().wait()
        return False

    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(),
        resolver=SimpleNamespace(is_authorized=MagicMock(return_value=True), prune_approvals=prune_approvals),
        retention_seconds=0.001,
        pass_timeout_seconds=_LOADED_HOST_PASS_TIMEOUT_SECONDS,
    )

    # Far above the pass deadline, so only the pass's own deadline lets this return in time.
    await asyncio.wait_for(runtime._run_complete_pass(), timeout=SETTLE_TIMEOUT_SECONDS)

    assert pruning.is_set()


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

    manager = _manager_stub(
        gateway_url="",
        reconcile_durable=reconcile_durable,
        request_revocation=store.request_cancel,
    )
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=manager,
        reconcile_interval_seconds=0.01,
        # Well above a pass on a loaded host: a pass that times out never reaches the reconcile this test counts.
        pass_timeout_seconds=5.0,
    )
    runtime.bind_api("http://primary.test/api/script-gateway")

    await runtime.start()
    _stored_run(store, runtime_paths)
    await asyncio.wait_for(recovered.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
    monkeypatch.setattr(ScriptRuntimeLifecycle, "_interrupt_and_prune_for_shutdown", AsyncMock())
    await runtime.shutdown()

    assert calls >= 3


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
    runtime = _lifecycle(runtime_paths, store=store, manager=_manager_stub(), retention_seconds=60.0)
    finished_at = datetime.fromisoformat(terminal.finished_at)

    await _prune_once(runtime, now=finished_at + timedelta(seconds=59))
    assert store.get_run(run_id).state is ScriptRunState.EXITED

    await _prune_once(runtime, now=finished_at + timedelta(seconds=61))
    with pytest.raises(ScriptRunNotFoundError):
        store.get_run(run_id)


def _built_runtime(
    runtime_paths: RuntimePaths,
    *,
    bot_provider: Callable[[str], object] = lambda _name: None,
    worker_lease_provider: Callable[[str | None], object] = lambda _locator: None,
) -> ScriptRuntimeLifecycle:
    """Build the production script runtime, with no live bot or worker lease unless given providers for them."""
    return build_script_runtime(
        runtime_paths,
        config_provider=_config,
        bot_provider=bot_provider,
        worker_lease_provider=worker_lease_provider,
        api_enabled=True,
    )


def _origin() -> BackgroundScriptToolOrigin:
    return BackgroundScriptToolOrigin(
        run_id="run-1",
        call_id="call-1",
        requester_id="@alice:example.test",
        toolkit_name="calculator",
        function_name="add",
    )


def _approval_context(run: ScriptRunRecord, runtime_paths: RuntimePaths) -> ToolRuntimeContext:
    """Return the context fields an approval request for *run* reads."""
    return cast(
        "ToolRuntimeContext",
        SimpleNamespace(
            requester_id=run.owner_user_id,
            room_id=run.room_id,
            resolved_thread_id=run.thread_root_event_id,
            current_config=_config(),
            runtime_paths=runtime_paths,
        ),
    )


@pytest.mark.parametrize("bot_state", ["missing", "stopped", "without_context"])
def test_live_resolver_reports_a_restarting_agent_runtime(tmp_path: Path, bot_state: str) -> None:
    """A run's calls wait while its agent's bot is missing, stopped, or cannot build a tool context yet."""
    runtime_paths = _runtime_paths(tmp_path)
    support = SimpleNamespace(
        build_context=MagicMock(return_value=None if bot_state == "without_context" else SimpleNamespace()),
    )
    bot = (
        None
        if bot_state == "missing"
        else SimpleNamespace(running=bot_state != "stopped", tool_runtime_support=support)
    )
    runtime = _built_runtime(runtime_paths, bot_provider=lambda _name: bot)

    with pytest.raises(_ScriptRuntimeUnavailableError, match="Agent runtime 'watcher' is restarting"):
        runtime.resolver.resolve(_run(runtime_paths), correlation_id="run-1:call-1")


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
    bot = SimpleNamespace(agent_name="watcher", running=True, tool_runtime_support=support)
    backend = _Backend([_worker(run)])
    approvals = SimpleNamespace(
        request_background_approval=AsyncMock(
            return_value=BackgroundApprovalDecision(status="approved", reason="operator decision"),
        ),
    )
    runtime = _built_runtime(
        runtime_paths,
        bot_provider=lambda _name: bot,
        worker_lease_provider=lambda _locator: _Lease(backend),
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
        origin=_origin(),
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


@pytest.mark.asyncio
async def test_live_resolver_approves_without_a_card_inside_the_requesters_auto_approve_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A background script call of the requester's auto-approve thread runs without publishing an approval card."""
    runtime_paths = _runtime_paths(tmp_path)
    run = _run(runtime_paths)
    approvals = SimpleNamespace(request_background_approval=AsyncMock())
    runtime = _built_runtime(runtime_paths)
    resolver = runtime.resolver
    resolver.approval_provider = lambda: approvals
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.resolve_tool_approval_approver",
        lambda *_args: run.owner_user_id,
    )
    start_auto_approve(
        runtime_paths,
        room_id=run.room_id,
        thread_id=run.thread_root_event_id,
        requester_id=run.owner_user_id,
        duration=timedelta(minutes=5),
    )

    decision = await resolver.request_approval(
        origin=_origin(),
        context=_approval_context(run, runtime_paths),
        grant=ScriptToolGrant("calculator", "add"),
        arguments={"a": 1, "b": 2},
        timeout_seconds=30.0,
    )

    assert decision == ToolApprovalDecision(approved=True)
    approvals.request_background_approval.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_approver", "router_in_room", "reason"),
    [(False, True, NO_APPROVER_REASON), (True, False, DEFAULT_ROUTER_MANAGED_ROOM_REASON)],
)
async def test_live_resolver_denies_a_call_no_one_can_answer_without_a_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    has_approver: bool,
    router_in_room: bool,
    reason: str,
) -> None:
    """A script call no one may approve, or one in a room without the router, is denied as every approval kind is."""
    runtime_paths = _runtime_paths(tmp_path)
    run = _run(runtime_paths)
    approvals = SimpleNamespace(request_background_approval=AsyncMock())
    runtime = _built_runtime(runtime_paths)
    resolver = runtime.resolver
    resolver.approval_provider = lambda: approvals
    monkeypatch.setattr(
        "mindroom.orchestration.script_runtime.resolve_tool_approval_approver",
        lambda *_args: run.owner_user_id if has_approver else None,
    )
    initialize_approval_store_for_test(runtime_paths, router_missing=lambda _room_id: not router_in_room)

    decision = await resolver.request_approval(
        origin=_origin(),
        context=_approval_context(run, runtime_paths),
        grant=ScriptToolGrant("calculator", "add"),
        arguments={"a": 1, "b": 2},
        timeout_seconds=30.0,
    )

    assert decision == ToolApprovalDecision(approved=False, reason=reason)
    approvals.request_background_approval.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_reload_drain_restores_broker_and_launch_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after fence acquisition cannot strand either admission gate."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    resolver = _StartupAdmissionResolver()
    broker = ScriptToolBroker(store=store, runtime_resolver=resolver)
    broker.open_call_admission()
    manager = ScriptRunManager(
        store=store,
        broker=broker,
        worker_client=MagicMock(),
        worker_backend=None,
        gateway_url="http://primary.test/api/script-gateway",
    )
    runtime = _lifecycle(runtime_paths, store=store, broker=broker, manager=manager, resolver=resolver)
    admitted_launch_released = False
    reload_task: asyncio.Task[None] | None = None
    await manager._admit_launch()

    drain_started = asyncio.Event()
    wait_for_drain = manager._launches_drained.wait

    async def observe_drain() -> bool:
        drain_started.set()
        return await wait_for_drain()

    monkeypatch.setattr(manager._launches_drained, "wait", observe_drain)

    try:
        reload_task = asyncio.create_task(
            runtime.interrupt_for_plugin_reload(),
        )
        await asyncio.wait_for(drain_started.wait(), timeout=SETTLE_TIMEOUT_SECONDS)
        reload_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reload_task

        await manager._release_launch_admission()
        admitted_launch_released = True

        assert manager._startup_reconciliation_owners == 0
        assert broker._call_admission_open.is_set()
        await manager._admit_launch()
        await manager._release_launch_admission()
    finally:
        if reload_task is not None and not reload_task.done():
            reload_task.cancel()
            await asyncio.gather(reload_task, return_exceptions=True)
        if not admitted_launch_released:
            await manager._release_launch_admission()
        while manager._startup_reconciliation_owners:
            await manager.end_startup_reconciliation()


@pytest.mark.asyncio
@pytest.mark.parametrize("addresses", [[(2, 1, 6, "", ("0.0.0.0", 8765))], []])  # noqa: S104
async def test_worker_gateway_rejects_unspecified_and_unresolvable_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    addresses: list[object],
) -> None:
    """A gateway that resolves to no address or to the unspecified address is unreachable from a worker."""
    runtime_paths = _runtime_paths(
        tmp_path,
        **_STATIC_RUNNER_ENV,
        MINDROOM_SCRIPT_GATEWAY_URL="http://gateway.test:8765/api/script-gateway",
    )
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: addresses)

    with pytest.raises(ValueError, match="non-loopback"):
        await _script_gateway_url(runtime_paths, host="0.0.0.0", port=8765)  # noqa: S104


@pytest.mark.asyncio
async def test_reconciliation_touches_only_workers_the_published_backend_owns(tmp_path: Path) -> None:
    """Maintenance keeps alive only the workers of runs the current backend owns, not another's or an unrecorded one."""
    runtime_paths = _runtime_paths(tmp_path)
    store = ScriptRunStore(runtime_paths)
    owned = _stored_run_pinned_to_worker(store, runtime_paths, run_id=f"script-{'1' * 32}")
    for run_id, locator in ((f"script-{'2' * 32}", "locator-b"), (f"script-{'3' * 32}", None)):
        _stored_run_pinned_to_worker(store, runtime_paths, run_id=run_id, worker_backend_locator=locator)
    backend = _Backend([])
    runtime = _lifecycle(
        runtime_paths,
        store=store,
        manager=_manager_stub(reconcile_durable=AsyncMock()),
        worker_lease_provider=lambda _locator: _Lease(backend),
    )

    await _reconcile_once(runtime)

    assert backend.actions == [f"touch:{owned.worker_key}"]


def test_live_resolver_finds_no_worker_through_a_backend_that_does_not_own_the_run(tmp_path: Path) -> None:
    """A run another backend owns has no live worker in the published backend."""
    runtime_paths = _runtime_paths(tmp_path)
    run = replace(_run(runtime_paths), worker_backend_locator="locator-b")
    runtime = _built_runtime(runtime_paths)
    runtime.manager.worker_backend = _Backend([_worker(run)])
    context = SimpleNamespace(agent_name="watcher", current_config=_config(), runtime_paths=runtime_paths)

    authority = runtime.resolver.resolve_worker_authority(run, context=cast("ToolRuntimeContext", context))

    assert authority.worker_id is None


@pytest.mark.asyncio
async def test_a_refresh_pinned_to_another_locator_releases_every_lease_it_cannot_use(tmp_path: Path) -> None:
    """Neither the held lease nor a newly acquired one of a different cleanup locator is published."""
    runtime_paths = _runtime_paths(tmp_path)
    leases = iter([_Lease(_Backend([])), _Lease(_Backend([]))])
    acquired: list[_Lease] = []

    def provide(_locator: str | None) -> _Lease:
        acquired.append(next(leases))
        return acquired[-1]

    runtime = _lifecycle(runtime_paths, manager=_manager_stub(), worker_lease_provider=provide)
    assert await runtime._refresh_worker_backend() is not None

    assert await runtime._refresh_worker_backend(required_backend_locator="locator-b") is None

    assert runtime.manager.worker_backend is None
    assert [lease.released for lease in acquired] == [True, True]
