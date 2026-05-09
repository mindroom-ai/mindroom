"""Platform selection for MindRoom user service managers."""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING

from mindroom.services.launchd import manager as launchd_manager
from mindroom.services.systemd import manager as systemd_manager

if TYPE_CHECKING:
    from mindroom.services.config import ServiceManager


def get_service_manager() -> ServiceManager:
    """Return the launchd or systemd user service manager for this platform."""
    system = platform.system()
    if system == "Darwin":
        return launchd_manager
    if system == "Linux":
        return systemd_manager
    msg = (
        f"Unsupported platform: {system}\n\n"
        "MindRoom user services are managed with launchd on macOS and systemd on Linux.\n"
        "Run MindRoom manually instead with: mindroom run"
    )
    raise RuntimeError(msg)
