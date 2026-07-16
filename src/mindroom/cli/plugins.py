"""Plugin validation, install, and update commands."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import typer

from mindroom import constants

from .config import console

if TYPE_CHECKING:
    from mindroom.plugin_install import InstallResult

plugins_app = typer.Typer(help="Validate and vendor external MindRoom plugins.")

_PLUGINS_DIR_OPTION: Path | None = typer.Option(
    None,
    "--plugins-dir",
    help="Plugin vendor directory (defaults to <config dir>/plugins).",
)


@plugins_app.command("check")
def plugin_check(
    path: Path = typer.Argument(  # noqa: B008
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Plugin directory containing mindroom.plugin.json.",
    ),
) -> None:
    """Strictly validate one plugin against this MindRoom version."""
    from mindroom.plugin_check import check_plugin  # noqa: PLC0415

    try:
        result = check_plugin(path)
    except Exception as exc:
        console.print(f"[red]Plugin check failed:[/red] {exc}")
        raise typer.Exit(1) from None

    console.print(f"[green]Plugin is compatible:[/green] {result.name}")
    console.print(f"  Tools:  {', '.join(result.tool_names) or 'none'}")
    console.print(f"  Hooks:  {', '.join(result.hook_names) or 'none'}")
    console.print(f"  Skills: {', '.join(result.skill_directories) or 'none'}")


@plugins_app.command("install")
def plugin_install(
    spec: str = typer.Argument(
        ...,
        help="Plugin to install as NAME, OWNER/REPO, or OWNER/REPO@REF (bare names use mindroom-ai).",
    ),
    plugins_dir: Path | None = _PLUGINS_DIR_OPTION,
) -> None:
    """Vendor one plugin from GitHub after it passes the strict compatibility check."""
    from mindroom.plugin_install import install_plugin, parse_plugin_spec  # noqa: PLC0415

    try:
        result = install_plugin(parse_plugin_spec(spec), _resolved_plugins_dir(plugins_dir))
    except Exception as exc:
        console.print(f"[red]Plugin install failed:[/red] {exc}")
        raise typer.Exit(1) from None

    _print_installed(result)
    console.print("\nAdd it to config.yaml:")
    console.print(f"plugins:\n  - path: {_config_snippet_path(result.directory)}")


@plugins_app.command("update")
def plugin_update(
    name: str | None = typer.Argument(None, help="Installed plugin directory name."),
    ref: str | None = typer.Option(None, "--ref", help="Repin to a different git reference."),
    update_all: bool = typer.Option(False, "--all", help="Update every vendored plugin."),
    plugins_dir: Path | None = _PLUGINS_DIR_OPTION,
) -> None:
    """Update vendored plugins to the latest commit of their pinned reference."""
    from mindroom.plugin_install import find_locked_plugin_dirs, update_plugin  # noqa: PLC0415

    if update_all == (name is not None):
        console.print("[red]Pass exactly one of NAME or --all.[/red]")
        raise typer.Exit(2)
    if update_all and ref is not None:
        console.print("[red]--ref requires a single plugin NAME.[/red]")
        raise typer.Exit(2)

    resolved_plugins_dir = _resolved_plugins_dir(plugins_dir)
    directories = find_locked_plugin_dirs(resolved_plugins_dir) if update_all else (resolved_plugins_dir / str(name),)
    if not directories:
        console.print(f"No vendored plugins found in {resolved_plugins_dir}")
        return

    failed = False
    for directory in directories:
        try:
            update = update_plugin(directory, ref=ref)
        except Exception as exc:
            failed = True
            console.print(f"[red]Plugin update failed:[/red] {directory.name}: {exc}")
            continue
        if update.installed is None:
            console.print(f"Already up to date: {directory.name} ({update.previous_commit[:12]})")
        else:
            _print_installed(update.installed, previous_commit=update.previous_commit)
    if failed:
        raise typer.Exit(1)


def _print_installed(result: InstallResult, previous_commit: str | None = None) -> None:
    transition = f"{previous_commit[:12]} -> " if previous_commit else ""
    console.print(
        f"[green]Installed plugin:[/green] {result.name} "
        f"({result.lock.repository}@{transition}{result.lock.commit[:12]})",
    )
    console.print(f"  Location: {result.directory}")
    if result.has_pyproject:
        console.print(
            "[yellow]  Note: the plugin declares pyproject.toml dependencies; "
            "they are not installed automatically.[/yellow]",
        )


def _resolved_plugins_dir(plugins_dir: Path | None) -> Path:
    if plugins_dir is not None:
        return plugins_dir.expanduser().resolve()
    runtime_paths = constants.resolve_runtime_paths(process_env=constants.exported_process_env())
    return runtime_paths.config_dir / "plugins"


def _config_snippet_path(directory: Path) -> str:
    runtime_paths = constants.resolve_runtime_paths(process_env=constants.exported_process_env())
    if directory.is_relative_to(runtime_paths.config_dir):
        return directory.relative_to(runtime_paths.config_dir).as_posix()
    return str(directory)
