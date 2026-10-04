"""Install a configuration tree with whole-tree and runtime source receipts."""

from __future__ import annotations

import json
import math
import sys
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import typer

from mindroom.cli.config import activate_cli_runtime
from mindroom.cli.config_reload import request_reload_status, wait_for_applied
from mindroom.constants import exported_process_env

if TYPE_CHECKING:
    from mindroom.config_bundle import BundleInstallResult
    from mindroom.config_reload import ConfigReloadStatus
    from mindroom.constants import RuntimePaths

type _ApplyStatus = Literal["applied", "pending", "failed", "rolled_back", "unconfirmed", "restart_required"]

_APPLY_EXIT_CODES: dict[_ApplyStatus, int] = {
    "applied": 0,
    "pending": 1,
    "failed": 2,
    "rolled_back": 3,
    "unconfirmed": 4,
    "restart_required": 5,
}


def initialize_runtime_bundle(
    source: Path,
    config_path: Path | None,
    storage_path: Path | None,
    revision: str | None = None,
) -> None:
    """Initialize the selected config directory before the runtime captures its environment."""
    # Both modules load Pydantic config models and cryptography-backed credentials;
    # keep that graph out of CLI help startup (see tests/test_import_graph.py).
    from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES  # noqa: PLC0415
    from mindroom.config_bundle import install_config_bundle  # noqa: PLC0415

    runtime = activate_cli_runtime(config_path, storage_path=storage_path)
    process_env = exported_process_env()
    if storage_path is not None:
        process_env["MINDROOM_STORAGE_PATH"] = str(storage_path.expanduser().resolve())
    try:
        install_config_bundle(
            source,
            runtime.config_dir,
            config=Path(runtime.config_path.name),
            initialize_only=True,
            revision=revision,
            process_env=process_env,
        )
    except (*CONFIG_LOAD_USER_ERROR_TYPES, ValueError) as exc:
        typer.echo(f"Bundle initialization failed: {exc}", err=True)
        raise typer.Exit(2) from None


def _install_json(result: BundleInstallResult | None) -> dict[str, object] | None:
    return None if result is None else {**asdict(result), "config_path": str(result.config_path)}


def config_install_bundle(
    source: Path = typer.Argument(..., help="Directory containing the complete configuration tree."),  # noqa: B008
    target: Path = typer.Option(..., help="Directory to install; its sibling TARGET.previous retains rollback."),  # noqa: B008
    config: Path = typer.Option(Path("config.yaml"), help="Config file path relative to the bundle root."),  # noqa: B008
    initialize_only: bool = typer.Option(False, help="Keep an existing target unless a declared revision changed."),
    force: bool = typer.Option(False, help="Explicitly replace authored edits or an unmanaged target."),
    source_only: bool = typer.Option(False, help="Refuse changes outside the YAML/include sources of --config."),
    expected_digest: str | None = typer.Option(None, help="Require this whole-tree candidate digest."),
    revision: str | None = typer.Option(None, help="Record this bootstrap revision in the installed tree."),
    json_output: bool = typer.Option(False, "--json", help="Print a filesystem receipt as JSON."),
) -> None:
    """Validate and install a complete tree; use check-applied to confirm runtime reload."""
    # Defer the Pydantic/cryptography config graph until this command runs,
    # preserving the slim CLI import contract.
    from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES  # noqa: PLC0415
    from mindroom.config_bundle import install_config_bundle  # noqa: PLC0415

    try:
        # Native config loading may log to stdout before logging is configured.
        with redirect_stdout(sys.stderr):
            result = install_config_bundle(
                source,
                target,
                config=config,
                initialize_only=initialize_only,
                force=force,
                source_only=source_only,
                expected_digest=expected_digest,
                revision=revision,
            )
    except (*CONFIG_LOAD_USER_ERROR_TYPES, ValueError) as exc:
        if json_output:
            typer.echo(json.dumps({"status": "failed", "detail": str(exc)}))
        else:
            typer.echo(f"Bundle installation failed: {exc}", err=True)
        raise typer.Exit(2) from None
    if json_output:
        typer.echo(json.dumps(_install_json(result)))
    else:
        typer.echo(f"Bundle {result.status}: {result.config_path}")
        if result.digest:
            typer.echo(f"Bundle digest: {result.digest}")
        if result.fingerprint:
            typer.echo(f"Source fingerprint: {result.fingerprint}; use config check-applied to confirm runtime reload.")
        if result.recovery_pending:
            typer.echo("Bundle is active; retry to finish previous-tree rotation or cleanup.", err=True)


