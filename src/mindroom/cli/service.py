"""CLI commands for installing MindRoom as a user service."""

from __future__ import annotations

import platform
import signal
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from dotenv import dotenv_values
from rich.console import Console
from rich.panel import Panel

from mindroom.constants import PROVIDER_ENV_KEYS, resolve_primary_runtime_paths
from mindroom.runtime_env_policy import is_unset_env_value

from .config import DEFAULT_API_HOST, DEFAULT_API_PORT, warn_dashboard_without_key, worker_dashboard_api_key_needed
from .env_file import upsert_env_values

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.services.config import InstallResult, ServiceActionResult, ServiceManager

_console = Console()
_err_console = Console(stderr=True)

service_app = typer.Typer(
    name="service",
    help="""Install and manage MindRoom as a background user service.

MindRoom runs the version installed by this command through `uv tool run` and starts automatically at login.
That version is also installed as a uv tool, so the service runs from a persistent environment instead of uv's cache.
Rerun `mindroom service install` after upgrading MindRoom.

Supported platforms:
- macOS: launchd (`~/Library/LaunchAgents/`)
- Linux: systemd user services (`~/.config/systemd/user/`)
""",
    rich_markup_mode="markdown",
    no_args_is_help=True,
)


def _get_service_manager() -> ServiceManager:
    """Load the platform service manager only when service commands run."""
    from mindroom.services.manager import get_service_manager as load_service_manager  # noqa: PLC0415

    return load_service_manager()


def _manager_or_exit() -> ServiceManager:
    try:
        return _get_service_manager()
    except RuntimeError as exc:
        _err_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from None


def _confirm_action(message: str) -> bool:
    """Ask for confirmation and return whether the user accepted."""
    try:
        answer = _console.input(f"[bold]{message} [Y/n]: [/bold]").strip().lower()
    except (KeyboardInterrupt, EOFError):
        _console.print("\n[dim]Cancelled.[/dim]")
        raise typer.Exit(0) from None
    return answer in {"", "y", "yes"}


def _ensure_uv_installed(manager: ServiceManager, *, no_confirm: bool) -> bool:
    """Ensure uv is installed before service installation and return whether it is."""
    uv_installed, uv_path = manager.check_uv_installed()
    if uv_installed:
        _console.print(f"  [green]uv installed:[/green] {uv_path}")
        return True

    _console.print("[yellow]uv is required to run the MindRoom service.[/yellow]")
    if not no_confirm and not _confirm_action("Install uv now?"):
        _console.print(
            "[yellow]Install uv from https://docs.astral.sh/uv/, then run `mindroom service install`.[/yellow]",
        )
        return False

    _console.print("Installing uv...")
    success, message = manager.install_uv()
    if not success:
        _err_console.print(f"[bold red]Error:[/bold red] {message}")
        return False
    _console.print(f"  [green]{message}[/green]")
    return True


def _print_service_action_result(result: ServiceActionResult) -> None:
    """Print a service lifecycle result and exit non-zero on failure."""
    if not result.success:
        _err_console.print(f"[bold red]Error:[/bold red] {result.message}")
        raise typer.Exit(1)
    _console.print(f"[green]{result.message}[/green]")


@service_app.command("install")
def install_service(
    skip_deps: bool = typer.Option(False, "--skip-deps", help="Skip uv dependency check."),
    no_confirm: bool = typer.Option(False, "--no-confirm", "-y", help="Skip confirmation prompts."),
) -> None:
    """Install and start MindRoom as a background user service."""
    manager = _manager_or_exit()

    if not skip_deps and not _ensure_uv_installed(manager, no_confirm=no_confirm):
        raise typer.Exit(1)

    if not no_confirm:
        _console.print()
        _console.print("[bold]Will install:[/bold] MindRoom user service")
        if not _confirm_action("Continue?"):
            _console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = _install_and_start_service(manager, resolve_primary_runtime_paths())
    except ValueError as exc:
        _err_console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(1) from None
    if not result.success:
        _err_console.print(f"[bold red]Error:[/bold red] {result.message}")
        raise typer.Exit(1)
    _print_installed_service(manager, result)


def require_login_service() -> ServiceManager:
    """Return the service manager for `mindroom run --service`, exiting before setup when this machine cannot run the service."""
    manager = _manager_or_exit()
    if not manager.is_available():
        _err_console.print(f"[bold red]Error:[/bold red] This machine cannot run a {manager.description}.")
        raise typer.Exit(1)
    return manager


