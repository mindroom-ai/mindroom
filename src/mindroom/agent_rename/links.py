"""Plan pointing the symlinks into an agent's state root at the root's new place.

An operator can share one agent's state root with another agent through a link such as ``agents/sidekick -> atlas``.
Once ``agents/atlas`` moves to ``agents/nova`` that link dangles, and MindRoom resolves it at its next start and
creates an empty ``agents/atlas``, so the other agent loses its history. Every such link in the storage root or
directly in the config directory is pointed at the moved root.
A link's target is worked out from its text the way ``os.path.normpath`` does, without following any link,
so a link into ``agents/code_team`` never counts as one into ``agents/code``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.agent_rename.scan import is_skipped
from mindroom.agent_rename.steps import RetargetLink
from mindroom.tool_system.worker_routing import agent_state_root_path

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.constants import RuntimePaths


def link_steps(runtime_paths: RuntimePaths, *, old_name: str, new_name: str) -> list[RetargetLink]:
    """Return a step for every symlink pointing into ``agents/<OLD>`` that points into ``agents/<NEW>`` afterwards.

    The storage root is walked like the leftover scan walks it, without entering linked directories, and the config
    directory only at its top level, since it can be a project checkout or a home directory.
    The steps run after the state root moved, so a link inside it is planned at its place in the moved root,
    and one whose text already works from there gets no step.
    The new text renames only the path components that enter ``agents/<OLD>``,
    so relative links stay relative and absolute links keep their prefix.
    """
    storage_root = runtime_paths.storage_root
    old_root = agent_state_root_path(storage_root, old_name)
    new_root = agent_state_root_path(storage_root, new_name)
    steps: dict[Path, RetargetLink] = {}
    for link in (*_storage_links(storage_root), *_directory_links(runtime_paths.config_dir)):
        moved = new_root / link.relative_to(old_root) if link.is_relative_to(old_root) else link
        # Path.readlink would drop a trailing / or ./ that the new text keeps.
        text = os.readlink(link)  # noqa: PTH115
        target = _retargeted(moved.parent, text, old_root=old_root, new_name=new_root.name)
        if target is not None:
            description = (
                f"Point the symlink {moved} at {target} instead of {text}, where the state of agent {old_name} moved"
            )
            steps[moved] = RetargetLink(moved, text, target, description)
    return [steps[path] for path in sorted(steps)]


def _storage_links(storage_root: Path) -> Iterator[Path]:
    for directory, directory_names, file_names in os.walk(storage_root):
        current = Path(directory)
        relative = current.relative_to(storage_root)
        # Links to directories are listed among the directories, and os.walk does not enter them.
        directory_names[:] = [name for name in directory_names if not is_skipped(relative / name)]
        yield from (current / name for name in (*directory_names, *file_names) if (current / name).is_symlink())


def _directory_links(directory: Path) -> list[Path]:
    try:
        return [entry for entry in directory.iterdir() if entry.is_symlink()]
    except OSError:
        return []


def _retargeted(parent: Path, text: str, *, old_root: Path, new_name: str) -> str | None:
    """Return the link text ``text`` with each component that enters ``old_root`` renamed to ``new_name``.

    ``parent`` is the directory holding the link. Returns ``None`` when the text does not point into ``old_root``.
    Both roots sit in the same ``agents/`` directory, so renaming the entering component moves the target along.
    """
    if not Path(os.path.normpath(parent / text)).is_relative_to(old_root):
        return None
    components = text.split("/")
    # An absolute text starts with an empty component.
    position = parent if components[0] else Path("/")
    for index, component in enumerate(components):
        if component == "..":
            position = position.parent
        elif component not in ("", "."):
            position /= component
            if position == old_root:
                components[index] = new_name
    return "/".join(components)
