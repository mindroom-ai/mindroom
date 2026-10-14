"""Mindroom CLI - Simplified multi-agent Matrix bot system."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from contextlib import nullcontext
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING

import typer
from rich.markup import escape

from mindroom.constants import ensure_writable_config_path

from .api import is_loopback_host
from .banner import make_banner
from .config import (
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    activate_cli_runtime,
    check_env_keys,
    config_app,
    console,
    create_first_run_config,
    ensure_worker_dashboard_api_key,
    format_validation_errors,
    load_config_quiet,
    print_config_search_locations,
    warn_dashboard_without_key,
)
from .config_bundle import (
    config_apply_bundle,
    config_classify_change,
    config_install_bundle,
    initialize_runtime_bundle,
)
from .config_reload import config_check_applied, config_fingerprint
from .debug_report import debug_report
from .desktop import desktop_app
from .help_format import PanelOrderedGroup, apply_help_theme, examples
from .interactive import confirm, section, terminal_is_interactive
from .local_stack import local_stack_setup
from .migrate import config_migrate
from .plugins import plugins_app
from .response_activity import check_active_responses
from .service import require_login_service, service_app, start_login_service
from .trigger import trigger_app

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from types import FrameType

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.thread_export import ThreadExportStats

_HELP = """\
AI agents that live in Matrix and work everywhere via bridges.

[bold]Quick start:[/bold]
  [cyan]mindroom run[/cyan]           Set up on first run, pair, and start
  [cyan]mindroom config init[/cyan]   Create a starter config without starting\