def start_login_service(runtime_paths: RuntimePaths, manager: ServiceManager | None) -> bool:
    """Install and start MindRoom as a login service after `mindroom run` setup, and return whether the service now runs it.

    `manager` comes from `require_login_service` for `--service`; without it, the user is asked first.
    The service reads only `.env`, so the dashboard and provider keys exported in this shell are saved there first.
    When asking, declining or a failed installation leaves MindRoom to start in this terminal.
    With `--service`, a failure exits with an error, because nobody may be watching a terminal run.
    """
    requested = manager is not None
    if manager is None:
        manager = _ask_for_login_service()
        if manager is None:
            return False
    # Asking skips installed services, so only `--service` replaces one.
    replacing = requested and manager.get_service_status().installed
    if _ensure_uv_installed(manager, no_confirm=requested):
        try:
            result = _install_and_start_service(manager, runtime_paths)
        except ValueError as exc:
            # Keys that cannot be saved to `.env` stop the installation before anything is installed.
            _err_console.print(f"[bold red]Error:[/bold red] {exc}")
        else:
            if result.success:
                _print_installed_service(manager, result)
                return True
            _err_console.print(f"[bold red]Error:[/bold red] {result.message}")
            if not replacing:
                # A half-installed unit or plist would still start at the next login.
                manager.uninstall_service()
    if requested:
        raise typer.Exit(1)
    _console.print("Starting MindRoom in this terminal instead.")
    return False


def _install_and_start_service(manager: ServiceManager, runtime_paths: RuntimePaths) -> InstallResult:
    """Save the keys this shell exports to `.env`, the only environment the service reads, then install and start it.

    Raises `ValueError`, before installing anything, when the keys cannot be saved to `.env`.
    """
    # Without a config there is no service to install, and no `.env` to save keys for.
    if runtime_paths.config_path.exists() and (shell_keys := _shell_service_keys(runtime_paths)):
        upsert_env_values(runtime_paths.env_path, shell_keys)
        _console.print(f"Saved {', '.join(shell_keys)} from your shell to {runtime_paths.env_path} for the service.")
    _install_service_runtime(manager)
    return manager.install_service()


def _install_service_runtime(manager: ServiceManager) -> None:
    """Install the service's MindRoom version as a persistent uv tool, warning instead of failing when uv cannot."""
    _, uv_path = manager.check_uv_installed()
    if uv_path is not None and not manager.install_runtime(uv_path):
        _console.print(
            "[yellow]Warning:[/yellow] Could not install this MindRoom version as a uv tool (see uv's output above), "
            "so the service runs from uv's cache, where `uv cache clean` removes it and the extras MindRoom installs.",
        )


def _shell_service_keys(runtime_paths: RuntimePaths) -> dict[str, str]:
    """Return `MINDROOM_API_KEY` and provider keys, `NAME` or `NAME_FILE`, with a usable shell value `.env` does not hold.

    A dashboard key that protected terminal runs must also protect the service, which listens on every interface.
    """
    candidates = [("MINDROOM_API_KEY", "MINDROOM_API_KEY")] + [
        (env_key, name) for env_key in PROVIDER_ENV_KEYS.values() for name in (env_key, f"{env_key}_FILE")
    ]
    # `${NAME}` in `.env` expands from this shell's variables here but not in the service, so compare lines as written.
    written_env_values = dotenv_values(runtime_paths.env_path, interpolate=False)
    return {
        name: value
        for env_key, name in candidates
        if (value := runtime_paths.process_env.get(name))
        and not is_unset_env_value(env_key, value)
        and written_env_values.get(name) != value
    }


def _ask_for_login_service() -> ServiceManager | None:
    """Return the service manager when the user wants a login service; an unusable or existing service skips the question."""
    try:
        manager = _get_service_manager()
    except RuntimeError:
        return None
    # Installing would fail without systemd, or repoint a service that already runs another setup.
    if not manager.is_available() or manager.get_service_status().installed:
        return None
    _console.print()
    if not _confirm_action(f"Run MindRoom in the background and start it at login ({manager.description})?"):
        return None
    return manager


def _print_installed_service(manager: ServiceManager, result: InstallResult) -> None:
    """Print where to check on a service that was just installed and started, and whether its dashboard is open."""
    log_hint = f"View logs: [cyan]{manager.get_log_command()}[/cyan]"
    if result.log_dir is not None:
        log_hint = f"View logs: [cyan]{result.log_dir}/[/cyan]"
    _console.print(
        Panel(
            f"[green]{result.message}[/green]\n\n"
            "The service is pinned to this MindRoom version.\n"
            "After upgrading, rerun [cyan]mindroom service install[/cyan].\n\n"
            f"Check status: [cyan]mindroom service status[/cyan]\n{log_hint}",
            title="Service Installed",
            border_style="green",
        ),
    )
    _warn_if_service_dashboard_is_open(manager.get_service_environment())


