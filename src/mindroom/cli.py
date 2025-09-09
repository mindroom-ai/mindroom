"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from rich.console import Console

from mindroom import __version__
from mindroom.bot import main as bot_main
from mindroom.constants import STORAGE_PATH

app = typer.Typer(
    help="Mindroom: Multi-agent Matrix bot system",
    pretty_exceptions_enable=True,
    # Disable showing locals which can be very large (also see `setup_logging`)
    pretty_exceptions_show_locals=False,
)
console = Console()


@app.command()
def version() -> None:
    """Show the current version of Mindroom."""
    console.print(f"Mindroom version: [bold]{__version__}[/bold]")
    console.print("Multi-agent Matrix bot system")


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
    """Run the multi-agent system."""
    console.print(f"🚀 Starting Mindroom multi-agent system (log level: {log_level})...")
    console.print("Press Ctrl+C to stop\n")

    try:
        await bot_main(log_level=log_level, storage_path=storage_path)
    except KeyboardInterrupt:
        console.print("\n✋ Stopped")


def main() -> None:
    """Main entry point that shows help by default."""
    # Handle -h flag by replacing with --help
    for i, arg in enumerate(sys.argv):
        if arg == "-h":
            sys.argv[i] = "--help"
            break

    # If no arguments provided, show help
    if len(sys.argv) == 1:
        # Show help by appending --help to argv
        sys.argv.append("--help")

    app()


if __name__ == "__main__":
    main()
