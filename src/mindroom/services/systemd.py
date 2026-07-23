"""systemd user service management for MindRoom on Linux."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from mindroom.services.config import (
    SERVICE_NAME,
    SERVICE_NOT_INSTALLED_MESSAGE,
    InstallResult,
    ServiceActionResult,
    ServiceManager,
    ServiceStatus,
    UninstallResult,
    build_service_command,
    find_uv,
    install_uv,
)
from mindroom.services.runtime import ServiceConfigMissingError, resolve_service_environment

_LINUX_UV_PATHS = [Path("/usr/bin/uv")]


def _get_unit_name() -> str:
    """Return the systemd unit name."""
    return f"{SERVICE_NAME}.service"


def _get_unit_path() -> Path:
    """Return the systemd user unit path."""
    return Path.home() / ".config" / "systemd" / "user" / _get_unit_name()


def _get_log_command() -> str:
    """Return the command for following service logs."""
    return f"journalctl --user -u {SERVICE_NAME} -f"


def _get_log_args() -> list[str]:
    """Return the argv for following service logs."""
    return ["journalctl", "--user", "-u", SERVICE_NAME, "-f"]


def _get_recent_logs(num_lines: int = 10) -> list[str]:
    """Return recent service logs from journalctl."""
    result = subprocess.run(
        [
            "journalctl",
            "--user",
            "-u",
            SERVICE_NAME,
            "-n",
            str(num_lines),
            "--no-pager",
            "-o",
            "cat",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line.rstrip() for line in result.stdout.splitlines() if line.strip()]


def _quote_environment_assignment(name: str, value: str) -> str:
    """Return one safely quoted systemd Environment assignment."""
    escaped_value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{name}={escaped_value}"'


def _generate_unit_file(uv_path: Path, service_environment: dict[str, str]) -> str:
    """Generate the systemd unit file content."""
    exec_start = " ".join(shlex.quote(part) for part in build_service_command(uv_path))
    environment = "\n".join(
        f"Environment={_quote_environment_assignment(name, value)}"
        for name, value in sorted(service_environment.items())
    )
    return f"""[Unit]
Description=MindRoom
After=network.target

[Service]
ExecStart={exec_start}
{environment}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _get_service_status() -> ServiceStatus:
    """Return the installed/running status of the systemd user service."""
    if not _get_unit_path().exists():
        return ServiceStatus(installed=False, running=False)

    unit_name = _get_unit_name()
    result = subprocess.run(
        ["systemctl", "--user", "is-active", unit_name],
        capture_output=True,
        text=True,
        check=False,
    )
    running = result.returncode == 0 and result.stdout.strip() == "active"
    pid = None
    if running:
        pid_result = subprocess.run(
            ["systemctl", "--user", "show", unit_name, "--property=MainPID"],
            capture_output=True,
            text=True,
            check=False,
        )
        if pid_result.returncode == 0:
            _, _, raw_pid = pid_result.stdout.strip().partition("=")
            try:
                parsed_pid = int(raw_pid)
            except ValueError:
                parsed_pid = 0
            pid = parsed_pid or None

    return ServiceStatus(installed=True, running=running, pid=pid)


def _install_service() -> InstallResult:
    """Install and start the systemd user service."""
    uv_path = find_uv(extra_paths=_LINUX_UV_PATHS)
    if uv_path is None:
        return InstallResult(success=False, message="uv not found. Install it from https://docs.astral.sh/uv/")

    unit_path = _get_unit_path()
    unit_name = _get_unit_name()
    try:
        service_environment = resolve_service_environment(uv_path)
    except ServiceConfigMissingError as exc:
        return InstallResult(success=False, message=str(exc))

    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(_generate_unit_file(uv_path, service_environment), encoding="utf-8")

    subprocess.run(["systemctl", "--user", "stop", unit_name], capture_output=True, check=False)

    result = subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return InstallResult(success=False, message=f"Failed to reload systemd: {result.stderr.strip()}")

    result = subprocess.run(
        ["systemctl", "--user", "enable", unit_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return InstallResult(success=False, message=f"Failed to enable service: {result.stderr.strip()}")

    result = subprocess.run(
        ["systemctl", "--user", "start", unit_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return InstallResult(success=False, message=f"Failed to start service: {result.stderr.strip()}")

    return InstallResult(success=True, message="Installed and started")


def _run_systemctl_action(action: str, success_message: str) -> ServiceActionResult:
    """Run one systemd user-service lifecycle action."""
    unit_path = _get_unit_path()
    if not unit_path.exists():
        return ServiceActionResult(success=False, message=SERVICE_NOT_INSTALLED_MESSAGE)

    result = subprocess.run(
        ["systemctl", "--user", action, _get_unit_name()],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip()
        message = f"Failed to {action} service"
        if detail:
            message = f"{message}: {detail}"
        return ServiceActionResult(success=False, message=message)

    return ServiceActionResult(success=True, message=success_message)


def _start_service() -> ServiceActionResult:
    """Start the installed systemd user service."""
    return _run_systemctl_action("start", "Service started")


def _stop_service() -> ServiceActionResult:
    """Stop the installed systemd user service without removing it."""
    return _run_systemctl_action("stop", "Service stopped")


def _restart_service() -> ServiceActionResult:
    """Restart the installed systemd user service."""
    return _run_systemctl_action("restart", "Service restarted")


def _uninstall_service() -> UninstallResult:
    """Stop and remove the systemd user service."""
    unit_path = _get_unit_path()
    unit_name = _get_unit_name()
    if not unit_path.exists():
        return UninstallResult(success=True, message="Service was not installed")

    was_running = _get_service_status().running
    subprocess.run(["systemctl", "--user", "stop", unit_name], capture_output=True, check=False)
    subprocess.run(["systemctl", "--user", "disable", unit_name], capture_output=True, check=False)
    unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
    return UninstallResult(
        success=True,
        message="Service stopped and removed" if was_running else "Service removed",
        was_running=was_running,
    )


def _check_uv_installed() -> tuple[bool, Path | None]:
    """Return whether uv is installed."""
    uv_path = find_uv(extra_paths=_LINUX_UV_PATHS)
    return uv_path is not None, uv_path


manager = ServiceManager(
    check_uv_installed=_check_uv_installed,
    install_uv=install_uv,
    install_service=_install_service,
    uninstall_service=_uninstall_service,
    start_service=_start_service,
    stop_service=_stop_service,
    restart_service=_restart_service,
    get_service_status=_get_service_status,
    get_log_command=_get_log_command,
    get_log_args=_get_log_args,
    get_recent_logs=_get_recent_logs,
)
