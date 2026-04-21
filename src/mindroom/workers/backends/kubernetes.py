"""Kubernetes-backed worker backend for the primary MindRoom runtime."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS
from mindroom.credentials import get_runtime_credentials_manager, sync_shared_credentials_to_worker
from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import (
    ProgressSink,
    WorkerHandle,
    WorkerReadyPhase,
    WorkerReadyProgress,
    WorkerSpec,
    WorkerStatus,
)

from . import kubernetes_resources as resources
from .kubernetes_config import _KubernetesWorkerBackendConfig, kubernetes_backend_config_signature

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

__all__ = [
    "KubernetesWorkerBackend",
    "_KubernetesWorkerBackendConfig",
    "kubernetes_backend_config_signature",
]

_COLD_START_GRACE_SECONDS = 1.5
_WAITING_PROGRESS_INTERVAL_SECONDS = 5.0
_PROGRESS_REPORTER_JOIN_TIMEOUT_SECONDS = 1.0


@dataclass
class _ProgressReporterState:
    started_at: float
    cold_start_emitted: bool = False
    next_waiting_elapsed: float = _WAITING_PROGRESS_INTERVAL_SECONDS
    reporter_done: bool = False


def _noop_finalize_progress(_phase: WorkerReadyPhase, _error: str | None) -> None:
    del _phase, _error


def _progress_event(
    *,
    phase: WorkerReadyPhase,
    worker_key: str,
    backend_name: str,
    elapsed_seconds: float,
    error: str | None = None,
) -> WorkerReadyProgress:
    return WorkerReadyProgress(
        phase=phase,
        worker_key=worker_key,
        backend_name=backend_name,
        elapsed_seconds=elapsed_seconds,
        error=error,
    )


def _pending_progress_events(
    *,
    state: _ProgressReporterState,
    worker_key: str,
    backend_name: str,
) -> list[WorkerReadyProgress]:
    elapsed_seconds = max(0.0, time.monotonic() - state.started_at)
    events: list[WorkerReadyProgress] = []
    if not state.cold_start_emitted and elapsed_seconds >= _COLD_START_GRACE_SECONDS:
        events.append(
            _progress_event(
                phase="cold_start",
                worker_key=worker_key,
                backend_name=backend_name,
                elapsed_seconds=elapsed_seconds,
            ),
        )
        state.cold_start_emitted = True
    while state.cold_start_emitted and elapsed_seconds >= state.next_waiting_elapsed:
        events.append(
            _progress_event(
                phase="waiting",
                worker_key=worker_key,
                backend_name=backend_name,
                elapsed_seconds=elapsed_seconds,
            ),
        )
        state.next_waiting_elapsed += _WAITING_PROGRESS_INTERVAL_SECONDS
    return events


def _next_progress_deadline_elapsed(state: _ProgressReporterState) -> float:
    if not state.cold_start_emitted:
        return _COLD_START_GRACE_SECONDS
    return state.next_waiting_elapsed


def _report_progress(progress_sink: ProgressSink, events: list[WorkerReadyProgress]) -> None:
    for event in events:
        progress_sink(event)


def _progress_terminal_event(
    *,
    state: _ProgressReporterState,
    phase: WorkerReadyPhase,
    worker_key: str,
    backend_name: str,
    error: str | None,
) -> WorkerReadyProgress | None:
    if phase == "ready" and not state.cold_start_emitted:
        return None
    return _progress_event(
        phase=phase,
        worker_key=worker_key,
        backend_name=backend_name,
        elapsed_seconds=max(0.0, time.monotonic() - state.started_at),
        error=error,
    )


def _progress_reporter_events(
    *,
    condition: threading.Condition,
    state: _ProgressReporterState,
    worker_key: str,
    backend_name: str,
) -> list[WorkerReadyProgress] | None:
    with condition:
        while True:
            if state.reporter_done:
                return None
            wait_timeout = state.started_at + _next_progress_deadline_elapsed(state) - time.monotonic()
            if wait_timeout > 0:
                condition.wait(timeout=wait_timeout)
                continue
            return _pending_progress_events(
                state=state,
                worker_key=worker_key,
                backend_name=backend_name,
            )


def _finalize_progress_events(
    *,
    condition: threading.Condition,
    state: _ProgressReporterState,
    phase: WorkerReadyPhase,
    worker_key: str,
    backend_name: str,
    error: str | None,
) -> tuple[list[WorkerReadyProgress], WorkerReadyProgress | None]:
    with condition:
        pending_events = _pending_progress_events(
            state=state,
            worker_key=worker_key,
            backend_name=backend_name,
        )
        terminal_event = _progress_terminal_event(
            state=state,
            phase=phase,
            worker_key=worker_key,
            backend_name=backend_name,
            error=error,
        )
        state.reporter_done = True
        condition.notify_all()
        return pending_events, terminal_event


def _progress_reporter_loop(
    *,
    condition: threading.Condition,
    state: _ProgressReporterState,
    progress_sink: ProgressSink,
    worker_key: str,
    backend_name: str,
) -> None:
    while True:
        pending_events = _progress_reporter_events(
            condition=condition,
            state=state,
            worker_key=worker_key,
            backend_name=backend_name,
        )
        if pending_events is None:
            return
        _report_progress(progress_sink, pending_events)


def _build_progress_reporter(
    *,
    worker_key: str,
    backend_name: str,
    progress_sink: ProgressSink,
) -> tuple[Callable[[float], None] | None, Callable[[WorkerReadyPhase, str | None], None]]:
    condition = threading.Condition()
    state = _ProgressReporterState(started_at=time.monotonic())

    thread = threading.Thread(
        target=_progress_reporter_loop,
        kwargs={
            "condition": condition,
            "state": state,
            "progress_sink": progress_sink,
            "worker_key": worker_key,
            "backend_name": backend_name,
        },
        name=f"kubernetes-worker-progress:{worker_key}",
        daemon=True,
    )
    thread.start()

    def on_poll_tick(_elapsed_seconds: float) -> None:
        with condition:
            condition.notify_all()

    def finalize(phase: WorkerReadyPhase, error: str | None) -> None:
        pending_events, terminal_event = _finalize_progress_events(
            condition=condition,
            state=state,
            phase=phase,
            worker_key=worker_key,
            backend_name=backend_name,
            error=error,
        )
        thread.join(timeout=_PROGRESS_REPORTER_JOIN_TIMEOUT_SECONDS)
        _report_progress(progress_sink, pending_events)
        if terminal_event is not None:
            progress_sink(terminal_event)

    return on_poll_tick, finalize


class KubernetesWorkerBackend:
    """Kubernetes-backed worker provider for dedicated worker pods."""

    backend_name = "kubernetes"

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        config: _KubernetesWorkerBackendConfig,
        auth_token: str | None,
        storage_root: Path,
        tool_validation_snapshot: dict[str, dict[str, object]],
        worker_grantable_credentials: frozenset[str],
    ) -> None:
        unsupported_services = sorted(worker_grantable_credentials & UNSUPPORTED_WORKER_GRANTABLE_CREDENTIALS)
        if unsupported_services:
            msg = (
                "Dedicated workers do not support "
                f"{', '.join(unsupported_services)}. Keep these credentials in the primary runtime."
            )
            raise WorkerBackendError(msg)
        self.runtime_paths = runtime_paths
        self.config = config
        self.auth_token = auth_token
        self.storage_root = storage_root.expanduser().resolve()
        self.worker_grantable_credentials = worker_grantable_credentials
        self.idle_timeout_seconds = config.idle_timeout_seconds
        self._resources = resources.KubernetesResourceManager(
            runtime_paths=runtime_paths,
            config=config,
            auth_token=auth_token,
            storage_root=self.storage_root,
            tool_validation_snapshot=tool_validation_snapshot,
            worker_grantable_credentials=worker_grantable_credentials,
        )
        self._worker_locks: dict[str, threading.Lock] = {}
        self._worker_locks_lock = threading.Lock()
        self._progress_sinks: dict[str, list[ProgressSink]] = {}
        self._progress_snapshots: dict[str, WorkerReadyProgress | None] = {}
        self._progress_sinks_lock = threading.Lock()

    @classmethod
    def from_runtime(
        cls,
        runtime_paths: RuntimePaths,
        *,
        auth_token: str | None,
        storage_root: Path,
        tool_validation_snapshot: dict[str, dict[str, object]],
        worker_grantable_credentials: frozenset[str],
    ) -> KubernetesWorkerBackend:
        """Construct a backend instance from one explicit runtime context."""
        return cls(
            runtime_paths=runtime_paths,
            config=_KubernetesWorkerBackendConfig.from_runtime(runtime_paths),
            auth_token=auth_token,
            storage_root=storage_root,
            tool_validation_snapshot=tool_validation_snapshot,
            worker_grantable_credentials=worker_grantable_credentials,
        )

    def _register_progress_sink(self, worker_key: str, progress_sink: ProgressSink) -> None:
        with self._progress_sinks_lock:
            self._progress_sinks.setdefault(worker_key, []).append(progress_sink)
            snapshot = self._progress_snapshots.setdefault(worker_key, None)
            if snapshot is not None:
                progress_sink(snapshot)

    def _unregister_progress_sink(self, worker_key: str, progress_sink: ProgressSink) -> None:
        with self._progress_sinks_lock:
            sinks = self._progress_sinks.get(worker_key)
            if sinks is None:
                return
            for index, current_sink in enumerate(sinks):
                if current_sink is progress_sink:
                    sinks.pop(index)
                    break
            if not sinks:
                self._progress_sinks.pop(worker_key, None)
                self._progress_snapshots.pop(worker_key, None)

    def _emit_progress(self, progress: WorkerReadyProgress) -> None:
        with self._progress_sinks_lock:
            if progress.phase in {"ready", "failed"}:
                self._progress_snapshots.pop(progress.worker_key, None)
            else:
                self._progress_snapshots[progress.worker_key] = progress
            for current_sink in self._progress_sinks.get(progress.worker_key, ()):
                current_sink(progress)

    def ensure_worker(  # noqa: PLR0915
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> WorkerHandle:
        """Resolve or start the worker backing the given worker key."""
        worker_key = spec.worker_key
        if progress_sink is not None:
            self._register_progress_sink(worker_key, progress_sink)
        try:
            with self._worker_lock(worker_key):
                timestamp = time.time() if now is None else now
                worker_id = self._worker_id(worker_key)
                state_subpath = self._state_subpath(worker_key)
                existing = self._resources.read_deployment(worker_id)
                current_handle = self._handle_from_deployment(existing, now=timestamp) if existing is not None else None
                should_restart = current_handle is None or current_handle.status in {"idle", "failed"}
                startup_count = (current_handle.startup_count if current_handle is not None else 0) + int(
                    should_restart,
                )
                created_at = current_handle.created_at if current_handle is not None else timestamp
                if should_restart:
                    last_started_at = timestamp
                else:
                    assert current_handle is not None
                    last_started_at = current_handle.last_started_at
                annotations = resources.metadata_annotations(
                    worker_key=worker_key,
                    state_subpath=state_subpath,
                    created_at=created_at,
                    last_used_at=timestamp,
                    last_started_at=last_started_at,
                    startup_count=startup_count,
                    failure_count=current_handle.failure_count if current_handle is not None else 0,
                    failure_reason=None,
                    status="starting",
                )

                sync_shared_credentials_to_worker(
                    worker_key,
                    allowed_services=self.worker_grantable_credentials,
                    credentials_manager=get_runtime_credentials_manager(self.runtime_paths),
                )
                self._resources.apply_service(worker_id)
                deployment_apply = self._resources.apply_deployment(
                    worker_key=worker_key,
                    worker_id=worker_id,
                    state_subpath=state_subpath,
                    annotations=annotations,
                    replicas=1,
                    private_agent_names=spec.private_agent_names,
                )
                startup_triggered = should_restart or deployment_apply.recreated
                if deployment_apply.recreated and not should_restart:
                    startup_count = (current_handle.startup_count if current_handle is not None else 0) + 1
                    annotations = resources.metadata_annotations(
                        worker_key=worker_key,
                        state_subpath=state_subpath,
                        created_at=created_at,
                        last_used_at=timestamp,
                        last_started_at=timestamp,
                        startup_count=startup_count,
                        failure_count=current_handle.failure_count if current_handle is not None else 0,
                        failure_reason=None,
                        status="starting",
                    )
                    self._resources.patch_deployment(worker_id, annotations=annotations)
                should_report_progress = startup_triggered or existing is None or not self._deployment_ready(existing)
                poll_reporter: Callable[[float], None] | None = None
                finalize_progress: Callable[[WorkerReadyPhase, str | None], None] = _noop_finalize_progress
                if should_report_progress:
                    poll_reporter, finalize_progress = _build_progress_reporter(
                        worker_key=worker_key,
                        backend_name=self.backend_name,
                        progress_sink=self._emit_progress,
                    )
                try:
                    deployment = self._resources.wait_for_ready(
                        worker_id,
                        timeout_seconds=self.config.ready_timeout_seconds,
                        deployment_ready_fn=self._deployment_ready,
                        on_poll_tick=poll_reporter,
                    )
                except Exception as exc:
                    failure_reason = str(exc)
                    finalize_progress("failed", failure_reason)
                    self.record_failure(worker_key, failure_reason, now=timestamp)
                    if isinstance(exc, WorkerBackendError):
                        raise
                    raise WorkerBackendError(failure_reason) from exc

                finalize_progress("ready", None)
                final_annotations = dict(annotations)
                final_annotations[resources.ANNOTATION_WORKER_STATUS] = "ready"
                self._resources.patch_deployment(worker_id, annotations=final_annotations)
                deployment.metadata.annotations = {
                    **dict(deployment.metadata.annotations or {}),
                    **final_annotations,
                }
                return self._handle_from_deployment(deployment, now=timestamp)
        finally:
            if progress_sink is not None:
                self._unregister_progress_sink(worker_key, progress_sink)

    def get_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Return the current worker handle for one worker key, if present."""
        deployment = self._resources.read_deployment(self._worker_id(worker_key))
        if deployment is None:
            return None
        timestamp = time.time() if now is None else now
        return self._handle_from_deployment(deployment, now=timestamp)

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used metadata for one existing worker."""
        timestamp = time.time() if now is None else now
        worker_id = self._worker_id(worker_key)
        deployment = self._resources.read_deployment(worker_id)
        if deployment is None:
            return None

        annotations = dict(deployment.metadata.annotations or {})
        annotations[resources.ANNOTATION_LAST_USED_AT] = str(timestamp)
        if annotations.get(resources.ANNOTATION_WORKER_STATUS) == "idle":
            annotations[resources.ANNOTATION_WORKER_STATUS] = "ready"
        self._resources.patch_deployment(worker_id, annotations=annotations)
        deployment.metadata.annotations = annotations
        return self._handle_from_deployment(deployment, now=timestamp)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List workers known to this backend."""
        timestamp = time.time() if now is None else now
        handles = [
            self._handle_from_deployment(deployment, now=timestamp) for deployment in self._resources.list_deployments()
        ]
        if not include_idle:
            handles = [handle for handle in handles if handle.status != "idle"]
        return sorted(handles, key=lambda handle: handle.last_used_at, reverse=True)

    def evict_worker(
        self,
        worker_key: str,
        *,
        preserve_state: bool = True,
        now: float | None = None,
    ) -> WorkerHandle | None:
        """Evict a worker and optionally retain its persisted state."""
        timestamp = time.time() if now is None else now
        worker_id = self._worker_id(worker_key)
        deployment = self._resources.read_deployment(worker_id)
        if deployment is None:
            return None
        if not preserve_state:
            self._resources.delete_deployment(worker_id)
            self._resources.delete_service(worker_id)
            return None

        annotations = dict(deployment.metadata.annotations or {})
        annotations[resources.ANNOTATION_LAST_USED_AT] = str(timestamp)
        annotations[resources.ANNOTATION_WORKER_STATUS] = "idle"
        self._resources.patch_deployment(worker_id, replicas=0, annotations=annotations)
        self._resources.delete_service(worker_id)
        deployment.spec.replicas = 0
        deployment.metadata.annotations = annotations
        return self._handle_from_deployment(deployment, now=timestamp)

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Scale idle workers to zero while retaining their state."""
        timestamp = time.time() if now is None else now
        cleaned: list[WorkerHandle] = []
        for deployment in self._resources.list_deployments():
            handle = self._handle_from_deployment(deployment, now=timestamp)
            if handle.status != "idle" or int(deployment.spec.replicas or 0) == 0:
                continue
            annotations = dict(deployment.metadata.annotations or {})
            annotations[resources.ANNOTATION_WORKER_STATUS] = "idle"
            self._resources.patch_deployment(handle.worker_id, replicas=0, annotations=annotations)
            self._resources.delete_service(handle.worker_id)
            deployment.spec.replicas = 0
            deployment.metadata.annotations = annotations
            cleaned.append(self._handle_from_deployment(deployment, now=timestamp))
        return cleaned

    def record_failure(self, worker_key: str, failure_reason: str, *, now: float | None = None) -> WorkerHandle:
        """Persist a failed worker startup or execution state."""
        timestamp = time.time() if now is None else now
        worker_id = self._worker_id(worker_key)
        deployment = self._resources.read_deployment(worker_id)
        if deployment is None:
            msg = f"Unknown worker '{worker_key}' for Kubernetes failure recording."
            raise WorkerBackendError(msg)

        annotations = dict(deployment.metadata.annotations or {})
        annotations[resources.ANNOTATION_LAST_USED_AT] = str(timestamp)
        annotations[resources.ANNOTATION_WORKER_STATUS] = "failed"
        annotations[resources.ANNOTATION_FAILURE_REASON] = failure_reason
        annotations[resources.ANNOTATION_FAILURE_COUNT] = str(
            resources.parse_annotation_int(annotations, resources.ANNOTATION_FAILURE_COUNT) + 1,
        )
        self._resources.patch_deployment(worker_id, replicas=0, annotations=annotations)
        self._resources.delete_service(worker_id)
        deployment.spec.replicas = 0
        deployment.metadata.annotations = annotations
        return self._handle_from_deployment(deployment, now=timestamp)

    def _worker_lock(self, worker_key: str) -> threading.Lock:
        with self._worker_locks_lock:
            worker_lock = self._worker_locks.get(worker_key)
            if worker_lock is None:
                worker_lock = threading.Lock()
                self._worker_locks[worker_key] = worker_lock
        return worker_lock

    def _worker_id(self, worker_key: str) -> str:
        return resources.worker_id_for_key(worker_key, prefix=self.config.name_prefix)

    def _state_subpath(self, worker_key: str) -> str:
        prefix = self.config.storage_subpath_prefix.strip().strip("/")
        worker_dir = worker_dir_name(worker_key)
        return f"{prefix}/{worker_dir}" if prefix else worker_dir

    def _deployment_ready(self, deployment: resources.KubernetesDeployment) -> bool:
        desired = int(deployment.spec.replicas or 0)
        if desired == 0:
            return True
        ready = int(deployment.status.ready_replicas or 0)
        observed_generation = deployment.status.observed_generation
        generation = deployment.metadata.generation
        generation_ready = observed_generation is None or generation is None or observed_generation >= generation
        return generation_ready and ready >= desired

    def _handle_from_deployment(self, deployment: resources.KubernetesDeployment, *, now: float) -> WorkerHandle:
        metadata = deployment.metadata
        annotations = dict(metadata.annotations or {})
        worker_key = annotations.get(resources.ANNOTATION_WORKER_KEY)
        if not worker_key:
            msg = f"Deployment '{metadata.name}' is missing worker metadata."
            raise WorkerBackendError(msg)

        worker_id = str(metadata.name)
        last_used_at = resources.parse_annotation_float(annotations, resources.ANNOTATION_LAST_USED_AT, now)
        created_at = resources.parse_annotation_float(annotations, resources.ANNOTATION_CREATED_AT, last_used_at)
        last_started_at = annotations.get(resources.ANNOTATION_LAST_STARTED_AT)
        status = self._effective_status(deployment, now=now)
        endpoint_root = resources.service_host(worker_id, self.config.namespace, self.config.worker_port)
        return WorkerHandle(
            worker_id=worker_id,
            worker_key=worker_key,
            endpoint=f"{endpoint_root}/api/sandbox-runner/execute",
            auth_token=self.auth_token,
            status=status,
            backend_name=self.backend_name,
            last_used_at=last_used_at,
            created_at=created_at,
            last_started_at=float(last_started_at) if last_started_at is not None else None,
            expires_at=None,
            startup_count=resources.parse_annotation_int(annotations, resources.ANNOTATION_STARTUP_COUNT),
            failure_count=resources.parse_annotation_int(annotations, resources.ANNOTATION_FAILURE_COUNT),
            failure_reason=annotations.get(resources.ANNOTATION_FAILURE_REASON),
            debug_metadata={
                "namespace": self.config.namespace,
                "deployment_name": worker_id,
                "service_name": worker_id,
                "state_subpath": annotations.get(resources.ANNOTATION_STATE_SUBPATH, ""),
                "api_root": f"{endpoint_root}/api/sandbox-runner",
            },
        )

    def _effective_status(self, deployment: resources.KubernetesDeployment, *, now: float) -> WorkerStatus:
        annotations = dict(deployment.metadata.annotations or {})
        stored_status = annotations.get(resources.ANNOTATION_WORKER_STATUS, "starting")
        if stored_status == "failed":
            return "failed"
        replicas = int(deployment.spec.replicas or 0)
        if replicas == 0:
            return "idle"
        if not self._deployment_ready(deployment):
            return "starting"
        last_used_at = resources.parse_annotation_float(annotations, resources.ANNOTATION_LAST_USED_AT, now)
        if now - last_used_at >= self.idle_timeout_seconds:
            return "idle"
        return "ready"
