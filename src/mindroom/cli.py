"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console

from mindroom import __version__
from mindroom.bot import main as bot_main
from mindroom.config import Config
from mindroom.constants import DEFAULT_AGENTS_CONFIG, STORAGE_PATH

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
def validate(
    config_path: Path = typer.Option(  # noqa: B008
        Path(DEFAULT_AGENTS_CONFIG),
        "--config",
        "-c",
        help="Path to the configuration file to validate",
    ),
) -> None:
    """Validate the configuration file.

    Parses the YAML configuration using Pydantic and reports any errors.
    """
    console.print(f"Validating configuration: [bold]{config_path}[/bold]")

    if not config_path.exists():
        console.print(f"[red]Error:[/red] Configuration file not found: {config_path}")
        raise typer.Exit(1)

    try:
        config = Config.from_yaml(config_path)
        console.print("[green]✓[/green] Configuration is valid!")
        console.print(f"  • {len(config.agents)} agent(s): {', '.join(config.agents.keys()) or 'none'}")
        console.print(f"  • {len(config.teams)} team(s): {', '.join(config.teams.keys()) or 'none'}")
        console.print(f"  • {len(config.models)} model(s): {', '.join(config.models.keys()) or 'none'}")
        rooms = config.get_all_configured_rooms()
        console.print(f"  • {len(rooms)} room(s): {', '.join(sorted(rooms)) or 'none'}")
    except ValidationError as e:
        console.print("[red]✗[/red] Configuration validation failed:")
        for error in e.errors():
            loc = " → ".join(str(x) for x in error["loc"])
            console.print(f"  [red]•[/red] {loc}: {error['msg']}")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]✗[/red] Error loading configuration: {e}")
        raise typer.Exit(1) from None


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


@app.command()
def proxy(
    upstream: str = typer.Option(
        "http://localhost:8765",
        "--upstream",
        "-u",
        help="MindRoom backend URL",
    ),
    port: int = typer.Option(
        8766,
        "--port",
        "-p",
        help="Port to listen on",
    ),
    host: str = typer.Option(
        "0.0.0.0",  # noqa: S104
        "--host",
        help="Host to bind to",
    ),
) -> None:
    """Run the tool-calling proxy for OpenAI-compatible UIs.

    The proxy sits between a chat UI and MindRoom, automatically
    executing tool calls server-side so the UI sees standard responses.
    """
    import uvicorn  # noqa: PLC0415

    from mindroom.proxy import ProxyConfig, create_proxy_app  # noqa: PLC0415

    proxy_config = ProxyConfig(upstream=upstream.rstrip("/"))
    proxy_app = create_proxy_app(proxy_config)

    console.print(f"Starting MindRoom proxy on [bold]{host}:{port}[/bold]")
    console.print(f"Upstream: [bold]{proxy_config.upstream}[/bold]")
    console.print(f"Point your UI at [bold]http://{host}:{port}/v1[/bold]")

    uvicorn.run(proxy_app, host=host, port=port)


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
