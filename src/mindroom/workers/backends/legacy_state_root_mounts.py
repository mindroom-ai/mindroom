"""Retire sandbox workers that mounted whole agent state roots."""

# LEGACY_COMPAT: Sandbox workers that mounted whole agent state roots writable.
# Legacy format: Kubernetes worker pods and Docker worker containers started by releases that mounted
#   agents/<agent> and private_instances/<scope> writable instead of only the workspaces below them.
# Last legacy release: v2026.9.324; the next release mounts only workspaces.
# Handling: at primary startup, stop every such running worker through its backend so the next ensure recreates it
#   with workspace mounts, and warn the operator to check storage for links those workers may have planted.
#   Any failure is logged and never blocks startup, because ensure recreates outdated workers anyway.
# Coverage: tests/test_legacy_state_root_mounts.py.

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.logging_config import get_logger
from mindroom.workers.runtime import primary_worker_backend_name

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


async def retire_state_root_worker_mounts(runtime_paths: RuntimePaths) -> None:
    """Stop workers that still mount state roots before runtime work starts."""
    await run_blocking_until_complete(_retire_state_root_worker_mounts, runtime_paths)


def _retire_state_root_worker_mounts(runtime_paths: RuntimePaths) -> None:
    backend_name: str | None = None
    try:
        backend_name = primary_worker_backend_name(runtime_paths)
        if backend_name not in {"docker", "kubernetes"}:
            return
        stopped = _stop_state_root_workers(runtime_paths, backend_name)
    except Exception:
        logger.exception("Could not stop sandbox workers that mount whole state roots", backend=backend_name)
        return
    if stopped:
        logger.warning(
            "Stopped sandbox workers that mounted whole agent state roots; "
            "check agent state roots for links they may have planted, as the migration guide describes",
            backend=backend_name,
            workers=list(stopped),
        )


def _stop_state_root_workers(runtime_paths: RuntimePaths, backend_name: str) -> tuple[str, ...]:
    # Keep Docker and Kubernetes dependencies off primary module import paths.
    if backend_name == "docker":
        from mindroom.workers.backends.docker import remove_docker_workers_mounting_state_roots  # noqa: PLC0415

        return remove_docker_workers_mounting_state_roots(runtime_paths)
    from mindroom.workers.backends.kubernetes import stop_kubernetes_workers_mounting_state_roots  # noqa: PLC0415

    return stop_kubernetes_workers_mounting_state_roots(runtime_paths)
