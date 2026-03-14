"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import sys
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import httpx
import typer
import yaml
from pydantic import ValidationError

import mindroom.cli.connect as cli_connect
from mindroom import __version__, constants
from mindroom.constants import (
    MATRIX_SSL_VERIFY,
    config_search_locations,
    ensure_writable_config_path,
    set_runtime_storage_path,
)
from mindroom.error_handling import AvatarGenerationError, AvatarSyncError
from mindroom.frontend_assets import ensure_frontend_dist_dir

from .banner import make_banner
from .config import (
    _check_env_keys,
    _format_validation_errors,
    _load_config_quiet,
    config_app,
    console,
)
from .doctor import doctor
from .local_stack import local_stack_setup

if TYPE_CHECKING:
    from mindroom.config.main import Config

_HELP = """\
AI agents that live in Matrix and work everywhere via bridges.

[bold]Quick start:[/bold]
  [cyan]mindroom config init[/cyan]   Create a starter config
  [cyan]mindroom run[/cyan]           Start the system\
"""

app = typer.Typer(
    help=_HELP,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    pretty_exceptions_enable=True,
    # Disable showing locals which can be very large (also see `setup_logging`)
    pretty_exceptions_show_locals=False,
)
avatars_app = typer.Typer(help="Generate and sync managed avatar assets.")
app.add_typer(config_app, name="config")
app.add_typer(avatars_app, name="avatars")


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
        envvar="LOG_LEVEL",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data (state, sessions, tracking)",
    ),
    api: bool = typer.Option(
        True,
        "--api/--no-api",
        help="Start the bundled dashboard/API server alongside the bot",
    ),
    api_port: int = typer.Option(
        8765,
        "--api-port",
        help="Port for the bundled dashboard/API server",
    ),
    api_host: str = typer.Option(
        "0.0.0.0",  # noqa: S104
        "--api-host",
        help="Host for the bundled dashboard/API server",
    ),
) -> None:
    """Run the mindroom multi-agent system.

    This command starts the multi-agent bot system which automatically:
    - Creates all necessary user and agent accounts
    - Creates all rooms defined in config.yaml
    - Manages agent room memberships
    - Starts the bundled dashboard/API server (disable with --no-api)
    """
    asyncio.run(
        _run(
            log_level=log_level.upper(),
            storage_path=storage_path or constants.get_runtime_paths().storage_root,
            api=api,
            api_port=api_port,
            api_host=api_host,
        ),
    )


def _load_active_config_or_exit() -> Config:
    """Load the active config file or exit with friendly validation errors."""
    ensure_writable_config_path()

    config_path = constants.runtime_config_path()
    if not config_path.exists():
        _print_missing_config_error()
        raise typer.Exit(1)

    try:
        config = _load_config_quiet(config_path)
    except ValidationError as exc:
        _format_validation_errors(exc, config_path)
        raise typer.Exit(1) from None
    except (yaml.YAMLError, OSError) as exc:
        console.print(f"[red]Error:[/red] Could not load configuration: {exc}")
        console.print("\n  [cyan]mindroom config validate[/cyan]  Check your config")
        raise typer.Exit(1) from None

    return config


