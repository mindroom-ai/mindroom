"""Runtime context helpers for installed MindRoom services."""

from __future__ import annotations

from pathlib import Path

from mindroom.constants import resolve_primary_runtime_paths, subprocess_path_with_prepends

_USER_PATH_ENTRIES = (
    str(Path.home() / ".local" / "bin"),
    str(Path.home() / ".cargo" / "bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


class ServiceConfigMissingError(RuntimeError):
    """Raised when service installation cannot find an active MindRoom config."""


def resolve_service_environment(uv_path: Path) -> dict[str, str]:
    """Resolve the runtime path environment captured by installed services."""
    runtime_paths = resolve_primary_runtime_paths()
    if not runtime_paths.config_path.exists():
        msg = (
            f"No config.yaml found at {runtime_paths.config_path}. "
            "Run `mindroom config init` to create one before installing the service."
        )
        raise ServiceConfigMissingError(msg)

    path = subprocess_path_with_prepends(
        runtime_paths.process_env.get("PATH"),
        prepend_entries=(str(uv_path.parent), *_USER_PATH_ENTRIES),
    )
    if path is None:
        path = ""

    return {
        "MINDROOM_CONFIG_PATH": str(runtime_paths.config_path),
        "MINDROOM_STORAGE_PATH": str(runtime_paths.storage_root),
        "PATH": path,
    }
