"""The tool-job state of each running instance, owned by its coordinator and keyed by storage root."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.models import BackgroundToolJobsConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_jobs.disabled import ParkedWork
    from mindroom.tool_jobs.runtime import ToolJobRuntime


@dataclass
class ToolJobInstance:
    """One running instance's startup setting and, depending on it, its recovered runtime or parked work."""

    settings: BackgroundToolJobsConfig
    runtime: ToolJobRuntime | None = None
    parked: ParkedWork | None = None


_instances: dict[Path, ToolJobInstance] = {}


def tool_job_instance(runtime_paths: RuntimePaths) -> ToolJobInstance | None:
    """Find the running instance of one storage root, if any."""
    return _instances.get(runtime_paths.storage_root)


def pin_background_tool_jobs(config: Config, runtime_paths: RuntimePaths) -> ToolJobInstance:
    """Start an instance with a frozen copy of the authored setting, replacing whatever a stopped one left."""
    instance = ToolJobInstance(config.background_tool_jobs.model_copy(deep=True))
    _instances[runtime_paths.storage_root] = instance
    return instance


def release_background_tool_jobs(runtime_paths: RuntimePaths, instance: ToolJobInstance) -> None:
    """Withdraw a stopped instance's setting, runtime, and parked work together, unless a newer instance replaced it."""
    if _instances.get(runtime_paths.storage_root) is instance:
        del _instances[runtime_paths.storage_root]
