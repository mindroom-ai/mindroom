"""The primary-owned ledger of shared directories and their storage below the storage root.

The ledger lives below ``tracking/``, which no worker mounts, so worker code can
never forge membership. Each directory lives at
``shared_directories/<owner>/<name>/``; no worker mounts the two levels above it,
so worker code cannot replace a directory another worker mounts with a link.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.constants import tracking_dir
from mindroom.durable_write import write_json_file_durable
from mindroom.path_confinement import open_directory_within_root
from mindroom.workers.worker_retirement import remove_directory_tree_at

if TYPE_CHECKING:
    from datetime import datetime

    from mindroom.constants import RuntimePaths

SHARED_DIRECTORIES_DIRNAME = "shared_directories"
_LEDGER_FILENAME = "shared_directories.json"
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
_ledger_lock = threading.Lock()


class SharedDirectoryError(ValueError):
    """A shared directory request that cannot be applied; the message is shown to the model."""


@dataclass(frozen=True, slots=True)
class SharedDirectory:
    """One directory an agent owns and the agents that may read it."""

    owner: str
    name: str
    members: tuple[str, ...]
    created_at: str


def shared_directory_relative_path(owner: str, name: str) -> Path:
    """Return a directory's location relative to the storage root."""
    return Path(SHARED_DIRECTORIES_DIRNAME, owner, name)


def _ledger_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _LEDGER_FILENAME


def _parse_record(key: str, raw: object) -> SharedDirectory | None:
    if not isinstance(raw, dict):
        return None
    record = cast("dict[str, object]", raw)
    owner, name, members, created_at = (record.get(field) for field in ("owner", "name", "members", "created_at"))
    if (
        not isinstance(owner, str)
        or not isinstance(name, str)
        or not isinstance(created_at, str)
        or not isinstance(members, list)
        or not _AGENT_NAME_PATTERN.fullmatch(owner)
        or not _NAME_PATTERN.fullmatch(name)
        or key != f"{owner}/{name}"
    ):
        return None
    member_names = cast("list[object]", members)
    if not all(isinstance(member, str) and _AGENT_NAME_PATTERN.fullmatch(member) for member in member_names):
        return None
    return SharedDirectory(
        owner=owner,
        name=name,
        members=tuple(sorted(set(cast("list[str]", member_names)))),
        created_at=created_at,
    )


def _read_ledger(runtime_paths: RuntimePaths) -> dict[str, SharedDirectory]:
    try:
        data = json.loads(_ledger_path(runtime_paths).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
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
            "created_at": directory.created_at,
        }
        for key, directory in directories.items()
    }
    write_json_file_durable(_ledger_path(runtime_paths), payload, indent=2, sort_keys=True)


def load_shared_directories(runtime_paths: RuntimePaths) -> tuple[SharedDirectory, ...]:
    """Return every recorded shared directory, sorted by owner and name."""
    return tuple(sorted(_read_ledger(runtime_paths).values(), key=lambda directory: (directory.owner, directory.name)))


def _existing(directories: dict[str, SharedDirectory], owner: str, name: str) -> SharedDirectory:
    directory = directories.get(f"{owner}/{name}")
    if directory is None:
        msg = f"{owner} has no shared directory named '{name}'."
        raise SharedDirectoryError(msg)
    return directory


def create_shared_directory(runtime_paths: RuntimePaths, *, owner: str, name: str, now: datetime) -> SharedDirectory:
    """Create one directory below the storage root and record it with no members."""
    if not _NAME_PATTERN.fullmatch(name):
        msg = "Shared directory names use 1-64 lowercase letters, digits, '-' or '_', starting with a letter or digit."
        raise SharedDirectoryError(msg)
    with _ledger_lock:
        directories = _read_ledger(runtime_paths)
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
        directory = SharedDirectory(owner=owner, name=name, members=(), created_at=now.isoformat())
        directories[key] = directory
        _write_ledger(runtime_paths, directories)
        return directory


def add_shared_directory_member(runtime_paths: RuntimePaths, *, owner: str, name: str, member: str) -> bool:
    """Add one member; return ``False`` when it already was one."""
    with _ledger_lock:
        directories = _read_ledger(runtime_paths)
        directory = _existing(directories, owner, name)
        if member in directory.members:
            return False
        directories[f"{owner}/{name}"] = replace(directory, members=tuple(sorted((*directory.members, member))))
        _write_ledger(runtime_paths, directories)
        return True


def remove_shared_directory_member(runtime_paths: RuntimePaths, *, owner: str, name: str, member: str) -> bool:
    """Remove one member; return ``False`` when it was not one."""
    with _ledger_lock:
        directories = _read_ledger(runtime_paths)
        directory = _existing(directories, owner, name)
        if member not in directory.members:
            return False
        directories[f"{owner}/{name}"] = replace(
            directory,
            members=tuple(existing for existing in directory.members if existing != member),
        )
        _write_ledger(runtime_paths, directories)
        return True


def delete_shared_directory(runtime_paths: RuntimePaths, *, owner: str, name: str) -> SharedDirectory:
    """Forget one directory, then remove its tree without following links."""
    with _ledger_lock:
        directories = _read_ledger(runtime_paths)
        directory = _existing(directories, owner, name)
        del directories[f"{owner}/{name}"]
        _write_ledger(runtime_paths, directories)
        try:
            with open_directory_within_root(
                runtime_paths.storage_root,
                Path(SHARED_DIRECTORIES_DIRNAME, owner),
            ) as owner_fd:
                remove_directory_tree_at(owner_fd, name)
        except FileNotFoundError:
            pass
        return directory