def config_classify_change(
    old: Path = typer.Argument(..., help="Directory containing the current tree, such as the install target."),  # noqa: B008
    new: Path = typer.Argument(..., help="Directory containing the candidate tree."),  # noqa: B008
    config: list[Path] = typer.Option(  # noqa: B008
        [Path("config.yaml")],
        help="Config entrypoint relative to both roots; repeat for several.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Classify tree differences; exit 0 YAML/include sources only, 1 other changes, 2 error."""
    # Defer the Pydantic/cryptography config graph until this command runs,
    # preserving the slim CLI import contract.
    from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES  # noqa: PLC0415
    from mindroom.config_bundle import classify_bundle_change  # noqa: PLC0415

    try:
        change = classify_bundle_change(old, new, config)
    except (*CONFIG_LOAD_USER_ERROR_TYPES, ValueError) as exc:
        if json_output:
            typer.echo(json.dumps({"status": "failed", "detail": str(exc)}))
        else:
            typer.echo(f"Change classification failed: {exc}", err=True)
        raise typer.Exit(2) from None
    status = "non_source" if change.other else "source_only"
    if json_output:
        typer.echo(json.dumps({"status": status, **asdict(change)}))
    else:
        typer.echo(f"{len(change.sources)} YAML/include source path(s) changed.")
        if change.other:
            typer.echo("Changes outside YAML/include sources, which config reload does not reread:")
            for name in change.other:
                typer.echo(f"  {name}")
    raise typer.Exit(1 if change.other else 0)


# Runtime results that settle an apply without rollback.
_SETTLED: dict[str, tuple[_ApplyStatus, str]] = {
    "applied": ("applied", "The runtime applied the candidate."),
    "pending": ("pending", "The runtime has not settled the candidate; confirm it later with config check-applied."),
    "restart_required": (
        "restart_required",
        "The runtime adopted the candidate but needs a restart to apply it fully.",
    ),
}


@dataclass(frozen=True)
class _ApplyReceipt:
    """Final outcome of one apply; only `applied` confirms the candidate."""

    status: _ApplyStatus
    detail: str
    install: BundleInstallResult | None = None
    runtime_status: str | None = None
    rollback: BundleInstallResult | None = None
    rollback_runtime_status: str | None = None


def _settled_status(
    runtime_paths: RuntimePaths,
    url: str | None,
    fingerprint: str | None,
    wait: float,
    timeout: float,
) -> str:
    """Return the runtime result for one fingerprint, or unavailable when it cannot be read."""
    if fingerprint is None:
        return "unavailable"
    try:
        return wait_for_applied(runtime_paths, url, fingerprint, wait, timeout).status
    except (ValueError, OSError):
        return "unavailable"


def _status_before_install(
    runtime_paths: RuntimePaths,
    url: str | None,
    wait: float,
    timeout: float,
) -> ConfigReloadStatus:
    """Prove the runtime result can be read before anything changes."""
    if not math.isfinite(wait):
        msg = "--wait must be finite."
        raise ValueError(msg)
    status = request_reload_status(runtime_paths, url, timeout)
    if status.status == "unavailable":
        msg = "MindRoom has no config reload status yet; nothing was installed."
        raise ValueError(msg)
    return status


def _apply_bundle(  # noqa: PLR0911 - one return per final receipt status
    source: Path,
    target: Path,
    *,
    config: Path,
    expected_digest: str | None,
    rollback_on_failure: bool,
    url: str | None,
    wait: float,
    timeout: float,
) -> _ApplyReceipt:
    # Defer the Pydantic/cryptography config graph until this command runs,
    # preserving the slim CLI import contract.
    from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES  # noqa: PLC0415
    from mindroom.config_bundle import install_config_bundle  # noqa: PLC0415

    try:
        runtime_paths = activate_cli_runtime(target / config)
        before = _status_before_install(runtime_paths, url, wait, timeout)
        # Native config loading may log to stdout before logging is configured.
        with redirect_stdout(sys.stderr):
            install = install_config_bundle(
                source,
                target,
                config=config,
                source_only=True,  # The runtime fingerprint covers only YAML/include sources.
                expected_digest=expected_digest,
            )
    except (*CONFIG_LOAD_USER_ERROR_TYPES, ValueError) as exc:
        return _ApplyReceipt("failed", str(exc))
    status = _settled_status(runtime_paths, url, install.fingerprint, wait, timeout)
    if status in _SETTLED:
        return _ApplyReceipt(*_SETTLED[status], install, status)
    # A reload already pending or failed for this or a not-yet-known fingerprint cannot confirm the new tree.
    stale = (
        install.status == "installed"
        and before.fingerprint in {install.fingerprint, None}
        and before.status in {"pending", "failed"}
    )
    if status != "failed" or stale:
        detail = "The candidate is installed, but its runtime result is unconfirmed; inspect before retrying."
        return _ApplyReceipt("unconfirmed", detail, install, status)
    if not rollback_on_failure or install.previous_digest is None:
        return _ApplyReceipt("failed", "The runtime rejected the candidate, which remains installed.", install, status)
    active = target.expanduser().absolute()
    try:
        with redirect_stdout(sys.stderr):
            rollback = install_config_bundle(
                active.with_name(f"{active.name}.previous"),
                target,
                config=config,
                expected_digest=install.previous_digest,
            )
    except (*CONFIG_LOAD_USER_ERROR_TYPES, ValueError) as exc:
        detail = f"The runtime rejected the candidate, and restoring the previous tree failed: {exc}"
        return _ApplyReceipt("unconfirmed", detail, install, status)
    rollback_status = _settled_status(runtime_paths, url, rollback.fingerprint, wait, timeout)
    if rollback_status == "applied":
        detail = "The runtime rejected the candidate; the previous tree is restored and applied."
        return _ApplyReceipt("rolled_back", detail, install, status, rollback, rollback_status)
    detail = "The runtime rejected the candidate; the previous tree is restored but not reported applied."
    return _ApplyReceipt("unconfirmed", detail, install, status, rollback, rollback_status)


def config_apply_bundle(
    source: Path = typer.Argument(..., help="Directory containing the complete configuration tree."),  # noqa: B008
    target: Path = typer.Option(..., help="Directory the running MindRoom loads; TARGET.previous retains rollback."),  # noqa: B008
    config: Path = typer.Option(Path("config.yaml"), help="Config file path relative to the bundle root."),  # noqa: B008
    expected_digest: str | None = typer.Option(None, help="Require this whole-tree candidate digest."),
    rollback_on_failure: bool = typer.Option(
        False,
        "--rollback-on-failure",
        help="Restore the digest-pinned previous tree when the runtime rejects a changed candidate.",
    ),
    url: str | None = typer.Option(None, help="MindRoom base URL; defaults to MINDROOM_URL or localhost:8765."),
    wait: float = typer.Option(300.0, min=0.0, help="Wait up to this many seconds for each runtime result."),
    timeout: float = typer.Option(10.0, min=0.001, help="HTTP timeout in seconds."),
    json_output: bool = typer.Option(False, "--json", help="Print the final receipt as JSON."),
) -> None:
    """Hot-apply a source-only tree change; exit 0 applied, 1 pending, 2 failed, 3 rolled back, 4 unconfirmed, 5 restart."""
    receipt = _apply_bundle(
        source,
        target,
        config=config,
        expected_digest=expected_digest,
        rollback_on_failure=rollback_on_failure,
        url=url,
        wait=wait,
        timeout=timeout,
    )
    if json_output:
        payload = {
            **asdict(receipt),
            "install": _install_json(receipt.install),
            "rollback": _install_json(receipt.rollback),
        }
        typer.echo(json.dumps(payload))
    else:
        typer.echo(f"Bundle apply {receipt.status}: {receipt.detail}")
    raise typer.Exit(_APPLY_EXIT_CODES[receipt.status])
