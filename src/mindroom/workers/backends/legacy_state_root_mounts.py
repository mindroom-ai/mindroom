"""Retire sandbox workers that mounted whole agent state roots."""

# LEGACY_COMPAT: Sandbox workers that mounted whole agent state roots writable.
# Legacy format: Kubernetes worker pods and Docker worker containers started by releases that mounted
#   agents/<agent> and private_instances/<scope> writable instead of only the workspaces below them.
# Last legacy release: v2026.9.324; the next release mounts only workspaces.
# Handling: at primary startup, stop every such running worker through its backend so the next ensure recreates it
#   with workspace mounts, and warn the operator to check storage for links those workers may have planted.
#   A failure never blocks startup: it is retried in the background with backoff and logged as an error each time
#   until no such worker remains, and /api/health reports it meanwhile. Ensure never serves such a worker, because
#   it replaces any Deployment or container whose template this release did not write before using it.
# Coverage: tests/test_legacy_state_root_mounts.py.

from __future__ import annotations

import asyncio
from itertools import chain, repeat
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task, run_blocking_until_complete
from mindroom.logging_config import get_logger
from mindroom.workers.runtime import primary_worker_backend_name

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_RETRY_DELAYS_SECONDS = (10.0, 30.0, 120.0, 300.0)
_pending: dict[str, str] = {}


def legacy_worker_retirement_pending() -> str | None:
    """Return why workers from an older release may still mount state roots, or ``None`` once none remain."""
    return _pending.get("detail")


async def retire_state_root_worker_mounts(runtime_paths: RuntimePaths) -> None:
    """Stop workers that still mount state roots, retrying in the background until none remain."""
    if not await run_blocking_until_complete(_retire_state_root_worker_mounts, runtime_paths):
        create_background_task(_retry_retirement(runtime_paths), name="retire_state_root_worker_mounts")


async def _retry_retirement(runtime_paths: RuntimePaths) -> None:
    for delay in chain(_RETRY_DELAYS_SECONDS, repeat(_RETRY_DELAYS_SECONDS[-1])):
        await asyncio.sleep(delay)
        if await run_blocking_until_complete(_retire_state_root_worker_mounts, runtime_paths):
            return


def _retire_state_root_worker_mounts(runtime_paths: RuntimePaths) -> bool:
    """Return whether no worker from an older release remains running."""
    backend_name: str | None = None
    try:
        backend_name = primary_worker_backend_name(runtime_paths)
        if backend_name not in {"docker", "kubernetes"}:
            return True
        # Keep Docker and Kubernetes dependencies off primary module import paths.
        if backend_name == "docker":
            from mindroom.workers.backends import docker  # noqa: PLC0415

            stopped = docker.remove_docker_workers_mounting_state_roots(runtime_paths)
        else:
            from mindroom.workers.backends import kubernetes  # noqa: PLC0415

            stopped = kubernetes.stop_kubernetes_workers_mounting_state_roots(runtime_paths)
    except Exception as exc:
        _pending["detail"] = f"Retrying retirement of sandbox workers that mount whole state roots: {exc}"
        logger.exception("Could not stop sandbox workers that mount whole state roots; retrying", backend=backend_name)
        return False
    _pending.pop("detail", None)
    if stopped:
        logger.warning(
            "Stopped sandbox workers that mounted whole agent state roots; "
            "check agent state roots for links they may have planted, as the migration guide describes",
            backend=backend_name,
            workers=list(stopped),
        )
    return True
