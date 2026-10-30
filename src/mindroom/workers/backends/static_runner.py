"""Static shared sandbox-runner backend for the primary MindRoom runtime."""

from __future__ import annotations

import threading
import time

import httpx

from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.tool_system.worker_routing import worker_dir_name
from mindroom.workers.backend import (
    WorkerBackendError,
    effective_idle_status,
    filter_and_sort_worker_handles,
)
from mindroom.workers.backend_kinds import WorkerBackendKind
from mindroom.workers.backends._lifecycle import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    WorkerLifecycleState,
    initial_worker_lifecycle_state,
    mark_worker_failed,
    mark_worker_idle,
    mark_worker_ready,
    prepare_worker_ensure_lifecycle,
    touch_worker_lifecycle,
    worker_handle,
)
from mindroom.workers.compatibility import ready_worker_protocol_mismatch
from mindroom.workers.models import (
    SANDBOX_RUNNER_API_ROOT,
    ProgressSink,
    WorkerHandle,
    WorkerSpec,
    WorkerStatus,
    normalize_sandbox_runner_api_root,
)

_HEALTH_CHECK_TIMEOUT_SECONDS = 5.0
_OLDER_RUNNER_HINT = "Restart the runner on this release, pulling its image again when it runs a tag such as ':latest'."


def _read_runner_health(runner_root: str) -> httpx.Response:
    """Return the runner's answer to its health probe."""
    with httpx.Client(timeout=_HEALTH_CHECK_TIMEOUT_SECONDS) as client:
        return client.get(f"{runner_root}/healthz")


