"""Shared configuration for MindRoom user service managers."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from packaging.version import Version

if TYPE_CHECKING:
    from collections.abc import Callable


SERVICE_NAME = "mindroom"
SERVICE_NOT_INSTALLED_MESSAGE = "Service is not installed. Run `mindroom service install` first."
_PACKAGE_NAME = "mindroom"


@dataclass(frozen=True)
class ServiceStatus:
    """Status of the MindRoom user service."""

    installed: bool
    running: bool
    pid: int | None = None


@dataclass(frozen=True)
class InstallResult:
    """Result of installing the MindRoom user service."""

    success: bool
    message: str
    log_dir: Path | None = None


@dataclass(frozen=True)
class UninstallResult:
    """Result of uninstalling the MindRoom user service."""

    success: bool
    message: str
    was_running: bool = False


@dataclass(frozen=True)
class ServiceActionResult:
    """Result of starting, stopping, or restarting the MindRoom user service."""

    success: bool
    message: str


class ServiceManager(NamedTuple):
    """Platform-specific MindRoom service manager interface."""

    check_uv_installed: Callable[[], tuple[bool, Path | None]]
    install_uv: Callable[[], tuple[bool, str]]
    install_service: Callable[[], InstallResult]
    uninstall_service: Callable[[], UninstallResult]
    start_service: Callable[[], ServiceActionResult]
    stop_service: Callable[[], ServiceActionResult]
    restart_service: Callable[[], ServiceActionResult]
    get_service_status: Callable[[], ServiceStatus]
    get_log_command: Callable[[], str]
    get_log_args: Callable[[], list[str]]
    get_recent_logs: Callable[[int], list[str]]


def build_service_command(uv_path: Path, *, package_version: str | None = None) -> list[str]:
    """Build a service command pinned to the installing MindRoom version."""
    installed_version = Version(package_version or distribution_version(_PACKAGE_NAME))
    resolved_version = installed_version.public
    if installed_version.is_devrelease:
        resolved_version = resolved_version.partition(".post")[0].partition(".dev")[0]
    requirement = f"{_PACKAGE_NAME}=={resolved_version}"
    return [str(uv_path), "tool", "run", "--from", requirement, "mindroom", "run"]


def find_uv(extra_paths: list[Path] | None = None) -> Path | None:
    """Find uv, preferring common install locations over virtualenv shims."""
    paths = [
        Path.home() / ".local" / "bin" / "uv",
        Path.home() / ".cargo" / "bin" / "uv",
        Path("/usr/local/bin/uv"),
    ]
    if extra_paths:
        paths = extra_paths + paths

    for path in paths:
        if path.is_file() and os.access(path, os.X_OK):
            return path

    which_result = shutil.which("uv")
    return Path(which_result) if which_result else None


def install_uv() -> tuple[bool, str]:
    """Install uv using the official installer."""
    try:
        result = subprocess.run(
            ["curl", "-LsSf", "https://astral.sh/uv/install.sh"],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["sh"],
            input=result.stdout,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        return False, f"Failed to install uv: {exc}"
    return True, "uv installed successfully"
