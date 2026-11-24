"""Read-only managed-worker absence preflight for private-storage upgrades."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        msg = "Managed-worker preflight timed out before absence was verified."
        raise WorkerBackendError(msg)
    return remaining


def check_workers_absent_for_storage_upgrade(
    runtime_paths: RuntimePaths,
    *,
    timeout_seconds: float,
) -> None:
    """Verify managed runtimes are absent without modifying containers or durable state."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        msg = "Managed-worker preflight timeout must be a positive finite number."
        raise WorkerBackendError(msg)
    deadline = time.monotonic() + timeout_seconds

    match worker_backend_kind(runtime_paths):
        case WorkerBackendKind.DOCKER:
            from mindroom.workers.backends.docker import (  # noqa: PLC0415
                check_docker_workers_absent_for_storage_upgrade,
            )

            check_docker_workers_absent_for_storage_upgrade(
                runtime_paths,
                timeout_seconds=_remaining_seconds(deadline),
            )
        case WorkerBackendKind.KUBERNETES:
            from mindroom.workers.backends.kubernetes import (  # noqa: PLC0415
                check_kubernetes_workers_absent_for_storage_upgrade,
            )

            check_kubernetes_workers_absent_for_storage_upgrade(
                runtime_paths,
                timeout_seconds=_remaining_seconds(deadline),
            )
        case WorkerBackendKind.STATIC_RUNNER:
            proxy_url = (runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["proxy_url"]) or "").strip()
            if proxy_url:
                msg = (
                    "Private-storage upgrade cannot verify absence of the configured external runner. "
                    "Stop it through the deployment lifecycle, temporarily unset MINDROOM_SANDBOX_PROXY_URL "
                    "for migration startup, then restart the primary."
                )
                raise WorkerBackendError(msg)
