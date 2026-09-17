"""Install a configuration tree with whole-tree and runtime source receipts."""

from __future__ import annotations

import json
import sys
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path

import typer

from mindroom.cli.config import activate_cli_runtime
from mindroom.constants import exported_process_env


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


def config_install_bundle(
    source: Path = typer.Argument(..., help="Directory containing the complete configuration tree."),  # noqa: B008
    target: Path = typer.Option(..., help="Directory to install; its sibling TARGET.previous retains rollback."),  # noqa: B008
    config: Path = typer.Option(Path("config.yaml"), help="Config file path relative to the bundle root."),  # noqa: B008
    initialize_only: bool = typer.Option(False, help="Keep an existing target unless a declared revision changed."),
    force: bool = typer.Option(False, help="Explicitly replace authored edits or an unmanaged target."),
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
        typer.echo(json.dumps({**asdict(result), "config_path": str(result.config_path)}))
    else:
        typer.echo(f"Bundle {result.status}: {result.config_path}")
        if result.digest:
            typer.echo(f"Bundle digest: {result.digest}")
        if result.fingerprint:
            typer.echo(f"Source fingerprint: {result.fingerprint}; use config check-applied to confirm runtime reload.")
        if result.recovery_pending:
            typer.echo("Bundle is active; retry to finish previous-tree rotation or cleanup.", err=True)