"""
_CONFIG_INIT_PROVIDER_CHOICES = (
    "{openrouter,ollama,openai,azure,bedrock_claude,codex,kimi,claude,llama.cpp,vertexai_claude}"
)
# Exit code of `mindroom connect` when it declines to re-pair a connected machine (no terminal, no --force).
# The macOS app matches it as `MindRoomCommand.alreadyConnectedExitCode`; change both together.
_CONNECT_ALREADY_CONNECTED_EXIT_CODE = 3
# Matches `MindRoomCommand.pairingCancelledExitCode` in the macOS app.
_CONNECT_CANCELLED_EXIT_CODE = 130

app = typer.Typer(
    cls=PanelOrderedGroup,
    help=_HELP,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    no_args_is_help=True,
    pretty_exceptions_enable=True,
    # Disable showing locals which can be very large (also see `setup_logging`)
    pretty_exceptions_show_locals=False,
)
avatars_app = typer.Typer(help="Generate and sync agent, room, and space avatars.")
threads_app = typer.Typer(help="Export Matrix threads to searchable files.")
journal_app = typer.Typer(help="Inspect and rebind the durable event journal.")
config_app.command("migrate", rich_help_panel="Create and edit")(config_migrate)
config_app.command("fingerprint", rich_help_panel="Deployment")(config_fingerprint)
config_app.command("install-bundle", rich_help_panel="Deployment")(config_install_bundle)
config_app.command("classify-change", rich_help_panel="Deployment")(config_classify_change)
config_app.command("apply-bundle", rich_help_panel="Deployment")(config_apply_bundle)
config_app.command("check-applied", rich_help_panel="Deployment")(config_check_applied)
app.add_typer(config_app, name="config", rich_help_panel="Configure")
app.add_typer(plugins_app, name="plugins", rich_help_panel="Configure")
app.add_typer(avatars_app, name="avatars", rich_help_panel="Configure")
app.add_typer(service_app, name="service", rich_help_panel="Keep running")
app.add_typer(desktop_app, name="desktop", rich_help_panel="Desktop")
app.add_typer(threads_app, name="threads", rich_help_panel="Troubleshoot")
app.add_typer(journal_app, name="journal", rich_help_panel="Troubleshoot")
app.add_typer(trigger_app, name="trigger", rich_help_panel="Automation")
app.command(rich_help_panel="Automation")(check_active_responses)
app.command("debug-report", rich_help_panel="Troubleshoot")(debug_report)


@app.command(rich_help_panel="Troubleshoot")
def version() -> None:
    """Show the MindRoom version."""
    from mindroom import __version__  # noqa: PLC0415

    console.print(f"MindRoom version: [bold]{__version__}[/bold]")
    console.print("AI agents that live in Matrix")


@app.command(
    rich_help_panel="Get started",
    epilog=examples(
        ("mindroom run", "Set up on first run, pair, and start"),
        ("mindroom run --provider anthropic", "Skip the provider question on first run"),
        ("mindroom run --service", "Run in the background at login"),
        ("mindroom run --no-api", "Start without the dashboard"),
    ),
)
def run(
    provider: str | None = typer.Option(
        None,
        "--provider",
        help=(
            "Model provider preset for the starter config when none exists, with the choices of "
            "`config init --provider`. It also lets setup run without a terminal, taking the API key "
            "from the environment."
        ),
    ),
    service: bool | None = typer.Option(
        None,
        "--service/--no-service",
        help=(
            "After setup and pairing, install and start MindRoom as a login service (systemd or launchd) "
            "instead of running it here, or never offer to. A first run in a terminal asks."
        ),
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level",
        "-l",
        help="Set the logging level (DEBUG, INFO, WARNING, ERROR)",
        case_sensitive=False,
        envvar="LOG_LEVEL",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Use this config file path. Defaults the storage location to the selected config directory unless --storage-path is set.",
        rich_help_panel="Paths",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data (state, sessions, tracking)",
        rich_help_panel="Paths",
    ),
    api: bool = typer.Option(
        True,
        "--api/--no-api",
        help="Start the bundled dashboard/API server alongside the bot",
        rich_help_panel="Dashboard",
    ),
    api_port: int = typer.Option(
        DEFAULT_API_PORT,
        "--api-port",
        help="Port for the bundled dashboard/API server",
        rich_help_panel="Dashboard",
    ),
    api_host: str = typer.Option(
        DEFAULT_API_HOST,
        "--api-host",
        help="Host for the bundled dashboard/API server",
        rich_help_panel="Dashboard",
    ),
    bootstrap_config_bundle: Path | None = typer.Option(  # noqa: B008
        None,
        help="Initialize the selected config directory from this bundle only when the directory is absent.",
        rich_help_panel="Deployment",
    ),
    bootstrap_config_bundle_revision: str | None = typer.Option(
        None,
        help="Install a changed bootstrap revision through native validation; preserve a matching active revision.",
        rich_help_panel="Deployment",
    ),
) -> None:
    """Set up on first run, pair, and start MindRoom.

    On first run in a terminal, or with --provider, MindRoom creates a starter config for MindRoom Chat, pairs this machine with your MindRoom Chat account, and offers to keep running in the background at login.
    It then creates the agent accounts and rooms from config.yaml, keeps room memberships in sync, and starts the dashboard and API (disable with --no-api).
    """
    if bootstrap_config_bundle_revision is not None and bootstrap_config_bundle is None:
        typer.echo("--bootstrap-config-bundle-revision requires --bootstrap-config-bundle.", err=True)
        raise typer.Exit(2)
    # The service runs a plain `mindroom run`, so it only takes over runs with the default paths and API options.
    plain_run = (
        config_path is None
        and storage_path is None
        and api
        and api_host == DEFAULT_API_HOST
        and api_port == DEFAULT_API_PORT
    )
    if service and not plain_run:
        typer.echo(
            "--service runs a plain `mindroom run`, so it cannot be combined with "
            "--config, --storage-path, --no-api, --api-host, or --api-port.",
            err=True,
        )
        raise typer.Exit(2)
    # Fail before setup and pairing ask a person for anything when this machine cannot run the service.
    service_manager = require_login_service() if service else None
    console.print(make_banner())
    if bootstrap_config_bundle is not None:
        initialize_runtime_bundle(bootstrap_config_bundle, config_path, storage_path, bootstrap_config_bundle_revision)

    from mindroom.matrix.provisioning_env import local_pairing_required  # noqa: PLC0415

    runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    interactive = terminal_is_interactive()
    first_run = not ensure_writable_config_path(runtime_paths=runtime_paths) and (interactive or provider is not None)
    if first_run:
        try:
            create_first_run_config(runtime_paths, provider=provider, interactive=interactive)
        except (OSError, ValueError) as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from None
        # Pick up the new config and .env before pairing and startup.
        runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    runtime_paths = _protect_dashboard_from_workers(runtime_paths, config_path, storage_path)
    # Report a broken config or missing model keys before any pairing waits for a human.
    config = _load_active_config_or_exit(runtime_paths)
    if not first_run:
        # First-run setup has already said which provider credentials are still missing.
        check_env_keys(config, runtime_paths=runtime_paths)
    try:
        if local_pairing_required(runtime_paths):
            import mindroom.cli.connect as cli_connect  # noqa: PLC0415
            from mindroom.cli.pairing_probes import serve_pairing_probes  # noqa: PLC0415

            # Container health probes must see a live process while the run waits for a human to approve it.
            with serve_pairing_probes(api_host, api_port) if api else nullcontext():
                cli_connect.pair_local_install(
                    runtime_paths,
                    console=console,
                    # `mindroom connect` or the macOS app may pair this machine while the run waits.
                    stop_waiting=lambda: _paired_elsewhere(config_path, storage_path),
                    confirm_approver=_approver_confirmation(),
                )
    except (OSError, TypeError, ValueError) as exc:
        # Pairing errors can carry text the provisioning service chose, such as an approver or error detail.
        console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from None
    ask_service = service is None and first_run and interactive and plain_run
    if (service or ask_service) and start_login_service(runtime_paths, service_manager):
        console.print(f"Dashboard: http://localhost:{api_port}")
        console.print(f"After editing {runtime_paths.env_path}, run [cyan]mindroom service restart[/cyan].")
        # The service runs MindRoom now; starting it here as well would run it twice.
        return

    asyncio.run(
        _run(
            log_level=log_level.upper(),
            config_path=config_path,
            storage_path=storage_path,
            api=api,
            api_port=api_port,
            api_host=api_host,
        ),
    )


def _protect_dashboard_from_workers(
    runtime_paths: RuntimePaths,
    config_path: Path | None,
    storage_path: Path | None,
) -> RuntimePaths:
    """Give the dashboard API a generated key when dedicated workers could reach it, then reload that runtime."""
    from mindroom.api.auth import dashboard_requires_credential  # noqa: PLC0415  # lazy: FastAPI import

    if ensure_worker_dashboard_api_key(
        runtime_paths,
        dashboard_has_credential=dashboard_requires_credential(runtime_paths),
    ):
        return activate_cli_runtime(path=config_path, storage_path=storage_path)
    return runtime_paths


def _load_active_config_or_exit(runtime_paths: RuntimePaths) -> Config:
    """Load the active config file or exit with friendly validation errors."""
    from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES  # noqa: PLC0415

    ensure_writable_config_path(runtime_paths=runtime_paths)

    config_path = runtime_paths.config_path
    if not config_path.exists():
        _print_missing_config_error(runtime_paths.process_env)
        raise typer.Exit(1)

    try:
        config = load_config_quiet(
            runtime_paths=runtime_paths,
            tolerate_plugin_load_errors=True,
        )
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        format_validation_errors(exc, config_path)
        raise typer.Exit(1) from None

    return config


async def _run(
    log_level: str,
    config_path: Path | None,
    storage_path: Path | None,
    *,
    api: bool,
    api_port: int,
    api_host: str,
) -> None:
    """Run the multi-agent system with friendly error handling."""
    from mindroom.startup_errors import EventJournalHoldLostError, PermanentStartupError  # noqa: PLC0415

    runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    # Validate again: pairing may have rewritten owner placeholders in the config.
    _load_active_config_or_exit(runtime_paths)

    console.print(section(f"Starting MindRoom (log level: {log_level})"))
    if api:
        from mindroom.frontend_assets import ensure_frontend_dist_dir  # noqa: PLC0415

        frontend_dir = ensure_frontend_dist_dir(runtime_paths)
        display_host = "localhost" if api_host == DEFAULT_API_HOST else api_host
        if frontend_dir is None:
            console.print("Dashboard: unavailable (frontend assets missing)")
            console.print("  Install Bun or provide MINDROOM_FRONTEND_DIST when running from a source checkout.")
        else:
            console.print(f"Dashboard: http://{display_host}:{api_port}")
        console.print(f"API: http://{display_host}:{api_port}/api")
        _warn_if_dashboard_is_open_beyond_loopback(runtime_paths, api_host, api_port)
    console.print("Press Ctrl+C to stop\n")

    try:
        from mindroom.orchestrator import main as bot_main  # noqa: PLC0415  # lazy: heavy import

        await bot_main(
            log_level=log_level,
            runtime_paths=runtime_paths,
            api=api,
            api_port=api_port,
            api_host=api_host,
        )
    except KeyboardInterrupt:
        console.print("\nStopped")
    except ConnectionError as exc:
        _print_connection_error(exc, runtime_paths)
        raise typer.Exit(1) from None
    except (PermanentStartupError, EventJournalHoldLostError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None
    except OSError as exc:
        if "connect" in str(exc).lower() or "refused" in str(exc).lower():
            _print_connection_error(exc, runtime_paths)
            raise typer.Exit(1) from None
        raise


def _warn_if_dashboard_is_open_beyond_loopback(runtime_paths: RuntimePaths, api_host: str, api_port: int) -> None:
    """Warn when anyone who can reach a non-loopback bind address would administer MindRoom."""
    from mindroom.api.auth import dashboard_requires_credential  # noqa: PLC0415  # lazy: FastAPI import

    if is_loopback_host(api_host) or dashboard_requires_credential(runtime_paths):
        return
    address_host = f"[{api_host}]" if ":" in api_host else api_host
    warn_dashboard_without_key(
        f"{address_host}:{api_port}",
        f"Set MINDROOM_API_KEY in {runtime_paths.env_path}, or pass --api-host 127.0.0.1.",
    )


@avatars_app.command("generate")
def avatars_generate(
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing managed workspace avatar files.",
    ),
) -> None:
    """Generate missing managed avatar files in the workspace."""
    from mindroom.error_handling import AvatarGenerationError  # noqa: PLC0415

    runtime_paths = activate_cli_runtime()
    _load_active_config_or_exit(runtime_paths)

    try:
        from mindroom.avatar_generation import run_avatar_generation  # noqa: PLC0415

        asyncio.run(run_avatar_generation(runtime_paths, force=force))
    except AvatarGenerationError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None


@avatars_app.command("sync")
def avatars_sync(
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace existing Matrix room and root-space avatars.",
    ),
) -> None:
    """Sync room and root-space avatars to Matrix.

    Uses the router account that `mindroom run` initialized.
    """
    from mindroom.error_handling import AvatarSyncError  # noqa: PLC0415

    runtime_paths = activate_cli_runtime()
    _load_active_config_or_exit(runtime_paths)

    try:
        from mindroom.avatar_generation import set_room_avatars_in_matrix  # noqa: PLC0415

        asyncio.run(set_room_avatars_in_matrix(runtime_paths, force=force))
    except AvatarSyncError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1) from None
    except ConnectionError as exc:
        _print_connection_error(exc, runtime_paths)
        raise typer.Exit(1) from None
    except OSError as exc:
        if "connect" in str(exc).lower() or "refused" in str(exc).lower():
            _print_connection_error(exc, runtime_paths)
            raise typer.Exit(1) from None
        raise


@threads_app.command(
    "export",
    epilog=examples(
        ("mindroom threads export", "Export every room once"),
        ("mindroom threads export --room lobby", "Only rooms matching lobby"),
        ("mindroom threads export --watch --interval 600", "Export again every 10 minutes"),
    ),
)
def _threads_export_command(
    url: str | None = typer.Option(
        None,
        "--url",
        help="Running MindRoom URL; defaults to MINDROOM_URL or localhost:8765.",
    ),
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Use this config file path.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data.",
    ),
    output: Path | None = typer.Option(  # noqa: B008
        None,
        "--output",
        "-o",
        help="Output directory. Defaults to <storage>/thread_exports.",
    ),
    room: str | None = typer.Option(
        None,
        "--room",
        "-r",
        help="Filter exported rooms by a substring of the room key, alias, name, or Matrix room ID.",
        rich_help_panel="Filter",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        help="Repeat the export forever on a fixed interval.",
        rich_help_panel="Watch",
    ),
    interval: int = typer.Option(
        300,
        "--interval",
        help="Watch interval in seconds.",
        rich_help_panel="Watch",
    ),
    max_thread_roots: int = typer.Option(
        2000,
        "--max-thread-roots",
        help="Maximum thread roots to enumerate per room.",
        rich_help_panel="Filter",
    ),
    invited_rooms: bool = typer.Option(
        True,
        "--invited-rooms/--no-invited-rooms",
        help="Include rooms joined through authorized invites (user-created rooms).",
        rich_help_panel="Filter",
    ),
) -> None:
    """Export Matrix threads from a running MindRoom to searchable YAML files."""
    asyncio.run(
        _threads_export(
            config_path=config_path,
            url=url,
            storage_path=storage_path,
            output=output,
            room=room,
            watch=watch,
            interval=interval,
            max_thread_roots=max_thread_roots,
            include_invited_rooms=invited_rooms,
        ),
    )


async def _journal_adopt(
    *,
    config_path: Path | None,
    storage_path: Path | None,
    yes: bool,
    force: bool,
) -> None:
    """Bind this install to whatever event-journal database is configured right now."""
    from mindroom.event_journal.opening import (  # noqa: PLC0415 - keeps the journal store out of CLI import time
        EventJournalBindingError,
        adopt_event_journal,
        current_binding_description,
        describe_event_journal,
    )

    runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    config = _load_active_config_or_exit(runtime_paths)
    description = describe_event_journal(config.event_journal, runtime_paths)
    # Read for the prompt only. What is actually replaced is decided again
    # under the binding lock, where nothing else can be publishing.
    previous = current_binding_description(runtime_paths.storage_root)
    if previous is not None and not yes:
        console.print(f"This install is bound to [bold]{previous}[/bold].")
        console.print(
            f"Adopting [bold]{description}[/bold] gives up the deduplication, delivery, and recovery "
            "history held in the bound journal.",
        )
        console.print("Stop MindRoom first: a runtime that is still up keeps writing to the journal it started on.")
        if not typer.confirm("Adopt the configured event journal?"):
            raise typer.Exit(1)

    try:
        generation = await adopt_event_journal(
            config.event_journal,
            runtime_paths=runtime_paths,
            storage_path=runtime_paths.storage_root,
            force=force,
        )
    except EventJournalBindingError as exc:
        # The previous binding is still in place, which is the point: a failed
        # adoption must leave the install exactly as usable as it was.
        console.print(f"[red]Not adopted:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]Bound[/green] this install to {description} (generation {generation}).")


@journal_app.command("adopt")
def _journal_adopt_command(
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Use this config file path.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Adopt without confirming, even when another journal is already bound.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Adopt even though another process still has this install's journal open.",
    ),
) -> None:
    """Bind this install to the configured event-journal database.

    MindRoom refuses to start against a journal it is not bound to, because
    using a different one loses turn deduplication, delivery ownership, and
    recovery ownership without any error. This is how you say the change was
    deliberate.
    """
    asyncio.run(_journal_adopt(config_path=config_path, storage_path=storage_path, yes=yes, force=force))


def _print_thread_export_stats(stats: ThreadExportStats) -> None:
    """Print one export-pass summary."""
    unchanged_note = f" ({stats.threads_unchanged} unchanged)" if stats.threads_unchanged else ""
    console.print(
        f"Exported {stats.threads_exported}/{stats.threads_seen} threads "
        f"from {stats.rooms_exported} room(s) to {stats.output_dir}{unchanged_note}",
    )
    if stats.truncated_rooms:
        console.print(f"[yellow]Warning:[/yellow] {stats.truncated_rooms} room(s) hit the thread enumeration limit")
    for failure in stats.failed_items:
        if failure.room_key is None:
            console.print(f"[red]Failed target:[/red] {failure.error}")
            continue
        target = failure.thread_id or failure.room_id
        console.print(f"[red]Failed:[/red] {failure.room_key} {target}: {failure.error}")


def _handle_thread_export_error(exc: RuntimeError | OSError, runtime_paths: RuntimePaths, *, watch: bool) -> None:
    """Print one top-level export error and exit unless watch mode can retry."""
    if isinstance(exc, ConnectionError) or _is_connection_os_error(exc):
        _print_connection_error(exc, runtime_paths)
    else:
        console.print(f"[red]Error:[/red] {exc}")
    if not watch:
        raise typer.Exit(1) from None


def _is_connection_os_error(exc: BaseException) -> bool:
    """Return whether an OS error looks like a Matrix connection failure."""
    return isinstance(exc, OSError) and ("connect" in str(exc).lower() or "refused" in str(exc).lower())


async def _threads_export(
    *,
    config_path: Path | None,
    url: str | None = None,
    storage_path: Path | None,
    output: Path | None,
    room: str | None,
    watch: bool,
    interval: int,
    max_thread_roots: int,
    include_invited_rooms: bool,
) -> None:
    """Run one thread export command."""
    from mindroom.cli.thread_export import request_thread_export  # noqa: PLC0415

    runtime_paths = activate_cli_runtime(path=config_path, storage_path=storage_path)
    if interval < 1:
        console.print("[red]Error:[/red] --interval must be at least 1 second")
        raise typer.Exit(1)
    if max_thread_roots < 1:
        console.print("[red]Error:[/red] --max-thread-roots must be at least 1")
        raise typer.Exit(1)

    while True:
        try:
            stats = await request_thread_export(
                url=url,
                runtime_paths=runtime_paths,
                output_dir=output,
                room_filter=room,
                max_thread_roots=max_thread_roots,
                include_invited_rooms=include_invited_rooms,
            )
        except ValueError as exc:
            # A refused URL stays refused, so watching would only repeat the error.
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from None
        except (OSError, RuntimeError) as exc:
            _handle_thread_export_error(exc, runtime_paths, watch=watch)
        else:
            _print_thread_export_stats(stats)
            if not watch:
                if stats.failures:
                    raise typer.Exit(1)
                return

        await asyncio.sleep(interval)


@app.command(
    rich_help_panel="Get started",
    epilog=examples(
        ("mindroom connect", "Pair this machine and save its credentials"),
        ("mindroom connect --open-browser", "Also open the approval link in the browser"),
        ("mindroom connect --force", "Pair again, replacing the saved connection"),
    ),
)
def connect(
    provisioning_url: str | None = typer.Option(
        None,
        "--provisioning-url",
        help="Base URL for the MindRoom provisioning API.",
        rich_help_panel="Advanced",
    ),
    client_name: str = typer.Option(
        socket.gethostname(),
        "--client-name",
        help="Human-readable name for this local machine.",
        rich_help_panel="Advanced",
    ),
    persist_env: bool = typer.Option(
        True,
        "--persist-env/--no-persist-env",
        help="Persist local provisioning credentials to .env next to config.yaml.",
        rich_help_panel="Advanced",
    ),
    open_browser: bool = typer.Option(
        False,
        "--open-browser",
        help="Open the approval link in the default browser.",
    ),
    path: Path | None = typer.Option(  # noqa: B008
        None,
        "--path",
        "-p",
        help="Override auto-detection and use this config file path for .env persistence.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Pair again even when this machine is already connected.",
    ),
    graceful_cancel: bool = typer.Option(
        False,
        "--graceful-cancel",
        help="Cancel on SIGTERM while waiting; finish an in-flight approval or save (used by the macOS app).",
        rich_help_panel="Advanced",
    ),
) -> None:
    """Pair this machine with your MindRoom Chat account.

    MindRoom prints an approval link and QR code; approve it in MindRoom Chat.

    When this machine is already connected, a terminal asks before pairing again.
    Without a terminal it exits with code 3 unless --force is given.
    With --graceful-cancel, SIGTERM while waiting exits with code 130 without saving.
    """
    import mindroom.cli.connect as cli_connect  # noqa: PLC0415

    try:
        runtime_paths = activate_cli_runtime(path)
        if provisioning_url is None and (refusal := cli_connect.self_hosted_pairing_error(runtime_paths)):
            console.print(f"[red]Error:[/red] {escape(refusal)}")
            raise typer.Exit(1)
        if runtime_paths.env_value("MINDROOM_LOCAL_CLIENT_ID") and runtime_paths.env_value(
            "MINDROOM_LOCAL_CLIENT_SECRET",
        ):
            console.print(
                "[yellow]Warning:[/yellow] This machine is already connected. "
                "Pairing again creates a new connection and a new agent namespace: "
                "existing agents keep working, and new agents get the new namespace.",
            )
            if not force:
                _confirm_reconnect()
        cancelled = False

        def request_cancel(_signum: int, _frame: FrameType | None) -> None:
            nonlocal cancelled
            cancelled = True

        def stop_waiting() -> bool:
            if cancelled:
                console.print("Connection cancelled. Nothing was saved.")
                raise typer.Exit(_CONNECT_CANCELLED_EXIT_CODE)
            return False

        if graceful_cancel:
            signal.signal(signal.SIGTERM, request_cancel)
        try:
            cli_connect.pair_local_install(
                runtime_paths,
                console=console,
                provisioning_url=provisioning_url,
                client_name=client_name,
                persist_env=persist_env,
                open_browser=open_browser,
                renew_expired=False,
                stop_waiting=stop_waiting if graceful_cancel else None,
                confirm_approver=_approver_confirmation(),
            )
            console.print("\nNext step:\n  mindroom run")
        finally:
            if graceful_cancel:
                # Preserve the result through shutdown; Python resets callable handlers during finalization.
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except (TypeError, ValueError) as exc:
        console.print(f"[red]Error:[/red] {escape(str(exc))}")
        raise typer.Exit(1) from None


@app.command(rich_help_panel="Get started")
def doctor(
    config_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--config",
        "-c",
        help="Use this config file path. Defaults the storage location to the selected config directory unless --storage-path is set.",
    ),
    storage_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--storage-path",
        "-s",
        help="Base directory for persistent MindRoom data (state, sessions, tracking)",
    ),
) -> None:
    """Check your setup for common problems.

    Runs connectivity, configuration, and credential checks in a single pass
    so you can fix everything before running `mindroom run`.
    """
    from .doctor import doctor as doctor_command  # noqa: PLC0415

    doctor_command(config_path=config_path, storage_path=storage_path)


def _confirm_reconnect() -> None:
    """Confirm re-pairing in a terminal, or let the macOS app ask via exit code 3."""
    if not _stdin_is_interactive():
        console.print("Run `mindroom connect --force` to pair again.")
        raise typer.Exit(_CONNECT_ALREADY_CONNECTED_EXIT_CODE)
    typer.confirm("Pair again?", abort=True)


def _stdin_is_interactive() -> bool:
    """Whether a person can answer prompts; the macOS app and services run without a terminal."""
    return sys.stdin.isatty()


def _paired_elsewhere(config_path: Path | None, storage_path: Path | None) -> bool:
    """Whether another process made pairing unnecessary since this run resolved its runtime."""
    from mindroom.matrix.provisioning_env import local_pairing_required  # noqa: PLC0415

    try:
        return not local_pairing_required(activate_cli_runtime(path=config_path, storage_path=storage_path))
    except ValueError:
        # A half-written or undecodable .env keeps the run waiting because its writer may not have finished.
        return False


def _approver_confirmation() -> Callable[[], bool] | None:
    """Ask whether the approving account is the user's own, only when a person can answer."""
    if not _stdin_is_interactive():
        return None

    def confirm_account() -> bool:
        try:
            return confirm("Is this your account?", default=True)
        except typer.Abort:
            # Ctrl+C or EOF is not a yes: discard the credentials with the revoke hint instead of a bare abort.
            console.print()
            return False

    return confirm_account


