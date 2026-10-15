"""Where an agent's tools see a shared directory."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from mindroom.api.sandbox_exec import runner_storage_root
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.shared_directories.store import agent_shared_directories, shared_directory_relative_path
from mindroom.shared_directories.tool_roots import SharedDirectoryRoot
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config, sandbox_proxy_enabled_for_tool
from mindroom.tool_system.worker_routing import SHARED_DIRECTORIES_DIRNAME
from mindroom.workers.runtime import primary_worker_backend_name, worker_visible_storage_root

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


def _real_subdirectories(directory: Path) -> list[str]:
    with os.scandir(directory) as entries:
        return sorted(entry.name for entry in entries if entry.is_dir(follow_symlinks=False))


def _mounted_roots(root: Path) -> tuple[SharedDirectoryRoot, ...]:
    # A dedicated worker mounts exactly the directories its agents own or joined, read-only where they only joined;
    # the shared sidecar sees every directory.
    if not root.is_dir():
        return ()
    roots: list[SharedDirectoryRoot] = []
    for owner in _real_subdirectories(root):
        for name in _real_subdirectories(root / owner):
            path = root / owner / name
            writable = not os.statvfs(path).f_flag & os.ST_RDONLY
            roots.append(SharedDirectoryRoot(path=path, writable=writable, spellings=(path,)))
    return tuple(roots)


def shared_directory_roots(
    runtime_paths: RuntimePaths,
    config: Config | None,
    agent_name: str | None,
) -> tuple[SharedDirectoryRoot, ...]:
    """Return the shared directories one agent's path tools may use in this runtime."""
    if runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]):
        return _mounted_roots(runner_storage_root(runtime_paths) / SHARED_DIRECTORIES_DIRNAME)
    if config is None or agent_name is None:
        return ()
    if primary_worker_backend_name(runtime_paths) == "static_runner" and sandbox_proxy_config(runtime_paths).proxy_url:
        # The shared sidecar writes shared_directories/, so it could swap an owner directory for a link that a
        # primary-process path tool would follow into primary-only storage.
        return ()
    worker_root = worker_visible_storage_root(runtime_paths)
    roots: list[SharedDirectoryRoot] = []
    for directory in agent_shared_directories(runtime_paths, config, agent_name):
        relative_path = shared_directory_relative_path(directory.owner, directory.name)
        path = runtime_paths.storage_root / relative_path
        # Prompts name the worker path when the agent's other tools run in a worker, so both spellings work here.
        spellings = (path,) if worker_root is None else (path, worker_root / relative_path)
        roots.append(SharedDirectoryRoot(path=path, writable=directory.owner == agent_name, spellings=spellings))
    return tuple(roots)
