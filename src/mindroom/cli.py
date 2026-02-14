"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console

from mindroom import __version__
from mindroom.cli_config import _check_env_keys, _load_config_quiet, config_app
from mindroom.constants import DEFAULT_AGENTS_CONFIG, STORAGE_PATH

app = typer.Typer(
    help="MindRoom - AI agents that live in Matrix\n\nQuick start:\n  mindroom config init   Create a starter config\n  mindroom run           Start the system",
    pretty_exceptions_enable=True,
    # Disable showing locals which can be very large (also see `setup_logging`)
    pretty_exceptions_show_locals=False,
)
app.add_typer(config_app, name="config")
console = Console()


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
        _print_missing_config_error(config_path)
        raise typer.Exit(1)

    # Validate config early so users get a clear message instead of a traceback
    try:
        config = _load_config_quiet(config_path)
    except ValidationError as exc:
        _print_validation_error(exc, config_path)
        raise typer.Exit(1) from None
    except Exception as exc:
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


# ---------------------------------------------------------------------------
# Friendly error output helpers
# ---------------------------------------------------------------------------


def _print_missing_config_error(config_path: Path) -> None:
    console.print("[red]Error:[/red] No config.yaml found.\n")
    console.print("MindRoom needs a configuration file to know which agents to run.\n")
    console.print("Quick start:")
    console.print("  [cyan]mindroom config init[/cyan]    Create a starter config")
    console.print("  [cyan]mindroom config edit[/cyan]    Edit your config\n")
    console.print("Config search locations:")
    console.print(f"  1. {config_path.resolve()}")
    env_path = os.getenv("MINDROOM_CONFIG_PATH") or os.getenv("CONFIG_PATH")
    if env_path:
        console.print(f"  2. {env_path} (from environment)")
    console.print("\nLearn more: https://github.com/mindroom-ai/mindroom")


def _print_validation_error(exc: ValidationError, config_path: Path) -> None:
    console.print(f"[red]Error:[/red] Invalid configuration in {config_path}\n")
    console.print("Issues found:")
    for error in exc.errors():
        loc = " -> ".join(str(x) for x in error["loc"])
        console.print(f"  [red]*[/red] {loc}: {error['msg']}")
    console.print("\nFix these issues:")
    console.print("  [cyan]mindroom config edit[/cyan]      Edit your config")
    console.print("  [cyan]mindroom config validate[/cyan]  Check config after editing")


def _print_connection_error(exc: BaseException) -> None:
    homeserver = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
    console.print("[red]Error:[/red] Could not connect to the Matrix homeserver.\n")
    console.print(f"  Details: {exc}\n")
    console.print("Check that:")
    console.print("  1. Your Matrix homeserver is running")
    console.print(f"  2. MATRIX_HOMESERVER is set correctly (current: {homeserver})")
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
