"""Configuration migration CLI command."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import typer

from mindroom import constants
from mindroom.cli.config import CONFIG_PATH_OPTION, console, format_validation_errors, validate_config_source_quiet


def _resolve_config_path(path: Path | None) -> Path:
    """Resolve the config file path from explicit argument or default."""
    if path is not None:
        return path.expanduser().resolve()
    return constants.resolve_primary_runtime_paths(process_env=constants.exported_process_env()).config_path.resolve()


def config_migrate(
    path: Path | None = CONFIG_PATH_OPTION,
) -> None:
    """Migrate config.yaml to membership access settings."""
    config_file = _resolve_config_path(path)

    if not config_file.exists():
        console.print(f"[yellow]No config file found at:[/yellow] {config_file}")
        console.print("\nRun [cyan]mindroom config init[/cyan] to create one.")
        raise typer.Exit(1)

    try:
        original = config_file.read_bytes()
        original.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        format_validation_errors(exc, config_path=config_file)
        raise typer.Exit(1) from None

    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_file,
        process_env=constants.exported_process_env(),
    )
    from yaml import YAMLError  # noqa: PLC0415

    try:
        validate_config_source_quiet(
            runtime_paths,
            source=original,
            original=original,
            tolerate_plugin_load_errors=True,
        )
        migrated_access = config_file.read_bytes() != original
    except (ValueError, YAMLError, OSError) as exc:
        format_validation_errors(exc, config_path=config_file)
        raise typer.Exit(1) from None

    if not migrated_access:
        console.print("[green]No migrations applied.[/green]")
        return

    console.print("[green]Applied migration:[/green] membership access schema")
    console.print(f"[green]Config updated:[/green] {config_file}")
