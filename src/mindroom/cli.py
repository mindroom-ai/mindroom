"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from mindroom import __version__
from mindroom.cli_config import (
    _check_env_keys,
    _config_search_locations,
    _format_validation_errors,
    _load_config_quiet,
    config_app,
    console,
)
from mindroom.constants import DEFAULT_AGENTS_CONFIG, STORAGE_PATH

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
    api: bool = typer.Option(
        True,
        "--api/--no-api",
        help="Start the dashboard API server alongside the bot",
    ),
    api_port: int = typer.Option(
        8765,
        "--api-port",
        help="Port for the dashboard API server",
    ),
    api_host: str = typer.Option(
        "0.0.0.0",  # noqa: S104
        "--api-host",
        help="Host for the dashboard API server",
    ),
) -> None:
    """Run the mindroom multi-agent system.

    This command starts the multi-agent bot system which automatically:
    - Creates all necessary user and agent accounts
    - Creates all rooms defined in config.yaml
    - Manages agent room memberships
    - Starts the dashboard API server (disable with --no-api)
    """
    asyncio.run(
        _run(
            log_level=log_level.upper(),
            storage_path=storage_path,
            api=api,
            api_port=api_port,
            api_host=api_host,
        ),
    )


async def _run(
    log_level: str,
    storage_path: Path,
    *,
    api: bool,
    api_port: int,
    api_host: str,
) -> None:
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
    if api:
        console.print(f"Dashboard API: http://{api_host}:{api_port}")
    console.print("Press Ctrl+C to stop\n")

    try:
        from mindroom.bot import main as bot_main  # noqa: PLC0415  # lazy: heavy import

        await bot_main(
            log_level=log_level,
            storage_path=storage_path,
            api=api,
            api_port=api_port,
            api_host=api_host,
        )
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