async def _run(
    log_level: str,
    storage_path: Path,
    *,
    api: bool,
    api_port: int,
    api_host: str,
) -> None:
    """Run the multi-agent system with friendly error handling."""
    resolved_storage_path = set_runtime_storage_path(storage_path)
    config = _load_active_config_or_exit()

    # Check for missing API keys
    _check_env_keys(config)

    console.print(make_banner())
    console.print()
    console.print(f"Starting Mindroom (log level: {log_level})...")
    if api:
        frontend_dir = ensure_frontend_dist_dir()
        display_host = "localhost" if api_host == "0.0.0.0" else api_host  # noqa: S104
        if frontend_dir is None:
            console.print("Dashboard: unavailable (frontend assets missing)")
            console.print("  Install Bun or provide MINDROOM_FRONTEND_DIST when running from a source checkout.")
        else:
            console.print(f"Dashboard: http://{display_host}:{api_port}")
        console.print(f"API: http://{display_host}:{api_port}/api")
    console.print("Press Ctrl+C to stop\n")

    try:
        from mindroom.orchestrator import main as bot_main  # noqa: PLC0415  # lazy: heavy import

        await bot_main(
            log_level=log_level,
            storage_path=resolved_storage_path,
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


app.command()(doctor)


@avatars_app.command("generate")
def avatars_generate() -> None:
    """Generate missing managed avatar files in the workspace."""
    _load_active_config_or_exit()

    try:
        from mindroom.avatar_generation import run_avatar_generation  # noqa: PLC0415

        asyncio.run(run_avatar_generation())
    except AvatarGenerationError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None


@avatars_app.command("sync")
def avatars_sync() -> None:
    """Sync configured room and root-space avatars to Matrix using the initialized router account."""
    _load_active_config_or_exit()

    try:
        from mindroom.avatar_generation import set_room_avatars_in_matrix  # noqa: PLC0415

        asyncio.run(set_room_avatars_in_matrix())
    except AvatarSyncError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None
    except ConnectionError as exc:
        _print_connection_error(exc)
        raise typer.Exit(1) from None
    except OSError as exc:
        if "connect" in str(exc).lower() or "refused" in str(exc).lower():
            _print_connection_error(exc)
            raise typer.Exit(1) from None
        raise


@app.command()
def connect(
    pair_code: str = typer.Option(
        ...,
        "--pair-code",
        help="Pair code shown in chat UI (format: ABCD-EFGH).",
    ),
    provisioning_url: str | None = typer.Option(
        None,
        "--provisioning-url",
        help="Base URL for the MindRoom provisioning API.",
    ),
    client_name: str = typer.Option(
        socket.gethostname(),
        "--client-name",
        help="Human-readable name for this local machine.",
    ),
    persist_env: bool = typer.Option(
        True,
        "--persist-env/--no-persist-env",
        help="Persist local provisioning credentials to .env next to config.yaml.",
    ),
    path: Path | None = typer.Option(  # noqa: B008
        None,
        "--path",
        "-p",
        help="Override auto-detection and use this config file path for .env persistence.",
    ),
) -> None:
    """Pair this local MindRoom install with the hosted provisioning service."""
    normalized_pair_code = pair_code.strip().upper()
    if not cli_connect.is_valid_pair_code(normalized_pair_code):
        console.print("[red]Error:[/red] Invalid pair code format. Expected ABCD-EFGH.")
        raise typer.Exit(1)

    resolved_provisioning_url = (
        provisioning_url or os.getenv("MINDROOM_PROVISIONING_URL", "https://mindroom.chat")
    ).strip()
    if not resolved_provisioning_url:
        console.print("[red]Error:[/red] Invalid provisioning URL.")
        raise typer.Exit(1)

    resolved_config_path = (path or constants.runtime_config_path()).expanduser().resolve()
    normalized_client_name = client_name.strip() or socket.gethostname()
    try:
        credentials = cli_connect.complete_local_pairing(
            provisioning_url=resolved_provisioning_url,
            pair_code=normalized_pair_code,
            client_name=normalized_client_name,
            client_fingerprint=_local_client_fingerprint(config_path=resolved_config_path),
            matrix_ssl_verify=MATRIX_SSL_VERIFY,
            post_request=httpx.post,
        )
    except (TypeError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None

    if credentials.owner_user_id_invalid:
        console.print(
            "[yellow]Warning:[/yellow] Pairing response included malformed owner_user_id; skipping config owner autofill.",
        )
    if credentials.namespace_invalid:
        console.print(
            "[yellow]Warning:[/yellow] Pairing response included malformed namespace; derived a fallback namespace.",
        )

    if persist_env:
        env_path = cli_connect.persist_local_provisioning_env(
            provisioning_url=resolved_provisioning_url,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            namespace=credentials.namespace,
            config_path=resolved_config_path,
        )
        console.print("[green]Paired successfully.[/green]")
        console.print(f"  Saved credentials to: {env_path}")
        if credentials.owner_user_id and cli_connect.replace_owner_placeholders_in_config(
            config_path=resolved_config_path,
            owner_user_id=credentials.owner_user_id,
        ):
            console.print(f"  Updated owner placeholder(s) in: {resolved_config_path}")
        console.print("\nNext step:")
        console.print("  uv run mindroom run")
        return

    _print_pairing_success_with_exports(
        provisioning_url=resolved_provisioning_url,
        client_id=credentials.client_id,
        client_secret=credentials.client_secret,
        namespace=credentials.namespace,
        owner_user_id=credentials.owner_user_id,
    )


app.command("local-stack-setup")(local_stack_setup)


def _print_pairing_success_with_exports(
    *,
    provisioning_url: str,
    client_id: str,
    client_secret: str,
    namespace: str,
    owner_user_id: str | None,
) -> None:
    """Print non-persisted exports for local provisioning credentials."""
    console.print("[green]Paired successfully.[/green]")
    console.print("\nExport these variables before running MindRoom:")
    console.print(f"  export MINDROOM_PROVISIONING_URL={provisioning_url}")
    console.print(f"  export MINDROOM_LOCAL_CLIENT_ID={client_id}")
    console.print(f"  export MINDROOM_LOCAL_CLIENT_SECRET={client_secret}")
    console.print(f"  export MINDROOM_NAMESPACE={namespace}")
    if owner_user_id:
        console.print(
            f"\nOwner user ID from pairing: {owner_user_id} (not persisted in --no-persist-env mode).",
        )
        console.print(
            "Update your config.yaml owner placeholder(s) manually if you rely on authorization defaults.",
        )
    console.print("\nThen run:")
    console.print("  uv run mindroom run")


def _local_client_fingerprint(*, config_path: Path | None = None) -> str:
    """Return a stable, non-secret local fingerprint."""
    resolved_config_path = (config_path or constants.runtime_config_path()).expanduser().resolve()
    raw = f"{socket.gethostname()}:{resolved_config_path}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# Friendly error output helpers
# ---------------------------------------------------------------------------


def _print_missing_config_error() -> None:
    console.print("[red]Error:[/red] No config.yaml found.\n")
    console.print("MindRoom needs a configuration file to know which agents to run.\n")
    console.print("Quick start:")
    console.print("  [cyan]mindroom config init[/cyan]    Create a starter config")
    console.print("  [cyan]mindroom config edit[/cyan]    Edit your config\n")
    console.print("Config search locations (first match wins):")
    for i, loc in enumerate(config_search_locations(), 1):
        status = "[green]exists[/green]" if loc.exists() else "[dim]not found[/dim]"
        console.print(f"  {i}. {loc} ({status})")
    console.print("\nLearn more: https://github.com/mindroom-ai/mindroom")


def _print_connection_error(exc: BaseException) -> None:
    console.print("[red]Error:[/red] Could not connect to the Matrix homeserver.\n")
    console.print(f"  Details: {exc}\n")
    console.print("Check that:")
    console.print("  1. Your Matrix homeserver is running")
    console.print(
        f"  2. MATRIX_HOMESERVER is set correctly (current: {constants.runtime_matrix_homeserver()})",
    )
    console.print("  3. The server is reachable from this machine")


def main() -> None:
    """Main entry point that shows help by default."""
    # Print banner for top-level help (no subcommand given)
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help")):
        console.print(
            make_banner(tagline=("💊 What if I told you... ", "AI agents live in Matrix.")),
        )

    app()


if __name__ == "__main__":
    main()
