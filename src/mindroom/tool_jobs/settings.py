"""The authored tool-job opt-in, pinned for one running instance."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_jobs.instances import tool_job_instance

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.config.models import BackgroundToolJobsConfig
    from mindroom.constants import RuntimePaths


def background_tool_jobs_enabled(config: Config, runtime_paths: RuntimePaths) -> bool:
    """Use the startup setting, or the authored setting outside a managed instance."""
    return _effective_settings(config, runtime_paths).enabled


def toolkit_is_background_excluded(name: str, config: Config, runtime_paths: RuntimePaths) -> bool:
    """Apply the same startup-pinned toolkit policy at every execution boundary."""
    return name in _effective_settings(config, runtime_paths).exclude_toolkits


def _effective_settings(config: Config, runtime_paths: RuntimePaths) -> BackgroundToolJobsConfig:
    instance = tool_job_instance(runtime_paths)
    return instance.settings if instance is not None else config.background_tool_jobs


def pending_background_tool_jobs_restart(config: Config, runtime_paths: RuntimePaths) -> bool:
    """Report saved changes without changing any running execution envelope."""
    effective = _effective_settings(config, runtime_paths)
    authored = config.background_tool_jobs
    return effective.enabled != authored.enabled or set(effective.exclude_toolkits) != set(authored.exclude_toolkits)
