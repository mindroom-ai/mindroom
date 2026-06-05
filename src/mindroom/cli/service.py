"""CLI commands for installing MindRoom as a user service."""

from __future__ import annotations

import platform
import signal
import subprocess
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.panel import Panel

if TYPE_CHECKING:
    from mindroom.services.config import ServiceActionResult, ServiceManager

_console = Console()
_err_console = Console(stderr=True)

service_app = typer.Typer(
    name="service",
    help="""Install and manage MindRoom as a background user service.

MindRoom runs through `uv tool run` and starts automatically at login.

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
    return not answer or answer == "y"


def _ensure_uv_installed(*, no_confirm: bool) -> None:
    """Ensure uv is installed before service installation."""
    manager = _manager_or_exit()
    uv_installed, uv_path = manager.check_uv_installed()
    if uv_installed:
        _console.print(f"  [green]uv installed:[/green] {uv_path}")
        return

    _console.print("[yellow]uv is required to run the MindRoom service.[/yellow]")
    if not no_confirm and not _confirm_action("Install uv now?"):
        _console.print("[yellow]Install uv from https://docs.astral.sh/uv/ and run this command again.[/yellow]")
        raise typer.Exit(1)

    _console.print("Installing uv...")
    success, message = manager.install_uv()
    if not success:
        _err_console.print(f"[bold red]Error:[/bold red] {message}")
        raise typer.Exit(1)
    _console.print(f"  [green]{message}[/green]")


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

    if not skip_deps:
        _ensure_uv_installed(no_confirm=no_confirm)

    if not no_confirm:
        _console.print()
        _console.print("[bold]Will install:[/bold] MindRoom user service")
        if not _confirm_action("Continue?"):
            _console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    result = manager.install_service()
    if not result.success:
        _err_console.print(f"[bold red]Error:[/bold red] {result.message}")
        raise typer.Exit(1)

    log_hint = f"View logs: [cyan]{manager.get_log_command()}[/cyan]"
    if result.log_dir is not None:
        log_hint = f"View logs: [cyan]{result.log_dir}/[/cyan]"
    _console.print(
        Panel(
            f"[green]{result.message}[/green]\n\nCheck status: [cyan]mindroom service status[/cyan]\n{log_hint}",
            title="Service Installed",
            border_style="green",
        ),
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
