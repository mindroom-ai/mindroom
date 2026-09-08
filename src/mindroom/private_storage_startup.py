"""Primary-only private storage migration before runtime admission.

The deployment must first stop the previous primary, independent controllers,
and supervised children, and provide its coordinated backup policy. Managed
persistent workers are stopped here before any private-state inventory or move.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.private_storage_upgrade import StorageUpgradeCheck

_WORKER_STOP_TIMEOUT_SECONDS = 120.0


def _quiesce_workers(runtime_paths: RuntimePaths, *, timeout_seconds: float) -> None:
    from mindroom.workers.storage_quiescence import quiesce_workers_for_storage_upgrade  # noqa: PLC0415

    quiesce_workers_for_storage_upgrade(runtime_paths, timeout_seconds=timeout_seconds)


def _ensure_private_storage_ready(runtime_paths: RuntimePaths) -> StorageUpgradeCheck:
    from mindroom import private_storage_upgrade as upgrade  # noqa: PLC0415
    from mindroom.logging_config import get_logger  # noqa: PLC0415

    discovery = upgrade.discover_runtime_storage_upgrade(runtime_paths)
    if discovery is not None:
        logger = get_logger(__name__)
        if discovery.direction == "stopped":
            message = "Rolled-back private storage requires explicit recovery before startup"
            raise upgrade.StorageUpgradeError(message)
        with upgrade.storage_upgrade_locks(discovery.volumes):
            current = upgrade.discover_runtime_storage_upgrade(runtime_paths)
            if current is not None:
                if current.volumes != discovery.volumes:
                    message = "Storage participants changed while acquiring migration locks"
                    raise upgrade.StorageUpgradeError(message)
                if current.direction == "stopped":
                    message = "Rolled-back private storage requires explicit recovery before startup"
                    raise upgrade.StorageUpgradeError(message)
                logger.info("private_storage_upgrade_stopping_workers", direction=current.direction)
                _quiesce_workers(runtime_paths, timeout_seconds=_WORKER_STOP_TIMEOUT_SECONDS)
                logger.info("private_storage_upgrade_inspecting")
                current = upgrade.discover_runtime_storage_upgrade(runtime_paths)
                if current is None or current.volumes != discovery.volumes:
                    message = "Storage changed while stopping managed workers"
                    raise upgrade.StorageUpgradeError(message)
                if current.direction == "stopped":
                    message = "Rolled-back private storage requires explicit recovery before startup"
                    raise upgrade.StorageUpgradeError(message)
                plan = current.plan or upgrade.plan_storage_upgrade(
                    runtime_paths.storage_root,
                    upgrade._runtime_roots(runtime_paths)[1],
                    control_state=runtime_paths.control_state_root,
                )
                logger.info(
                    "private_storage_upgrade_applying",
                    direction=current.direction,
                    private_scopes=len(plan.operations),
                    unresolved_worker_files=plan.unresolved_worker_files,
                )
                if current.direction == "rollback":
                    upgrade.rollback_storage_upgrade_locked(plan)
                    message = "Private storage reversal completed; explicit recovery is required"
                    raise upgrade.StorageUpgradeError(message)
                upgrade.apply_storage_upgrade_locked(plan)
                upgrade.check_runtime_storage_upgrade(runtime_paths)
                logger.info("private_storage_upgrade_complete", private_scopes=len(plan.operations))
    return upgrade.StorageUpgradeCheck(upgrade._runtime_roots(runtime_paths))


async def ensure_private_storage_ready(runtime_paths: RuntimePaths) -> StorageUpgradeCheck:
    """Complete required private-storage migration before primary admission."""
    from mindroom.background_tasks import run_blocking_until_complete  # noqa: PLC0415

    return await run_blocking_until_complete(_ensure_private_storage_ready, runtime_paths)
