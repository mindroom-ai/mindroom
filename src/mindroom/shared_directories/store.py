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
    from collections.abc import Callable, Iterable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_LEDGER_FILENAME = "shared_directories.json"
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ledger_lock = threading.Lock()
_MAX_PENDING_INVITATIONS_PER_AGENT = 10
logger = get_logger(__name__)


class SharedDirectoryError(ValueError):
    """A shared directory request that cannot be applied; the message is shown to the model."""


@dataclass(frozen=True, slots=True)
class PendingInvitation:
    """An invitation to an agent that waits for a user who may use that agent to accept it."""

    agent: str
    requester: str
    requested_at: str


@dataclass(frozen=True, slots=True)
class SharedDirectory:
    """One directory an agent owns, the agents that may read it, and the invitations not yet accepted."""

    owner: str
    name: str
    members: tuple[str, ...]
    # Recorded apart from members, which grant access, so a reader that knows only members ignores them.
    pending: tuple[PendingInvitation, ...] = ()

    def pending_for(self, agent_name: str) -> PendingInvitation | None:
        """Return the invitation waiting for one agent, if any."""
        return next((invitation for invitation in self.pending if invitation.agent == agent_name), None)


def shared_directory_relative_path(owner: str, name: str) -> Path:
    """Return a directory's location relative to the storage root."""
    return Path(SHARED_DIRECTORIES_DIRNAME, owner, name)


def _ledger_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _LEDGER_FILENAME


def _parse_pending(raw: object, members: tuple[str, ...]) -> tuple[PendingInvitation, ...]:
    invitations: dict[str, PendingInvitation] = {}
    for entry in cast("list[object]", raw) if isinstance(raw, list) else ():
        if not isinstance(entry, dict):
            continue
        fields = [cast("dict[str, object]", entry).get(field) for field in ("agent", "requester", "requested_at")]
        if all(isinstance(field, str) for field in fields) and fields[0] not in members:
            agent, requester, requested_at = cast("list[str]", fields)
            invitations.setdefault(
                agent,
                PendingInvitation(agent=agent, requester=requester, requested_at=requested_at),
            )
    return tuple(invitations[agent] for agent in sorted(invitations))


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
    member_tuple = tuple(sorted(set(cast("list[str]", member_names))))
    return SharedDirectory(
        owner=owner,
        name=name,
        members=member_tuple,
        pending=_parse_pending(record.get("pending"), member_tuple),
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
            "pending": [
                {"agent": invitation.agent, "requester": invitation.requester, "requested_at": invitation.requested_at}
                for invitation in directory.pending
            ],
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


def shared_directory_access(
    directory: SharedDirectory,
    agent_names: Iterable[str],
    is_shareable: Callable[[str], bool],
) -> bool | None:
    """Return ``True`` when the agents own a directory, ``False`` when one only joined it, ``None`` without access.

    Agents that cannot share, such as private ones, neither own nor join a directory.
    """
    if not is_shareable(directory.owner):
        return None
    agents = {agent_name for agent_name in agent_names if is_shareable(agent_name)}
    if directory.owner in agents:
        return True
    if agents.intersection(directory.members):
        return False
    return None


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


def _update_directory(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    update: Callable[[SharedDirectory], SharedDirectory],
) -> tuple[SharedDirectory, SharedDirectory]:
    """Apply one change to a directory's record and return the record before and after it."""
    with _ledger_lock:
        directories = _read_ledger(runtime_paths, for_update=True)
        directory = _existing(directories, owner, name)
        updated = update(directory)
        if updated != directory:
            directories[f"{owner}/{name}"] = updated
            _write_ledger(runtime_paths, directories)
        return directory, updated


def _without(directory: SharedDirectory, agent_name: str) -> SharedDirectory:
    """Return the directory with one agent neither a member nor invited."""
    return replace(
        directory,
        members=tuple(member for member in directory.members if member != agent_name),
        pending=tuple(invitation for invitation in directory.pending if invitation.agent != agent_name),
    )


def _with_member(directory: SharedDirectory, agent_name: str) -> SharedDirectory:
    without = _without(directory, agent_name)
    return replace(without, members=tuple(sorted({*without.members, agent_name})))


def add_shared_directory_member(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    member: str,
) -> tuple[SharedDirectory, bool]:
    """Add one member, replacing any invitation waiting for it; return the directory and whether it changed."""
    before, after = _update_directory(
        runtime_paths,
        owner=owner,
        name=name,
        update=lambda directory: _with_member(directory, member),
    )
    return after, after != before


def remove_shared_directory_member(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    member: str,
) -> tuple[SharedDirectory, SharedDirectory]:
    """Remove one member or cancel the invitation waiting for it; return the directory before and after."""
    return _update_directory(
        runtime_paths,
        owner=owner,
        name=name,
        update=lambda directory: _without(directory, member),
    )


def invite_pending_shared_directory_member(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    invitation: PendingInvitation,
) -> tuple[SharedDirectory, SharedDirectory]:
    """Record an invitation unless the agent is already a member or invited; return the directory before and after.

    Refuse it when the agent already holds the most pending invitations, which keeps the agent's prompt short.
    """
    agent_name = invitation.agent
    with _ledger_lock:
        directories = _read_ledger(runtime_paths, for_update=True)
        directory = _existing(directories, owner, name)
        if agent_name in directory.members or directory.pending_for(agent_name) is not None:
            return directory, directory
        held = sum(1 for existing in directories.values() if existing.pending_for(agent_name) is not None)
        if held >= _MAX_PENDING_INVITATIONS_PER_AGENT:
            msg = (
                f"{agent_name} already has {_MAX_PENDING_INVITATIONS_PER_AGENT} pending invitations, so it cannot be "
                "invited until it accepts or declines one, or an owner cancels one."
            )
            raise SharedDirectoryError(msg)
        pending = sorted((*directory.pending, invitation), key=lambda existing: existing.agent)
        updated = replace(directory, pending=tuple(pending))
        directories[f"{owner}/{name}"] = updated
        _write_ledger(runtime_paths, directories)
        return directory, updated


def _answer_invitation(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    agent_name: str,
    answer: Callable[[SharedDirectory, str], SharedDirectory],
) -> SharedDirectory:
    def update(directory: SharedDirectory) -> SharedDirectory:
        if directory.pending_for(agent_name) is None:
            msg = f"{agent_name} has no pending invitation to {owner}'s shared directory '{name}'."
            raise SharedDirectoryError(msg)
        return answer(directory, agent_name)

    return _update_directory(runtime_paths, owner=owner, name=name, update=update)[1]


def accept_shared_directory_invitation(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    agent_name: str,
) -> SharedDirectory:
    """Make an invited agent a member; refuse when no invitation waits for it."""
    return _answer_invitation(runtime_paths, owner=owner, name=name, agent_name=agent_name, answer=_with_member)


def decline_shared_directory_invitation(
    runtime_paths: RuntimePaths,
    *,
    owner: str,
    name: str,
    agent_name: str,
) -> SharedDirectory:
    """Drop the invitation waiting for one agent; refuse when there is none."""
    return _answer_invitation(runtime_paths, owner=owner, name=name, agent_name=agent_name, answer=_without)


def pending_shared_directory_invitations(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
) -> tuple[tuple[SharedDirectory, PendingInvitation], ...]:
    """Return the invitations waiting for one agent from owners that may still share."""
    if not is_shareable_agent(config, agent_name):
        return ()
    return tuple(
        (directory, invitation)
        for directory in load_shared_directories(runtime_paths)
        if is_shareable_agent(config, directory.owner) and (invitation := directory.pending_for(agent_name))
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
