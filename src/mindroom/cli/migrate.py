"""Configuration migration CLI command."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003

import typer

from mindroom.cli.config import (
    CONFIG_PATH_OPTION,
    activate_cli_runtime,
    console,
    format_validation_errors,
    load_config_quiet,
)


def config_migrate(
    path: Path | None = CONFIG_PATH_OPTION,
) -> None:
    """Migrate config.yaml to membership access settings."""
    runtime_paths = activate_cli_runtime(path)
    config_file = runtime_paths.config_path

    if not config_file.exists():
        console.print(f"[yellow]No config file found at:[/yellow] {config_file}")
        console.print("\nRun [cyan]mindroom config init[/cyan] to create one.")
        raise typer.Exit(1)

    from yaml import YAMLError  # noqa: PLC0415

    try:
        original = config_file.read_bytes()
        load_config_quiet(runtime_paths, tolerate_plugin_load_errors=True)
        migrated_access = config_file.read_bytes() != original
    except (ValueError, YAMLError, OSError) as exc:
        format_validation_errors(exc, config_path=config_file)
        raise typer.Exit(1) from None

    if not migrated_access:
        console.print("[green]No migrations applied.[/green]")
        return

    console.print("[green]Applied migration:[/green] membership access schema")
    console.print(f"[green]Config updated:[/green] {config_file}")
