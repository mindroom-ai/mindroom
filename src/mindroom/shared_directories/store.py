"""The primary-owned ledger of shared directories and their storage below the storage root.

The ledger lives below ``tracking/``, which no worker mounts, so worker code can
never forge membership. Each directory lives at
``shared_directories/<owner>/<name>/``; no worker mounts the two levels above it,
so worker code cannot replace a directory another worker mounts with a link.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.tool_system.worker_routing import SHARED_DIRECTORIES_DIRNAME
from mindroom.workers.worker_retirement import remove_directory_tree_at

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_LEDGER_FILENAME = "shared_directories.json"
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ledger_lock = threading.Lock()
logger = get_logger(__name__)


class SharedDirectoryError(ValueError):
    """A shared directory request that cannot be applied; the message is shown to the model."""


@dataclass(frozen=True, slots=True)
class SharedDirectory:
    """One directory an agent owns and the agents that may read it."""

    owner: str
    name: str
    members: tuple[str, ...]


def shared_directory_relative_path(owner: str, name: str) -> Path:
    """Return a directory's location relative to the storage root."""
    return Path(SHARED_DIRECTORIES_DIRNAME, owner, name)


def _ledger_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _LEDGER_FILENAME


def _parse_record(key: str, raw: object) -> SharedDirectory | None:
    if not isinstance(raw, dict):
        return None
    record = cast("dict[str, object]", raw)
    owner, name, members = (record.get(field) for field in ("owner", "name", "members"))
    if (
        not isinstance(owner, str)
        or not isinstance(name, str)
        or not isinstance(members, list)
        or not _NAME_PATTERN.fullmatch(name)
        or key != f"{owner}/{name}"
    ):
        return None
    member_names = cast("list[object]", members)
    if not all(isinstance(member, str) for member in member_names):
        return None
    return SharedDirectory(
        owner=owner,
        name=name,
        members=tuple(sorted(set(cast("list[str]", member_names)))),
    )


