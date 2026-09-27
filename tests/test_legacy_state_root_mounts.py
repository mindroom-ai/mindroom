"""Startup retirement of workers that mounted whole state roots."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException
from structlog.testing import capture_logs

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.kubernetes_resources import KubernetesResourceManager
from mindroom.workers.backends.legacy_state_root_mounts import retire_state_root_worker_mounts

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
