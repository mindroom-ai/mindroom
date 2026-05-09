"""Runtime context helpers for installed MindRoom services."""

from __future__ import annotations

from mindroom.constants import resolve_primary_runtime_paths


def resolve_service_environment() -> dict[str, str]:
    """Resolve the runtime path environment captured by installed services."""
    runtime_paths = resolve_primary_runtime_paths()
    return {
        "MINDROOM_CONFIG_PATH": str(runtime_paths.config_path),
        "MINDROOM_STORAGE_PATH": str(runtime_paths.storage_root),
    }
