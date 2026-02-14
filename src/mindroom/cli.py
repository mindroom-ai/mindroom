"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import typer
import yaml
from pydantic import ValidationError

from mindroom import __version__

if TYPE_CHECKING:
    from mindroom.config import Config
from mindroom.cli_config import (
    _check_env_keys,
    _config_search_locations,
    _find_missing_env_keys,
    _format_validation_errors,
    _load_config_quiet,
    config_app,
    console,
)
from mindroom.constants import (
    DEFAULT_AGENTS_CONFIG,
    MATRIX_HOMESERVER,
    MATRIX_SSL_VERIFY,
    STORAGE_PATH,
)

app = typer.Typer(
    help="MindRoom - AI agents that live in Matrix\n\nQuick start:\n  mindroom config init   Create a starter config\n  mindroom run           Start the system",
    pretty_exceptions_enable=True,
    # Disable showing locals which can be very large (also see `setup_logging`)
    pretty_exceptions_show_locals=False,
)
app.add_typer(config_app, name="config")


@app.command()
def version() -> None:
    """Show the current version of Mindroom."""
    console.print(f"Mindroom version: [bold]{__version__}[/bold]")
    console.print("AI agents that live in Matrix")


@app.command()
def run(
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Set the logging level (DEBUG, INFO, WARNING, ERROR)",
        case_sensitive=False,
    ),
    storage_path: Path = typer.Option(  # noqa: B008
        Path(STORAGE_PATH),
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data (state, sessions, tracking)",
    ),
) -> None:
    """Run the mindroom multi-agent system.

    This command starts the multi-agent bot system which automatically:
    - Creates all necessary user and agent accounts
    - Creates all rooms defined in config.yaml
    - Manages agent room memberships
    """
    asyncio.run(_run(log_level=log_level.upper(), storage_path=storage_path))


async def _run(log_level: str, storage_path: Path) -> None:
    """Run the multi-agent system with friendly error handling."""
    # Check config exists before starting
    config_path = Path(DEFAULT_AGENTS_CONFIG)
    if not config_path.exists():
        _print_missing_config_error()
        raise typer.Exit(1)

    # Validate config early so users get a clear message instead of a traceback
    try:
        config = _load_config_quiet(config_path)
    except ValidationError as exc:
        _format_validation_errors(exc, config_path)
        raise typer.Exit(1) from None
    except (yaml.YAMLError, OSError) as exc:
        console.print(f"[red]Error:[/red] Could not load configuration: {exc}")
        console.print("\n  [cyan]mindroom config validate[/cyan]  Check your config")
        raise typer.Exit(1) from None

    # Check for missing API keys
    _check_env_keys(config)

    console.print(f"Starting Mindroom (log level: {log_level})...")
    console.print("Press Ctrl+C to stop\n")

    try:
        from mindroom.bot import main as bot_main  # noqa: PLC0415  # lazy: heavy import

        await bot_main(log_level=log_level, storage_path=storage_path)
    except KeyboardInterrupt:
        console.print("\nStopped")
    except ConnectionError as exc:
        _print_connection_error(exc)
        raise typer.Exit(1) from None
    except OSError as exc:
        if "connect" in str(exc).lower() or "refused" in str(exc).lower():
            _print_connection_error(exc)
            raise typer.Exit(1) from None
        raise


@app.command()
def doctor() -> None:
    """Check your environment for common issues.

    Runs connectivity, configuration, and credential checks in a single pass
    so you can fix everything before running `mindroom run`.
    """
    console.print("[bold]MindRoom Doctor[/bold]\n")

    passed = 0
    failed = 0
    warnings = 0

    config_path = Path(DEFAULT_AGENTS_CONFIG)

    # 1. Config file exists
    p, f, w = _check_config_exists(config_path)
    passed += p
    failed += f
    warnings += w

    # 2-3. Config validity + API keys (skip if file missing)
    if config_path.exists():
        config, p, f, w = _check_config_valid(config_path)
        passed += p
        failed += f
        warnings += w
        if config is not None:
            p, f, w = _check_api_keys(config)
            passed += p
            failed += f
            warnings += w

    # 4. Matrix homeserver reachable
    p, f, w = _check_matrix_homeserver()
    passed += p
    failed += f
    warnings += w

    # 5. Storage directory writable
    p, f, w = _check_storage_writable()
    passed += p
    failed += f
    warnings += w

    # Summary
    console.print(f"\n{passed} passed, {failed} failed, {warnings} warning{'s' if warnings != 1 else ''}")

    if failed > 0:
        raise typer.Exit(1)


def _check_config_exists(config_path: Path) -> tuple[int, int, int]:
    """Check config file exists. Returns (passed, failed, warnings)."""
    if config_path.exists():
        console.print(f"[green]✓[/green] Config file: {config_path}")
        return 1, 0, 0
    console.print(f"[red]✗[/red] Config file not found: {config_path}")
    return 0, 1, 0


