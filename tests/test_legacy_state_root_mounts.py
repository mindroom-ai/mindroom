"""Startup retirement of workers that mounted whole state roots."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from kubernetes.client.exceptions import ApiException
from kubernetes.config import ConfigException

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.orchestrator import main
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends import legacy_state_root_mounts
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


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("docker", ["docker"]), ("kubernetes", ["kubernetes"]), (None, []), ("static", [])],
)
def test_startup_retires_old_workers_only_on_dedicated_backends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str | None,
    expected: list[str],
) -> None:
    """Only Docker and Kubernetes ever mounted state roots into workers."""
    calls: list[str] = []
    monkeypatch.setattr(
        legacy_state_root_mounts,
        "_remove_docker_workers_mounting_state_roots",
        lambda _paths: calls.append("docker"),
    )
    monkeypatch.setattr(
        legacy_state_root_mounts,
        "_stop_kubernetes_workers_mounting_state_roots",
        lambda _resources: calls.append("kubernetes"),
    )

    retire_state_root_worker_mounts(_runtime_paths(tmp_path, backend))

    assert calls == expected


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
def test_unreachable_kubernetes_api_fails_retirement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    error: Exception,
) -> None:
    """Retirement that cannot tell whether old workers still run raises instead of letting the primary serve."""

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(KubernetesResourceManager, method, fail)

    with pytest.raises(type(error)):
        retire_state_root_worker_mounts(_runtime_paths(tmp_path, "kubernetes"))


@pytest.mark.asyncio
async def test_failed_retirement_fails_startup_before_anything_serves(tmp_path: Path) -> None:
    """The primary never builds its runtime or API while an old worker may still run."""
    with (
        patch("mindroom.orchestrator.setup_logging"),
        patch("mindroom.orchestrator.sync_env_to_credentials"),
        patch(
            "mindroom.orchestrator.retire_state_root_worker_mounts",
            side_effect=WorkerBackendError("Could not stop workers that mount whole state roots: old-worker"),
        ),
        patch("mindroom.orchestrator._MultiAgentOrchestrator") as orchestrator,
        pytest.raises(WorkerBackendError, match="old-worker"),
    ):
        await main(log_level="INFO", runtime_paths=_runtime_paths(tmp_path, "kubernetes"), api=True)

    orchestrator.assert_not_called()
