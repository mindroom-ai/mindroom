"""Startup retirement of workers that mounted whole state roots."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException
from structlog.testing import capture_logs

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends import legacy_state_root_mounts
from mindroom.workers.backends.kubernetes_resources import KubernetesResourceManager
from mindroom.workers.backends.legacy_state_root_mounts import (
    legacy_worker_retirement_pending,
    retire_state_root_worker_mounts,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path, backend: str | None = None) -> RuntimePaths:
    process_env = {
        "MINDROOM_KUBERNETES_WORKER_IMAGE": "test-image",
        "MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME": "worker-storage",
    }
    if backend is not None:
        process_env["MINDROOM_WORKER_BACKEND"] = backend
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env=process_env,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
async def test_startup_stops_old_workers_through_their_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    """Dedicated backends stop old workers and point the operator at the manual link check."""
    runtime_paths = _runtime_paths(tmp_path, backend)
    calls: list[RuntimePaths] = []
    target = (
        "mindroom.workers.backends.docker.remove_docker_workers_mounting_state_roots"
        if backend == "docker"
        else "mindroom.workers.backends.kubernetes.stop_kubernetes_workers_mounting_state_roots"
    )
    monkeypatch.setattr(target, lambda paths: calls.append(paths) or ("old-worker",))

    with capture_logs() as logs:
        await retire_state_root_worker_mounts(runtime_paths)

    assert calls == [runtime_paths]
    [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warning["workers"] == ["old-worker"]
    assert "links" in warning["event"]

    monkeypatch.setattr(target, lambda paths: calls.append(paths) or ())
    with capture_logs() as logs:
        await retire_state_root_worker_mounts(runtime_paths)

    assert logs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "error"),
    [
        ("_load_clients", ConfigException("Service host/port is not set.")),
        ("list_deployments", ApiException(status=503, reason="Service Unavailable")),
        ("list_deployments", WorkerBackendError("Kubernetes Deployment list returned invalid JSON.")),
        ("list_deployments", ConnectionResetError("connection reset by peer")),
    ],
    ids=["config", "api", "backend", "transport"],
)
async def test_failing_kubernetes_api_never_blocks_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    error: Exception,
) -> None:
    """Any failure while stopping old workers is logged; ensure still recreates them later."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(KubernetesResourceManager, method, fail)

    with capture_logs() as logs:
        await retire_state_root_worker_mounts(_runtime_paths(tmp_path, "kubernetes"))

    [entry] = [entry for entry in logs if entry["log_level"] == "error"]
    assert entry["backend"] == "kubernetes"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [None, "static"])
async def test_startup_skips_backends_without_dedicated_workers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str | None,
) -> None:
    """Only Docker and Kubernetes ever mounted state roots into workers."""

    def unexpected(_paths: RuntimePaths) -> tuple[str, ...]:
        pytest.fail("no dedicated backend to stop")

    monkeypatch.setattr("mindroom.workers.backends.kubernetes.stop_kubernetes_workers_mounting_state_roots", unexpected)
    monkeypatch.setattr("mindroom.workers.backends.docker.remove_docker_workers_mounting_state_roots", unexpected)

    with capture_logs() as logs:
        await retire_state_root_worker_mounts(_runtime_paths(tmp_path, backend))

    assert logs == []


@pytest.mark.asyncio
async def test_failed_retirement_keeps_retrying_in_the_background_until_it_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup continues, the failure stays visible as pending and logged, and a later retry clears it."""
    attempts: list[int] = []

    def stop_on_second_attempt(_paths: RuntimePaths) -> tuple[str, ...]:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise ApiException(status=503, reason="Service Unavailable")
        return ("old-worker",)

    monkeypatch.setattr(
        "mindroom.workers.backends.kubernetes.stop_kubernetes_workers_mounting_state_roots",
        stop_on_second_attempt,
    )
    monkeypatch.setattr(legacy_state_root_mounts, "_RETRY_DELAYS_SECONDS", (0.0,))

    with capture_logs() as logs:
        await retire_state_root_worker_mounts(_runtime_paths(tmp_path, "kubernetes"))
        assert legacy_worker_retirement_pending() is not None
        await wait_for_background_tasks(timeout=5)

    assert attempts == [0, 1]
    assert legacy_worker_retirement_pending() is None
    assert [entry["log_level"] for entry in logs if entry["log_level"] in {"error", "warning"}] == ["error", "warning"]
