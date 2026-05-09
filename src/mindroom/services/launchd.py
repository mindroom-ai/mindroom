"""launchd service management for MindRoom on macOS."""

from __future__ import annotations

import contextlib
import os
import plistlib
import subprocess
from pathlib import Path

from mindroom.services.config import (
    InstallResult,
    ServiceManager,
    ServiceStatus,
    UninstallResult,
    build_service_command,
    find_uv,
    install_uv,
)
from mindroom.services.runtime import resolve_service_environment

_MACOS_UV_PATHS = [Path("/opt/homebrew/bin/uv")]
_LABEL = "chat.mindroom.local"


def _get_plist_path() -> Path:
    """Return the launchd plist path."""
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _get_log_dir() -> Path:
    """Return the launchd log directory."""
    return Path.home() / "Library" / "Logs" / "mindroom"


def _get_log_command() -> str:
    """Return the command for following service logs."""
    return f"tail -f {_get_log_dir()}/*.log"


def _get_recent_logs(num_lines: int = 10) -> list[str]:
    """Return recent service logs."""
    log_dir = _get_log_dir()
    lines: list[str] = []
    for log_file in [log_dir / "stdout.log", log_dir / "stderr.log"]:
        if log_file.exists():
            try:
                with log_file.open(encoding="utf-8") as f:
                    lines = [line.rstrip() for line in f.readlines()[-num_lines:]]
            except OSError:
                continue
            if lines:
                break
    return lines


def _generate_plist(
    uv_path: Path,
    home_dir: Path,
    log_dir: Path,
    service_environment: dict[str, str],
) -> dict[str, object]:
    """Generate the launchd plist dictionary."""
    return {
        "Label": _LABEL,
        "ProgramArguments": build_service_command(uv_path),
        "EnvironmentVariables": service_environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(home_dir),
        "StandardOutPath": str(log_dir / "stdout.log"),
        "StandardErrorPath": str(log_dir / "stderr.log"),
    }


def _get_service_status() -> ServiceStatus:
    """Return the installed/running status of the launchd service."""
    if not _get_plist_path().exists():
        return ServiceStatus(installed=False, running=False)

    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ServiceStatus(installed=True, running=False)

    pid = None
    for line in result.stdout.splitlines():
        if "pid =" in line.lower():
            _, _, raw_pid = line.partition("=")
            with contextlib.suppress(ValueError):
                pid = int(raw_pid.strip())
            break

    running = pid is not None and pid != 0
    return ServiceStatus(installed=True, running=running, pid=pid if running else None)


def _install_service() -> InstallResult:
    """Install and start the launchd service."""
    uv_path = find_uv(extra_paths=_MACOS_UV_PATHS)
    if uv_path is None:
        return InstallResult(success=False, message="uv not found. Install it from https://docs.astral.sh/uv/")

    home_dir = Path.home()
    log_dir = _get_log_dir()
    plist_path = _get_plist_path()
    log_dir.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    with plist_path.open("wb") as f:
        plistlib.dump(_generate_plist(uv_path, home_dir, log_dir, resolve_service_environment()), f)

    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(plist_path)], capture_output=True, check=False)
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return InstallResult(success=False, message=f"Failed to load service: {result.stderr.strip()}", log_dir=log_dir)

    return InstallResult(success=True, message="Installed and started", log_dir=log_dir)


def _uninstall_service() -> UninstallResult:
    """Stop and remove the launchd service."""
    plist_path = _get_plist_path()
    if not plist_path.exists():
        return UninstallResult(success=True, message="Service was not installed")

    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        check=False,
    )
    was_running = result.returncode == 0
    plist_path.unlink()
    return UninstallResult(
        success=True,
        message="Service stopped and removed" if was_running else "Service removed",
        was_running=was_running,
    )


def _check_uv_installed() -> tuple[bool, Path | None]:
    """Return whether uv is installed."""
    uv_path = find_uv(extra_paths=_MACOS_UV_PATHS)
    return uv_path is not None, uv_path


manager = ServiceManager(
    check_uv_installed=_check_uv_installed,
    install_uv=install_uv,
    install_service=_install_service,
    uninstall_service=_uninstall_service,
    get_service_status=_get_service_status,
    get_log_command=_get_log_command,
    get_recent_logs=_get_recent_logs,
)