def _check_config_valid(config_path: Path) -> tuple[Config | None, int, int, int]:
    """Validate config file. Returns (config_or_none, passed, failed, warnings)."""
    try:
        config = _load_config_quiet(config_path)
    except ValidationError as exc:
        n = len(exc.errors())
        console.print(f"[red]✗[/red] Config invalid ({n} validation error{'s' if n != 1 else ''})")
        return None, 0, 1, 0
    except (yaml.YAMLError, OSError) as exc:
        console.print(f"[red]✗[/red] Config invalid: {exc}")
        return None, 0, 1, 0
    agents = len(config.agents)
    teams = len(config.teams)
    models = len(config.models)
    rooms = len(config.get_all_configured_rooms())
    console.print(
        f"[green]✓[/green] Config valid"
        f" ({agents} agent{'s' if agents != 1 else ''},"
        f" {teams} team{'s' if teams != 1 else ''},"
        f" {models} model{'s' if models != 1 else ''},"
        f" {rooms} room{'s' if rooms != 1 else ''})",
    )
    return config, 1, 0, 0


def _check_api_keys(config: Config) -> tuple[int, int, int]:
    """Check API keys for configured providers. Returns (passed, failed, warnings)."""
    missing = _find_missing_env_keys(config)
    if missing:
        for provider, env_key in missing:
            console.print(f"[yellow]![/yellow] Missing env: {env_key} (provider: {provider})")
        return 0, 0, len(missing)
    console.print("[green]✓[/green] API keys set for all configured providers")
    return 1, 0, 0


def _check_matrix_homeserver() -> tuple[int, int, int]:
    """Check Matrix homeserver reachability. Returns (passed, failed, warnings)."""
    url = f"{MATRIX_HOMESERVER}/_matrix/client/versions"
    verify: bool = MATRIX_SSL_VERIFY
    try:
        resp = httpx.get(url, timeout=5, verify=verify)
        if not resp.is_success:
            console.print(
                f"[red]✗[/red] Matrix homeserver returned {resp.status_code}: {MATRIX_HOMESERVER}",
            )
            return 0, 1, 0
    except httpx.HTTPError as exc:
        console.print(f"[red]✗[/red] Matrix homeserver unreachable: {MATRIX_HOMESERVER} ({exc})")
        return 0, 1, 0
    console.print(f"[green]✓[/green] Matrix homeserver: {MATRIX_HOMESERVER}")
    return 1, 0, 0


def _check_storage_writable() -> tuple[int, int, int]:
    """Check storage directory is writable. Returns (passed, failed, warnings)."""
    storage = Path(STORAGE_PATH)
    try:
        storage.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=storage)
        os.close(fd)
        Path(tmp).unlink()
    except OSError as exc:
        console.print(f"[red]✗[/red] Storage not writable: {storage} ({exc})")
        return 0, 1, 0
    console.print(f"[green]✓[/green] Storage writable: {storage}/")
    return 1, 0, 0


# ---------------------------------------------------------------------------
# Friendly error output helpers
# ---------------------------------------------------------------------------


def _print_missing_config_error() -> None:
    console.print("[red]Error:[/red] No config.yaml found.\n")
    console.print("MindRoom needs a configuration file to know which agents to run.\n")
    console.print("Quick start:")
    console.print("  [cyan]mindroom config init[/cyan]    Create a starter config")
    console.print("  [cyan]mindroom config edit[/cyan]    Edit your config\n")
    console.print("Config search locations:")
    for loc in _config_search_locations():
        status = "[green]exists[/green]" if loc.exists() else "[dim]not found[/dim]"
        console.print(f"  - {loc} ({status})")
    console.print("\nLearn more: https://github.com/mindroom-ai/mindroom")


def _print_connection_error(exc: BaseException) -> None:
    from mindroom.constants import MATRIX_HOMESERVER  # noqa: PLC0415

    console.print("[red]Error:[/red] Could not connect to the Matrix homeserver.\n")
    console.print(f"  Details: {exc}\n")
    console.print("Check that:")
    console.print("  1. Your Matrix homeserver is running")
    console.print(f"  2. MATRIX_HOMESERVER is set correctly (current: {MATRIX_HOMESERVER})")
    console.print("  3. The server is reachable from this machine")


def main() -> None:
    """Main entry point that shows help by default."""
    # Handle -h flag by replacing with --help
    for i, arg in enumerate(sys.argv):
        if arg == "-h":
            sys.argv[i] = "--help"
            break

    # If no arguments provided, show help
    if len(sys.argv) == 1:
        sys.argv.append("--help")

    app()


if __name__ == "__main__":
    main()