def _read_ledger(runtime_paths: RuntimePaths, *, for_update: bool) -> dict[str, SharedDirectory]:
    """Read the ledger; a change refuses an unreadable one so it never writes back without its records."""
    try:
        data = json.loads(_ledger_path(runtime_paths).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if for_update:
            msg = f"The shared directory records could not be read, so nothing was changed: {exc}"
            raise SharedDirectoryError(msg) from exc
        logger.warning("Shared directory records could not be read", error=str(exc))
        return {}
    if not isinstance(data, dict):
        if for_update:
            msg = "The shared directory records are not a JSON object, so nothing was changed."
            raise SharedDirectoryError(msg)
        return {}
    directories = {}
    for key, raw in cast("dict[object, object]", data).items():
        if isinstance(key, str) and (directory := _parse_record(key, raw)) is not None:
            directories[key] = directory
    return directories


def _write_ledger(runtime_paths: RuntimePaths, directories: dict[str, SharedDirectory]) -> None:
    payload = {
        key: {
            "owner": directory.owner,
            "name": directory.name,
            "members": list(directory.members),
        }
        for key, directory in directories.items()
    }
    write_json_file_durable(_ledger_path(runtime_paths), payload, indent=2, sort_keys=True)


def load_shared_directories(runtime_paths: RuntimePaths) -> tuple[SharedDirectory, ...]:
    """Return every recorded shared directory, sorted by owner and name."""
    directories = _read_ledger(runtime_paths, for_update=False).values()
    return tuple(sorted(directories, key=lambda directory: (directory.owner, directory.name)))


def is_shareable_agent(config: Config, agent_name: str) -> bool:
    """Return whether an agent may own or join shared directories: configured and not private."""
    agent = config.agents.get(agent_name)
    return agent is not None and agent.private is None


def agent_shared_directories(
    runtime_paths: RuntimePaths,
    config: Config,
    agent_name: str,
) -> tuple[SharedDirectory, ...]:
    """Return the directories one agent owns or joined, skipping any whose owner can no longer share."""
    if not is_shareable_agent(config, agent_name):
        return ()
    return tuple(
        directory
        for directory in load_shared_directories(runtime_paths)
        if is_shareable_agent(config, directory.owner) and agent_name in {directory.owner, *directory.members}
    )


def _existing(directories: dict[str, SharedDirectory], owner: str, name: str) -> SharedDirectory:
    directory = directories.get(f"{owner}/{name}")
    if directory is None:
        msg = f"{owner} has no shared directory named '{name}'."
        raise SharedDirectoryError(msg)
    return directory


def create_shared_directory(runtime_paths: RuntimePaths, *, owner: str, name: str) -> SharedDirectory:
    """Create one directory below the storage root and record it with no members."""
    if not _NAME_PATTERN.fullmatch(name):
        msg = "Shared directory names use 1-64 lowercase letters, digits, '-' or '_', starting with a letter or digit."
        raise SharedDirectoryError(msg)
    with _ledger_lock:
        directories = _read_ledger(runtime_paths, for_update=True)
        key = f"{owner}/{name}"
        if key in directories:
            msg = f"{owner} already has a shared directory named '{name}'."
            raise SharedDirectoryError(msg)
        runtime_paths.storage_root.mkdir(parents=True, exist_ok=True)
        try:
            with open_directory_within_root(
                runtime_paths.storage_root,
                shared_directory_relative_path(owner, name),
                create=True,
            ):
                pass
        except OSError as exc:
            msg = f"The shared directory '{name}' could not be created: {exc.strerror or exc}"
            raise SharedDirectoryError(msg) from exc
        directory = SharedDirectory(owner=owner, name=name, members=())
        directories[key] = directory
        _write_ledger(runtime_paths, directories)
        return directory


def _set_members(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    members: Callable[[tuple[str, ...]], tuple[str, ...]],
) -> tuple[SharedDirectory, bool]:
    with _ledger_lock:
        directories = _read_ledger(runtime_paths, for_update=True)
        directory = _existing(directories, owner, name)
        updated = replace(directory, members=members(directory.members))
        if updated == directory:
            return directory, False
        directories[f"{owner}/{name}"] = updated
        _write_ledger(runtime_paths, directories)
        return updated, True


def add_shared_directory_member(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    member: str,
) -> tuple[SharedDirectory, bool]:
    """Add one member; return the directory and whether it changed."""
    return _set_members(
        runtime_paths,
        owner=owner,
        name=name,
        members=lambda members: tuple(sorted({*members, member})),
    )


def remove_shared_directory_member(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    member: str,
) -> tuple[SharedDirectory, bool]:
    """Remove one member; return the directory and whether it changed."""
    return _set_members(
        runtime_paths,
        owner=owner,
        name=name,
        members=lambda members: tuple(existing for existing in members if existing != member),
    )


def _empty_directory(directory_fd: int) -> None:
    with os.scandir(directory_fd) as entries:
        names = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
    for name, is_directory in names:
        if is_directory:
            remove_directory_tree_at(directory_fd, name)
        else:
            os.unlink(name, dir_fd=directory_fd)


def delete_shared_directory(runtime_paths: RuntimePaths, *, owner: str, name: str) -> SharedDirectory:
    """Remove every file in one directory without following links, then forget it.

    The directory itself stays, empty and unmounted, because running workers may still
    mount it; a directory created again under the same name reuses it, so those mounts
    keep reaching the current files instead of a deleted directory.
    """
    with _ledger_lock:
        directories = _read_ledger(runtime_paths, for_update=True)
        directory = _existing(directories, owner, name)
        try:
            with open_directory_within_root(
                runtime_paths.storage_root,
                shared_directory_relative_path(owner, name),
            ) as directory_fd:
                _empty_directory(directory_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            msg = f"The shared directory '{name}' could not be deleted: {exc.strerror or exc}"
            raise SharedDirectoryError(msg) from exc
        del directories[f"{owner}/{name}"]
        _write_ledger(runtime_paths, directories)
        return directory
