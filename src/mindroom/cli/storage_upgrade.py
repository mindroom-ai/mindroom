"""Offline owner-verified private-storage upgrade commands."""

import os
from pathlib import Path
from typing import Annotated

import typer

from mindroom.durable_write import fsync_directory_durable
from mindroom.private_instance_identity_store import load_private_instance_record_payload
from mindroom.private_storage_upgrade import (
    StorageUpgradeError,
    StorageUpgradePlan,
    apply_storage_upgrade,
    plan_storage_upgrade,
    rollback_storage_upgrade,
    verify_storage_upgrade,
)

storage_upgrade_app = typer.Typer(help="Plan and recover offline private-storage upgrades.")


def _read_plan(manifest: Path) -> StorageUpgradePlan:
    try:
        return StorageUpgradePlan.model_validate(
            load_private_instance_record_payload(manifest, max_bytes=64 * 1024 * 1024),
        )
    except (OSError, ValueError) as error:
        message = "Cannot read the protected storage upgrade manifest"
        raise StorageUpgradeError(message) from error


@storage_upgrade_app.command("plan")
def plan(
    storage: Annotated[Path, typer.Option(help="Existing main storage root.")],
    manifest: Annotated[Path, typer.Option(help="New protected local plan file.")],
    sessions: Annotated[Path | None, typer.Option(help="Existing separate session root, if configured.")] = None,
    control_state: Annotated[
        Path | None,
        typer.Option(help="Control state root; defaults to storage/control_state."),
    ] = None,
) -> None:
    """Inspect volumes; save a protected owner mapping without changing storage."""
    if manifest.exists() or manifest.is_symlink():
        message = "Manifest already exists"
        raise typer.BadParameter(message)
    result = plan_storage_upgrade(storage, sessions, control_state=control_state)
    descriptor = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        destination.write(result.model_dump_json())
        destination.flush()
        os.fsync(destination.fileno())
    fsync_directory_durable(manifest.parent)
    typer.echo(f"Private scopes: {len(result.operations)}; unresolved worker files: {result.unresolved_worker_files}")


@storage_upgrade_app.command("apply")
@storage_upgrade_app.command("resume")
def apply(
    manifest: Path,
    writers_stopped: Annotated[
        bool,
        typer.Option(help="All primary, worker, script, and watcher writers are stopped."),
    ] = False,
    backup_verified: Annotated[
        bool,
        typer.Option(help="Restorable backups of every participating volume are verified."),
    ] = False,
) -> None:
    """Apply or resume the same inspected transaction with ingress held closed."""
    apply_storage_upgrade(_read_plan(manifest), writers_stopped=writers_stopped, backup_verified=backup_verified)
    typer.echo("Private storage upgraded; worker credential recovery remains independent.")


@storage_upgrade_app.command("rollback")
def rollback(
    manifest: Path,
    writers_stopped: Annotated[
        bool,
        typer.Option(help="All writers remain stopped; no candidate traffic has written data."),
    ] = False,
) -> None:
    """Reverse unchanged data using the original receipt, including interrupted moves."""
    rollback_storage_upgrade(_read_plan(manifest), writers_stopped=writers_stopped)
    typer.echo("Private storage reversed; candidate startup remains fenced.")


@storage_upgrade_app.command("verify")
def verify(manifest: Path) -> None:
    """Verify exact relocated data and owner resolution without modifying storage."""
    verify_storage_upgrade(_read_plan(manifest))
    typer.echo("Relocated private storage verified.")
