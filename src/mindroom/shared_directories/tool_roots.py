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

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True, slots=True)
class SharedDirectoryRoot:
    """One shared directory a path tool may use and whether it may write there."""

    path: Path
    writable: bool


def no_shared_directory_roots() -> tuple[SharedDirectoryRoot, ...]:
    """Return no shared directories, for toolkits built without an agent."""
    return ()


class _SharedDirectoryAwareToolkit(Protocol):
    base_dir: Path
    restrict_to_base_dir: bool
    shared_directory_roots: Callable[[], tuple[SharedDirectoryRoot, ...]]


def _matching_root(
    raw_path: object,
    base_dir: Path,
    roots: Callable[[], tuple[SharedDirectoryRoot, ...]],
) -> tuple[SharedDirectoryRoot, Path] | None:
    """Return the shared directory a path names and the path below it."""
    if not isinstance(raw_path, str) or not raw_path:
        return None
    requested = Path(raw_path).expanduser()
    # Lexical only: the rooted toolkit resolves links itself and refuses any that leave the root.
    candidate = Path(os.path.normpath(requested if requested.is_absolute() else base_dir / requested))
    if candidate.is_relative_to(base_dir):
        return None
    for root in roots():
        if candidate.is_relative_to(root.path):
            return root, candidate.relative_to(root.path)
    return None


def shared_directory_aware(path_parameter: str, *, writes: bool) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Route a workspace-confined tool method to the shared directory its path argument names."""

    def decorator(method: Callable[..., str]) -> Callable[..., str]:
        signature = inspect.signature(method)

        @functools.wraps(method)
        def wrapper(*args: object, **kwargs: object) -> str:
            bound = signature.bind(*args, **kwargs)
            toolkit: _SharedDirectoryAwareToolkit = bound.arguments["self"]
            if not toolkit.restrict_to_base_dir:
                return method(*args, **kwargs)
            raw_path = bound.arguments.get(path_parameter)
            match = _matching_root(raw_path, toolkit.base_dir, toolkit.shared_directory_roots)
            if match is None:
                return method(*args, **kwargs)
            root, relative = match
            if writes and not root.writable:
                return f"Error: {raw_path} is in a shared directory you can only read."
            # The tools package imports every toolkit, and those toolkits import this module.
            from mindroom.tools.path_safety import resolve_tool_base_dir, spell_output_paths  # noqa: PLC0415

            rooted = copy.copy(toolkit)
            try:
                rooted.base_dir = resolve_tool_base_dir(root.path)
            except ValueError as exc:
                return f"Error: {exc}"
            rooted.shared_directory_roots = no_shared_directory_roots
            bound.arguments["self"] = rooted
            # An absolute path below the pinned root keeps the toolkit's own messages absolute too.
            bound.arguments[path_parameter] = str(rooted.base_dir / relative)
            with spell_output_paths(rooted.base_dir, root.path):
                return method(*bound.args, **bound.kwargs)

        return wrapper

    return decorator
