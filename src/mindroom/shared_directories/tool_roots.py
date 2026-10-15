"""Let workspace-confined path tools reach the shared directories of their agent.

A path inside a shared directory is handed to a copy of the toolkit rooted at that
directory, so the toolkit's existing workspace confinement, no-follow reads, and
atomic writes apply unchanged with the shared directory as the root.
"""

from __future__ import annotations

import copy
import functools
import inspect
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.shared_directories.store import agent_shared_directories, shared_directory_relative_path
from mindroom.tool_system.worker_routing import SHARED_DIRECTORIES_DIRNAME

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


@dataclass(frozen=True, slots=True)
class SharedDirectoryRoot:
    """One shared directory a path tool may use, and whether it may write there."""

    path: Path
    writable: bool


class _SharedDirectoryAwareToolkit(Protocol):
    base_dir: Path
    restrict_to_base_dir: bool
    shared_directory_roots: tuple[SharedDirectoryRoot, ...]


def _mounted_roots(runtime_paths: RuntimePaths) -> tuple[SharedDirectoryRoot, ...]:
    # A dedicated worker mounts exactly the directories its agents own or joined, read-only where they only
    # joined, below its shared storage root; the shared sidecar mounts every directory below its own storage root.
    shared_storage_root = runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"], default="")
    root = (
        Path(shared_storage_root) if shared_storage_root else runtime_paths.storage_root
    ) / SHARED_DIRECTORIES_DIRNAME
    roots: list[SharedDirectoryRoot] = []
    try:
        with os.scandir(root) as owner_entries:
            owners = sorted(entry.name for entry in owner_entries if entry.is_dir(follow_symlinks=False))
        for owner in owners:
            with os.scandir(root / owner) as name_entries:
                names = sorted(entry.name for entry in name_entries if entry.is_dir(follow_symlinks=False))
            roots.extend(SharedDirectoryRoot(path=root / owner / name, writable=True) for name in names)
    except OSError:
        return tuple(roots)
    return tuple(roots)


def shared_directory_roots(
    runtime_paths: RuntimePaths,
    config: Config | None,
    agent_name: str | None,
) -> tuple[SharedDirectoryRoot, ...]:
    """Return the shared directories one agent's path tools may use in this runtime."""
    if runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]):
        return _mounted_roots(runtime_paths)
    if config is None or agent_name is None:
        return ()
    return tuple(
        SharedDirectoryRoot(
            path=runtime_paths.storage_root / shared_directory_relative_path(directory.owner, directory.name),
            writable=directory.owner == agent_name,
        )
        for directory in agent_shared_directories(runtime_paths, config, agent_name)
    )


def _matching_root(
    raw_path: object,
    base_dir: Path,
    roots: tuple[SharedDirectoryRoot, ...],
) -> tuple[SharedDirectoryRoot, str] | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    requested = Path(raw_path).expanduser()
    # Lexical only: the rooted toolkit resolves links itself and refuses any that leave the root.
    candidate = Path(os.path.normpath(requested if requested.is_absolute() else base_dir / requested))
    if candidate.is_relative_to(base_dir):
        return None
    for root in roots:
        if candidate.is_relative_to(root.path):
            return root, str(candidate.relative_to(root.path))
    return None


def shared_directory_aware(path_parameter: str, *, writes: bool) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Route a workspace-confined tool method to the shared directory its path argument names."""

    def decorator(method: Callable[..., str]) -> Callable[..., str]:
        signature = inspect.signature(method)

        @functools.wraps(method)
        def wrapper(*args: object, **kwargs: object) -> str:
            bound = signature.bind(*args, **kwargs)
            toolkit: _SharedDirectoryAwareToolkit = bound.arguments["self"]
            if not toolkit.restrict_to_base_dir or not toolkit.shared_directory_roots:
                return method(*args, **kwargs)
            raw_path = bound.arguments.get(path_parameter)
            match = _matching_root(raw_path, toolkit.base_dir, toolkit.shared_directory_roots)
            if match is None:
                return method(*args, **kwargs)
            root, relative_path = match
            if writes and not root.writable:
                return f"Error: {raw_path} is in a shared directory you can only read."
            # The tools package imports every toolkit, and those toolkits import this module.
            from mindroom.tools.path_safety import resolve_tool_base_dir  # noqa: PLC0415

            rooted = copy.copy(toolkit)
            try:
                rooted.base_dir = resolve_tool_base_dir(root.path)
            except ValueError as exc:
                return f"Error: {exc}"
            rooted.shared_directory_roots = ()
            bound.arguments["self"] = rooted
            bound.arguments[path_parameter] = relative_path
            return method(*bound.args, **bound.kwargs)

        return wrapper

    return decorator
