"""Validation helpers for one cloud-side Desktop target configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from mindroom.desktop.protocol import MAX_COMMAND_TTL_MS
from mindroom.matrix.device_identity import PinnedMatrixDevice

DESKTOP_IDENTITY_FIELDS = frozenset({"device_user_id", "device_id", "device_ed25519"})


class DesktopConfigurationStatus(str, Enum):
    """Runtime readiness for one scoped Desktop configuration document."""

    READY = "ready"
    SETUP_REQUIRED = "setup_required"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class DesktopConfigurationState:
    """Validated Desktop target state without exposing stored values in errors."""

    status: DesktopConfigurationStatus
    missing_fields: tuple[str, ...] = ()
    error: str | None = None
    target: PinnedMatrixDevice | None = None
    timeout_seconds: float = 30.0


def desktop_configuration_state(credentials: dict[str, Any] | None) -> DesktopConfigurationState:
    """Return a fail-closed readiness state for one stored Desktop document."""
    values = credentials or {}
    timeout_value = values.get("timeout_seconds", 30.0)
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, int | float):
        return DesktopConfigurationState(
            status=DesktopConfigurationStatus.INVALID,
            error="Desktop timeout_seconds must be a number.",
        )
    if not 1 <= timeout_value <= MAX_COMMAND_TTL_MS / 1000:
        return DesktopConfigurationState(
            status=DesktopConfigurationStatus.INVALID,
            error=f"Desktop timeout_seconds must be between 1 and {MAX_COMMAND_TTL_MS // 1000}.",
        )
    timeout_seconds = float(timeout_value)

    missing_fields = tuple(sorted(field for field in DESKTOP_IDENTITY_FIELDS if not values.get(field)))
    if missing_fields:
        return DesktopConfigurationState(
            status=DesktopConfigurationStatus.SETUP_REQUIRED,
            missing_fields=missing_fields,
            timeout_seconds=timeout_seconds,
        )

    identity_values = {field: values[field] for field in DESKTOP_IDENTITY_FIELDS}
    if any(not isinstance(value, str) for value in identity_values.values()):
        return DesktopConfigurationState(
            status=DesktopConfigurationStatus.INVALID,
            error="Desktop device identity fields must be strings.",
            timeout_seconds=timeout_seconds,
        )

    try:
        target = PinnedMatrixDevice(
            user_id=identity_values["device_user_id"],
            device_id=identity_values["device_id"],
            ed25519=identity_values["device_ed25519"],
        )
    except ValueError as exc:
        return DesktopConfigurationState(
            status=DesktopConfigurationStatus.INVALID,
            error=str(exc),
        )
    return DesktopConfigurationState(
        status=DesktopConfigurationStatus.READY,
        target=target,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "DESKTOP_IDENTITY_FIELDS",
    "DesktopConfigurationState",
    "DesktopConfigurationStatus",
    "desktop_configuration_state",
]
