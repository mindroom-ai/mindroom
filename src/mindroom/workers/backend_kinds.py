"""The worker backends a primary runtime can select, and the one table of their selector values."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from mindroom.runtime_env_policy import KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

__all__ = ["WorkerBackendKind", "worker_backend_kind"]

_WORKER_BACKEND_ENV = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["worker_backend"]


class WorkerBackendKind(StrEnum):
    """One worker backend that routed tool calls can run on."""

    STATIC_RUNNER = "static_runner"
    DOCKER = "docker"
    KUBERNETES = "kubernetes"

    @property
    def dedicated(self) -> bool:
        """Return whether this backend provisions a dedicated runtime per worker key."""
        return self is not WorkerBackendKind.STATIC_RUNNER


_KIND_BY_SELECTOR = {
    "": WorkerBackendKind.STATIC_RUNNER,
    "static": WorkerBackendKind.STATIC_RUNNER,
    "static_runner": WorkerBackendKind.STATIC_RUNNER,
    "shared_runner": WorkerBackendKind.STATIC_RUNNER,
    "static_sandbox_runner": WorkerBackendKind.STATIC_RUNNER,
    "docker": WorkerBackendKind.DOCKER,
    "k8s": WorkerBackendKind.KUBERNETES,
    "kubernetes": WorkerBackendKind.KUBERNETES,
}


def worker_backend_kind(runtime_paths: RuntimePaths) -> WorkerBackendKind:
    """Return the worker backend this runtime selects, raising on a selector value no backend answers to."""
    raw_value = runtime_paths.env_value(_WORKER_BACKEND_ENV)
    kind = _KIND_BY_SELECTOR.get((raw_value or "").strip().lower())
    if kind is None:
        msg = f"Unsupported worker backend: {raw_value}"
        raise WorkerBackendError(msg)
    return kind
