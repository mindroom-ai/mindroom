"""The worker backend each ``MINDROOM_WORKER_BACKEND`` value selects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.constants import resolve_primary_runtime_paths
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path, backend: str | None) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={} if backend is None else {"MINDROOM_WORKER_BACKEND": backend},
    )


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        (None, WorkerBackendKind.STATIC_RUNNER),
        ("", WorkerBackendKind.STATIC_RUNNER),
        ("static", WorkerBackendKind.STATIC_RUNNER),
        ("static_runner", WorkerBackendKind.STATIC_RUNNER),
        ("shared_runner", WorkerBackendKind.STATIC_RUNNER),
        ("static_sandbox_runner", WorkerBackendKind.STATIC_RUNNER),
        ("docker", WorkerBackendKind.DOCKER),
        (" Docker ", WorkerBackendKind.DOCKER),
        ("k8s", WorkerBackendKind.KUBERNETES),
        ("kubernetes", WorkerBackendKind.KUBERNETES),
    ],
)
def test_each_selector_value_names_one_backend(
    tmp_path: Path,
    selector: str | None,
    expected: WorkerBackendKind,
) -> None:
    """Every accepted spelling resolves to exactly one backend, and only the shared runner is not dedicated."""
    kind = worker_backend_kind(_runtime_paths(tmp_path, selector))

    assert kind is expected
    assert kind.dedicated is (expected is not WorkerBackendKind.STATIC_RUNNER)


def test_an_unknown_selector_value_is_refused(tmp_path: Path) -> None:
    """A backend no table entry names fails instead of falling back to the shared runner."""
    with pytest.raises(WorkerBackendError, match="Unsupported worker backend: local"):
        worker_backend_kind(_runtime_paths(tmp_path, "local"))