app.command("local-stack-setup", rich_help_panel="Development")(local_stack_setup)


# ---------------------------------------------------------------------------
# Friendly error output helpers
# ---------------------------------------------------------------------------


def _print_missing_config_error(process_env: Mapping[str, str]) -> None:
    console.print("[red]Error:[/red] No config found.\n")
    console.print("MindRoom needs a configuration file to know which agents to run.\n")
    console.print("Quick start:")
    console.print("  [cyan]mindroom config init[/cyan]    Create a hosted starter config")
    console.print(
        f"  [cyan]mindroom config init --provider {_CONFIG_INIT_PROVIDER_CHOICES}[/cyan]    Choose a model provider",
        soft_wrap=True,
    )
    console.print(
        "  [cyan]mindroom run[/cyan]            In an interactive terminal: create a hosted starter config, pair, and start",
        soft_wrap=True,
    )
    console.print(
        f"  [cyan]mindroom run --provider {_CONFIG_INIT_PROVIDER_CHOICES} [--service][/cyan]    "
        "Without a terminal: the same, with the API key from the environment\n",
        soft_wrap=True,
    )
    print_config_search_locations(process_env, title="Config search locations (first match wins):")
    console.print("\nLearn more: https://github.com/mindroom-ai/mindroom")


def _print_connection_error(exc: BaseException, runtime_paths: RuntimePaths) -> None:
    from mindroom import constants  # noqa: PLC0415

    console.print("[red]Error:[/red] Could not connect to the Matrix homeserver.\n")
    console.print(f"  Details: {exc}\n")
    console.print("Check that:")
    console.print("  1. Your Matrix homeserver is running")
    console.print(
        "  2. MATRIX_HOMESERVER is set correctly "
        f"(current: {constants.runtime_matrix_homeserver(runtime_paths=runtime_paths)})",
    )
    console.print("  3. The server is reachable from this machine")


def main() -> None:
    """Main entry point that shows help by default."""
    # Shell completion runs this program without arguments and evaluates what it prints.
    if "_MINDROOM_COMPLETE" not in os.environ:
        _prepare_help_output(sys.argv[1:])
    app()


def _prepare_help_output(args: list[str]) -> None:
    """Load the help theme when `args` can show help, and print the banner above the top-level help."""
    # Help appears for -h/--help and for a bare command or group such as `mindroom config`; deeper calls
    # such as `mindroom service status`, which the macOS app polls, skip loading Typer's help renderer.
    if len(args) <= 1 or {"-h", "--help"} & set(args):
        apply_help_theme()
    if not args or args in (["-h"], ["--help"]):
        console.print(make_banner(tagline=("💊 What if I told you... ", "AI agents live in Matrix.")))


if __name__ == "__main__":
    main()