def _warn_if_service_dashboard_is_open(service_environment: Mapping[str, str]) -> None:
    """Warn here, where someone reads it, when the service's dashboard API will listen on every interface without a key."""
    from mindroom.api.auth import dashboard_requires_credential  # noqa: PLC0415  # lazy: FastAPI import

    try:
        runtime_paths = _service_runtime_paths(service_environment)
        # With dedicated workers, the service's `mindroom run` generates a key when it starts.
        if (
            runtime_paths is None
            or worker_dashboard_api_key_needed(runtime_paths)
            or dashboard_requires_credential(runtime_paths)
        ):
            return
    except ValueError:
        # The service stops with its own error.
        return
    warn_dashboard_without_key(
        f"{DEFAULT_API_HOST}:{DEFAULT_API_PORT}",
        f"Set MINDROOM_API_KEY in {runtime_paths.env_path}, then run [cyan]mindroom service restart[/cyan].",
    )


@service_app.command("uninstall")
def uninstall_service(
    no_confirm: bool = typer.Option(False, "--no-confirm", "-y", help="Skip confirmation prompts."),
) -> None:
    """Stop and remove the MindRoom user service."""
    manager = _manager_or_exit()
    if not no_confirm:
        _console.print("[bold]Will uninstall:[/bold] MindRoom user service")
        if not _confirm_action("Continue?"):
            _console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    result = manager.uninstall_service()
    if not result.success:
        _err_console.print(f"[bold red]Error:[/bold red] {result.message}")
        raise typer.Exit(1)
    _console.print(f"[green]{result.message}[/green]")
    if platform.system() == "Darwin":
        _console.print("[dim]Log files are preserved at ~/Library/Logs/mindroom/[/dim]")


@service_app.command("start")
def start_service() -> None:
    """Start the installed MindRoom user service."""
    manager = _manager_or_exit()
    _print_service_action_result(manager.start_service())


@service_app.command("stop")
def stop_service() -> None:
    """Stop the installed MindRoom user service without removing it."""
    manager = _manager_or_exit()
    _print_service_action_result(manager.stop_service())


@service_app.command("restart")
def restart_service() -> None:
    """Restart the installed MindRoom user service."""
    manager = _manager_or_exit()
    _print_service_action_result(manager.restart_service())


def _service_runtime_paths(service_environment: Mapping[str, str]) -> RuntimePaths | None:
    """Return the installed service's runtime, from the environment saved in its unit (config and storage paths), not the caller's.

    Returns None when no installed unit or plist holds that environment, such as after a concurrent uninstall.
    """
    config_path = service_environment.get("MINDROOM_CONFIG_PATH")
    if config_path is None:
        return None
    return resolve_primary_runtime_paths(config_path=Path(config_path), process_env=dict(service_environment))


def _service_pairing_required(service_environment: Mapping[str, str]) -> bool:
    """Whether the installed service's runtime still waits for this machine to be paired, so its dashboard is not up yet."""
    from mindroom.matrix.provisioning_env import local_pairing_required  # noqa: PLC0415

    try:
        runtime_paths = _service_runtime_paths(service_environment)
        return runtime_paths is not None and local_pairing_required(runtime_paths)
    except ValueError:
        # An undecodable .env, incomplete credentials, or an unreadable secret file stop the service with its own error.
        return False


@service_app.command("status")
def service_status(
    logs: int = typer.Option(10, "--logs", "-l", help="Number of recent log lines to show. Use 0 to hide logs."),
) -> None:
    """Show MindRoom service status and recent logs."""
    manager = _manager_or_exit()
    status = manager.get_service_status()

    if not status.installed:
        _console.print("MindRoom service: [dim]not installed[/dim]")
    elif status.running:
        _console.print(f"MindRoom service: [green]running[/green] (pid {status.pid})")
        if _service_pairing_required(manager.get_service_environment()):
            _console.print(
                "pairing: [yellow]required[/yellow] "
                "(open the approval link from `mindroom service logs`, or run `mindroom connect`)",
            )
    else:
        _console.print("MindRoom service: [yellow]installed but not running[/yellow]")

    if logs > 0 and status.installed:
        log_lines = manager.get_recent_logs(logs)
        if log_lines:
            _console.print()
            _console.print(f"[dim]Recent logs ({len(log_lines)} lines):[/dim]")
            for line in log_lines:
                display_line = line[:120] + "..." if len(line) > 120 else line
                _console.print(f"  [dim]{display_line}[/dim]")
        elif status.running:
            _console.print()
            _console.print("[dim]No recent logs available[/dim]")

    _console.print()
    _console.print(f"[dim]Full logs: {manager.get_log_command()}[/dim]")


@service_app.command("logs")
def service_logs() -> None:
    """Follow MindRoom service logs."""
    manager = _manager_or_exit()
    try:
        result = subprocess.run(manager.get_log_args(), check=False)
    except KeyboardInterrupt:
        raise typer.Exit(0) from None
    except (OSError, subprocess.SubprocessError) as exc:
        _err_console.print(f"[bold red]Error:[/bold red] Failed to run log command: {exc}")
        raise typer.Exit(1) from None
    if result.returncode not in (0, -signal.SIGINT):
        raise typer.Exit(result.returncode)
