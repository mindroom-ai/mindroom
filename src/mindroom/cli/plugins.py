"""Plugin validation, install, and update commands."""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext, redirect_stdout
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import typer
from rich.markup import escape

from .config import CONFIG_PATH_OPTION, activate_cli_runtime, console

if TYPE_CHECKING:
    from mindroom.plugin_check import OutsideImport
    from mindroom.plugin_install import InstallResult

plugins_app = typer.Typer(help="Check, install, and update MindRoom plugins.")

_OUTSIDE_IMPORTS = "imports from outside the plugin API can break in any MindRoom release"

_PLUGINS_DIR_OPTION: Path | None = typer.Option(
    None,
    "--plugins-dir",
    help="Plugin vendor directory (defaults to <config dir>/plugins).",
)


@plugins_app.command("check")
def plugin_check(
    path: str = typer.Argument(
        ...,
        help="Plugin directory or Python package spec, in any form a config.yaml plugins entry takes.",
    ),
    strict: bool = typer.Option(False, "--strict", help="Treat imports from outside the plugin API as errors."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Strictly validate one plugin against this MindRoom version."""
    from mindroom.plugin_check import check_plugin, imports_outside_plugin_api, resolve_plugin_root  # noqa: PLC0415

    outside_imports: list[OutsideImport] = []
    try:
        # With --json, logs and anything the plugin prints go to stderr, so stdout carries only the JSON document.
        with redirect_stdout(sys.stderr) if json_output else nullcontext():
            plugin_root = resolve_plugin_root(path)
            outside_imports = imports_outside_plugin_api(plugin_root)
            result = check_plugin(plugin_root)
    except Exception as exc:
        if json_output:
            failure = {
                "status": "failed",
                "detail": str(exc),
                "internal_imports": _internal_imports_json(outside_imports),
            }
            typer.echo(json.dumps(failure))
        else:
            _print_literal("[red]Plugin check failed:[/red] ", exc)
            _print_outside_imports(outside_imports, strict=strict)
        raise typer.Exit(1) from None

    strict_failure = strict and bool(outside_imports)
    if json_output:
        summary = {
            "status": "compatible",
            "name": result.name,
            "tools": result.tool_names,
            "hooks": result.hook_names,
            "automations": result.automation_names,
            "skills": result.skill_directories,
            "internal_imports": _internal_imports_json(outside_imports),
        }
        if strict_failure:
            summary |= {"status": "failed", "detail": _OUTSIDE_IMPORTS}
        typer.echo(json.dumps(summary))
        if strict_failure:
            raise typer.Exit(1)
        return
    _print_literal("[green]Plugin is compatible:[/green] ", result.name)
    _print_literal("  Tools:       ", ", ".join(result.tool_names) or "none")
    _print_literal("  Hooks:       ", ", ".join(result.hook_names) or "none")
    _print_literal("  Automations: ", ", ".join(result.automation_names) or "none")
    _print_literal("  Skills:      ", ", ".join(result.skill_directories) or "none")
    _print_outside_imports(outside_imports, strict=strict)
    if strict_failure:
        raise typer.Exit(1)


def _print_literal(markup: str, text: object) -> None:
    """Print fixed Rich markup followed by text shown as is, with no markup or emoji codes read from the text."""
    console.print(f"{markup}{escape(str(text))}", emoji=False)


def _internal_imports_json(outside_imports: list[OutsideImport]) -> list[dict[str, str | int]]:
    return [{"path": found.path.as_posix(), "line": found.line, "name": found.name} for found in outside_imports]


def _print_outside_imports(outside_imports: list[OutsideImport], *, strict: bool) -> None:
    if outside_imports:
        label = "[red]Error:[/red]" if strict else "[yellow]Warning:[/yellow]"
        console.print(f"{label} {_OUTSIDE_IMPORTS}:")
        for found in outside_imports:
            console.out(f"  {found.path}:{found.line}: {found.name}")


@plugins_app.command("install")
def plugin_install(
    spec: str = typer.Argument(
        ...,
        help="Plugin to install as NAME, OWNER/REPO, or OWNER/REPO@REF (bare names use mindroom-ai).",
    ),
    path: Path | None = CONFIG_PATH_OPTION,
    plugins_dir: Path | None = _PLUGINS_DIR_OPTION,
) -> None:
    """Install one plugin from GitHub after a strict compatibility check."""
    from mindroom.plugin_install import install_plugin, parse_plugin_spec  # noqa: PLC0415

    config_dir = activate_cli_runtime(path).config_dir
    try:
        result = install_plugin(parse_plugin_spec(spec), _resolved_plugins_dir(plugins_dir, config_dir))
    except Exception as exc:
        _print_literal("[red]Plugin install failed:[/red] ", exc)
        raise typer.Exit(1) from None

    _print_installed(result)
    console.print("\nAdd it to config.yaml:")
    _print_literal("plugins:\n  - path: ", _config_snippet_path(result.directory, config_dir))


@plugins_app.command("update")
def plugin_update(
    name: str | None = typer.Argument(None, help="Installed plugin directory name."),
    ref: str | None = typer.Option(None, "--ref", help="Repin to a different git reference."),
    update_all: bool = typer.Option(False, "--all", help="Update every vendored plugin."),
    path: Path | None = CONFIG_PATH_OPTION,
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
    if name is not None and (not name or name in {".", ".."} or "/" in name or "\\" in name):
        _print_literal("[red]Invalid plugin name (expected a plain directory name):[/red] ", name)
        raise typer.Exit(2)

    resolved_plugins_dir = _resolved_plugins_dir(plugins_dir, activate_cli_runtime(path).config_dir)
    directories = find_locked_plugin_dirs(resolved_plugins_dir) if update_all else (resolved_plugins_dir / str(name),)
    if not directories:
        _print_literal("No vendored plugins found in ", resolved_plugins_dir)
        return

    failed = False
    for directory in directories:
        try:
            update = update_plugin(directory, ref=ref)
        except Exception as exc:
            failed = True
            _print_literal("[red]Plugin update failed:[/red] ", f"{directory.name}: {exc}")
            continue
        if update.installed is None:
            _print_literal("Already up to date: ", f"{directory.name} ({update.previous_commit[:12]})")
        else:
            _print_installed(update.installed, previous_commit=update.previous_commit)
    if failed:
        raise typer.Exit(1)


def _print_installed(result: InstallResult, previous_commit: str | None = None) -> None:
    transition = f"{previous_commit[:12]} -> " if previous_commit else ""
    _print_literal(
        "[green]Installed plugin:[/green] ",
        f"{result.name} ({result.lock.repository}@{transition}{result.lock.commit[:12]})",
    )
    _print_literal("  Location: ", result.directory)
    if result.has_pyproject:
        console.print(
            "[yellow]  Note: the plugin declares pyproject.toml dependencies; "
            "they are not installed automatically.[/yellow]",
        )


def _resolved_plugins_dir(plugins_dir: Path | None, config_dir: Path) -> Path:
    if plugins_dir is not None:
        return plugins_dir.expanduser().resolve()
    return config_dir / "plugins"


def _config_snippet_path(directory: Path, config_dir: Path) -> str:
    if directory.is_relative_to(config_dir):
        return directory.relative_to(config_dir).as_posix()
    return str(directory)
