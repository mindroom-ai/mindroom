"""Where an agent's tools see a shared directory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.shared_directories.store import agent_shared_directories, shared_directory_relative_path
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_enabled_for_tool
from mindroom.workers.runtime import worker_visible_storage_root

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.shared_directories.store import SharedDirectory

_SHARE_READING_TOOLS = ("shell", "python", "file", "coding")


def agent_reads_shares_in_worker(config: Config, runtime_paths: RuntimePaths, agent_name: str) -> bool:
    """Return whether the agent's file-reading tools run in a dedicated worker that mounts its shares."""
    if worker_visible_storage_root(runtime_paths) is None:
        return False
    worker_tools = config.get_agent_worker_tools(agent_name)
    return any(
        sandbox_proxy_enabled_for_tool(tool_name, runtime_paths=runtime_paths, worker_tools_override=worker_tools)
        for tool_name in _SHARE_READING_TOOLS
    )


def shared_directory_view_path(runtime_paths: RuntimePaths, directory: SharedDirectory, *, in_worker: bool) -> Path:
    """Return the path of one shared directory as the agent's tools see it."""
    worker_root = worker_visible_storage_root(runtime_paths) if in_worker else None
    root = worker_root if worker_root is not None else runtime_paths.storage_root
    return root / shared_directory_relative_path(directory.owner, directory.name)


def describe_agent_shared_directories(config: Config, runtime_paths: RuntimePaths, agent_name: str) -> tuple[str, ...]:
    """Describe each shared directory one agent reaches by its path, owner, and access."""
    directories = agent_shared_directories(runtime_paths, config, agent_name)
    if not directories:
        return ()
    in_worker = agent_reads_shares_in_worker(config, runtime_paths, agent_name)
    return tuple(
        f"`{shared_directory_view_path(runtime_paths, directory, in_worker=in_worker)}` "
        + ("(yours, read-write)" if directory.owner == agent_name else f"(from {directory.owner}, read-only)")
        for directory in directories
    )
