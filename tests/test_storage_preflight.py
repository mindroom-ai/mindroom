"""Tests for migration-only managed-worker preflight dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.storage_preflight import check_workers_absent_for_storage_upgrade

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path, process_env: dict[str, str]) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "state",
        process_env=process_env,
    )


@pytest.mark.parametrize("backend", [None, "static", "shared_runner"])
def test_local_contained_runtime_needs_no_managed_worker_api(tmp_path: Path, backend: str | None) -> None:
    """The stopped local supervisor contract has no persistent runtime to remove."""
    process_env = {} if backend is None else {"MINDROOM_WORKER_BACKEND": backend}

    check_workers_absent_for_storage_upgrade(_runtime_paths(tmp_path, process_env), timeout_seconds=5.0)


def test_static_external_runner_fails_safely(tmp_path: Path) -> None:
    """A configured remote shared runner cannot have its absence verified."""
    runtime_paths = _runtime_paths(
        tmp_path,
        {
            "MINDROOM_WORKER_BACKEND": "static",
            "MINDROOM_SANDBOX_PROXY_URL": "https://runner.example.test",
        },
    )

    with pytest.raises(WorkerBackendError, match="external runner"):
        check_workers_absent_for_storage_upgrade(runtime_paths, timeout_seconds=5.0)


@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0, float("inf"), float("nan")])
def test_preflight_requires_positive_finite_timeout(tmp_path: Path, timeout_seconds: float) -> None:
    """Every backend absence check must have one usable overall deadline."""
    with pytest.raises(WorkerBackendError, match="positive finite"):
        check_workers_absent_for_storage_upgrade(_runtime_paths(tmp_path, {}), timeout_seconds=timeout_seconds)


def test_kubernetes_dispatch_is_lazy_and_passes_remaining_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatch calls the migration helper without constructing a normal manager."""
    runtime_paths = _runtime_paths(tmp_path, {"MINDROOM_WORKER_BACKEND": "kubernetes"})
    calls: list[tuple[object, float]] = []

    monkeypatch.setattr(
        "mindroom.workers.backends.kubernetes.check_kubernetes_workers_absent_for_storage_upgrade",
        lambda paths, *, timeout_seconds: calls.append((paths, timeout_seconds)),
    )

    check_workers_absent_for_storage_upgrade(runtime_paths, timeout_seconds=5.0)

    assert len(calls) == 1
    assert calls[0][0] == runtime_paths
    assert 0.0 < calls[0][1] <= 5.0


def test_docker_dispatch_is_lazy_and_passes_remaining_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docker dispatch uses its dedicated preflight rather than the backend manager."""
    runtime_paths = _runtime_paths(tmp_path, {"MINDROOM_WORKER_BACKEND": "docker"})
    calls: list[tuple[object, float]] = []

    monkeypatch.setattr(
        "mindroom.workers.backends.docker.check_docker_workers_absent_for_storage_upgrade",
        lambda paths, *, timeout_seconds: calls.append((paths, timeout_seconds)),
    )

    check_workers_absent_for_storage_upgrade(runtime_paths, timeout_seconds=5.0)

    assert len(calls) == 1
    assert calls[0][0] == runtime_paths
    assert 0.0 < calls[0][1] <= 5.0


def test_unknown_backend_fails_before_worker_activity(tmp_path: Path) -> None:
    """Unknown topology cannot be treated as absent."""
    runtime_paths = _runtime_paths(tmp_path, {"MINDROOM_WORKER_BACKEND": "unknown"})

    with pytest.raises(WorkerBackendError, match="Unsupported worker backend"):
        check_workers_absent_for_storage_upgrade(runtime_paths, timeout_seconds=5.0)