class StaticSandboxRunnerBackend:
    """Worker backend representing the current shared sandbox-runner deployment."""

    backend_name = WorkerBackendKind.STATIC_RUNNER.value
    cleanup_locator: str | None = None

    def __init__(
        self,
        *,
        api_root: str,
        auth_token: str | None,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    ) -> None:
        self.api_root = normalize_sandbox_runner_api_root(api_root)
        self.auth_token = auth_token
        self.idle_timeout_seconds = max(1.0, idle_timeout_seconds)
        self._lock = threading.Lock()
        self._workers: dict[str, WorkerLifecycleState] = {}
        # A probe that passed stands until the next recorded failure, which may mean the runner was replaced.
        self._recorded_failures = 0
        self._compatible_since_failures: int | None = None

    def shutdown(self) -> None:
        """Drop cached shared-runner worker handles before manager replacement."""
        with self._lock:
            self._workers.clear()

    def ensure_worker(
        self,
        spec: WorkerSpec,
        *,
        now: float | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> WorkerHandle:
        """Resolve or create one worker handle for the shared sandbox runner."""
        del progress_sink
        self.require_compatible_runner()

        timestamp = time.time() if now is None else now
        with self._lock:
            lifecycle = self._workers.get(spec.worker_key)
            if lifecycle is None:
                lifecycle = initial_worker_lifecycle_state(now=timestamp)
                should_restart = True
            else:
                should_restart = self._effective_status(lifecycle, timestamp) == "idle"
            lifecycle = mark_worker_ready(
                prepare_worker_ensure_lifecycle(lifecycle, now=timestamp, should_restart=should_restart),
                now=timestamp,
            )
            self._workers[spec.worker_key] = lifecycle
            return self._to_handle(spec.worker_key, lifecycle, now=timestamp)

    def require_compatible_runner(self) -> None:
        """Refuse an unconfigured runner, or one that speaks another worker protocol.

        The protocol is asked at first use and again after a recorded failure.
        A runner that ignores newer request fields, such as the variables a plugin removes from a shell call's
        environment, would run the call without them, so the call fails instead.
        """
        if not self.api_root:
            msg = f"{SANDBOX_RUNTIME_ENV_BY_KEY['proxy_url']} must be set when sandbox proxying is enabled."
            raise WorkerBackendError(msg)
        if self.auth_token is None:
            msg = f"{SANDBOX_RUNTIME_ENV_BY_KEY['proxy_token']} must be set when sandbox proxying is enabled."
            raise WorkerBackendError(msg)
        with self._lock:
            failures_before_probe = self._recorded_failures
            if self._compatible_since_failures == failures_before_probe:
                return
        runner_root = self.api_root.removesuffix(SANDBOX_RUNNER_API_ROOT)
        try:
            response = _read_runner_health(runner_root)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            msg = f"Sandbox runner at '{runner_root}' did not answer its health check: {exc}"
            raise WorkerBackendError(msg) from exc
        # The shared runner is not this primary's to replace, so a mismatch fails the call.
        mismatch = ready_worker_protocol_mismatch(
            response,
            worker=f"Sandbox runner at '{runner_root}'",
            older_worker_hint=_OLDER_RUNNER_HINT,
        )
        if mismatch is not None:
            raise WorkerBackendError(mismatch.error)
        with self._lock:
            # A failure recorded while the probe was in flight leaves the next call to probe again.
            self._compatible_since_failures = failures_before_probe

    def touch_worker(self, worker_key: str, *, now: float | None = None) -> WorkerHandle | None:
        """Refresh last-used bookkeeping for one shared-runner worker."""
        timestamp = time.time() if now is None else now
        with self._lock:
            lifecycle = self._workers.get(worker_key)
            if lifecycle is None:
                return None
            lifecycle = touch_worker_lifecycle(lifecycle, now=timestamp)
            self._workers[worker_key] = lifecycle
            return self._to_handle(worker_key, lifecycle, now=timestamp)

    def list_workers(self, *, include_idle: bool = True, now: float | None = None) -> list[WorkerHandle]:
        """List workers seen through the shared-runner provider."""
        timestamp = time.time() if now is None else now
        with self._lock:
            handles = [
                self._to_handle(worker_key, lifecycle, now=timestamp) for worker_key, lifecycle in self._workers.items()
            ]
        return filter_and_sort_worker_handles(handles, include_idle)

    def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
        """Mark idle shared-runner workers inactive."""
        timestamp = time.time() if now is None else now
        cleaned_workers: list[WorkerHandle] = []
        with self._lock:
            for worker_key, lifecycle in self._workers.items():
                if lifecycle.status == "ready" and self._effective_status(lifecycle, timestamp) == "idle":
                    idled = mark_worker_idle(lifecycle)
                    self._workers[worker_key] = idled
                    cleaned_workers.append(self._to_handle(worker_key, idled, now=timestamp))
        return filter_and_sort_worker_handles(cleaned_workers, True)

    def record_failure(
        self,
        worker_key: str,
        failure_reason: str,
        *,
        now: float | None = None,
        startup_count: int | None = None,
    ) -> WorkerHandle:
        """Persist one shared-runner worker failure."""
        del startup_count  # This backend never replaces a worker behind an in-flight request.
        timestamp = time.time() if now is None else now
        with self._lock:
            # The runner may have been replaced by one from another release, so the next call asks again.
            self._recorded_failures += 1
            lifecycle = self._workers.get(worker_key) or initial_worker_lifecycle_state(now=timestamp)
            lifecycle = mark_worker_failed(lifecycle, now=timestamp, failure_reason=failure_reason)
            self._workers[worker_key] = lifecycle
            return self._to_handle(worker_key, lifecycle, now=timestamp)

    def _effective_status(self, lifecycle: WorkerLifecycleState, now: float) -> WorkerStatus:
        return effective_idle_status(lifecycle.status, lifecycle.last_used_at, self.idle_timeout_seconds, now)

    def _to_handle(self, worker_key: str, lifecycle: WorkerLifecycleState, *, now: float) -> WorkerHandle:
        return worker_handle(
            worker_id=worker_dir_name(worker_key),
            worker_key=worker_key,
            lifecycle=lifecycle,
            endpoint=f"{self.api_root}/execute",
            auth_token=self.auth_token,
            status=self._effective_status(lifecycle, now),
            backend_name=self.backend_name,
            debug_metadata={"api_root": self.api_root},
        )
