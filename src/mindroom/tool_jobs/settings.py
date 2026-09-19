"""The authored tool-job opt-in, pinned for one running instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.models import BackgroundToolJobsConfig
    from mindroom.constants import RuntimePaths

_STARTED: dict[Path, BackgroundToolJobsConfig] = {}


def pin_background_tool_jobs(config: Config, runtime_paths: RuntimePaths) -> bool:
    """Freeze the setting before any responder or recovery owner starts."""
    return _STARTED.setdefault(
        runtime_paths.storage_root.resolve(),
        config.background_tool_jobs.model_copy(deep=True),
    ).enabled


def release_background_tool_jobs(runtime_paths: RuntimePaths) -> None:
    """Release the stopped instance's setting so a new startup can choose again."""
    _STARTED.pop(runtime_paths.storage_root.resolve(), None)


def background_tool_jobs_enabled(config: Config, runtime_paths: RuntimePaths) -> bool:
    """Use the startup setting, or the authored setting outside a managed instance."""
    return _effective_settings(config, runtime_paths).enabled


def toolkit_is_background_excluded(name: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Apply the same startup-pinned toolkit policy at every execution boundary."""
    return name in _effective_settings(config, runtime_paths).exclude_toolkits


def _effective_settings(config: Config, runtime_paths: RuntimePaths) -> BackgroundToolJobsConfig:
    return _STARTED.get(runtime_paths.storage_root.resolve(), config.background_tool_jobs)


def pending_background_tool_jobs_restart(config: Config, runtime_paths: RuntimePaths) -> bool:
    """Report saved changes without changing any running execution envelope."""
    return _effective_settings(config, runtime_paths) != config.background_tool_jobs
