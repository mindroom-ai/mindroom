"""Shared helpers for persisted invited-room membership state."""

from __future__ import annotations

import json
import threading
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from mindroom.constants import ROUTER_AGENT_NAME, primary_records_dir, safe_replace
from mindroom.logging_config import get_logger
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.tool_system.worker_routing import agent_state_root_path

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.access import InviteAcceptancePolicy
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# These files are the only record of their state. Rewrites run in worker threads and in plugin code, so one lock keeps
# two read-modify-writes from replacing each other's fresh read.
_RECORDS_LOCK = threading.Lock()
# Invited rooms are read on the event loop for every approval and call-room check, so a read costs one stat while the
# file is unchanged: the store's own writes publish what they saved, and keying by the file's (mtime, size) makes a
# write from anywhere else seen at the next read.
_INVITED_ROOMS_BY_PATH: dict[Path, tuple[tuple[int, int], frozenset[str]]] = {}
# Each agent's records directory by storage root, resolved once because resolving it takes several path lookups.
_RECORDS_DIRECTORIES: dict[tuple[Path, str], Path] = {}


def invited_rooms_path(runtime_paths: RuntimePaths, agent_name: str) -> Path:
    """Return the primary-only storage path for one agent's persisted invited rooms."""
    return _records_dir(runtime_paths, agent_name) / "invited_rooms.json"


def pending_room_invites_path(runtime_paths: RuntimePaths, agent_name: str) -> Path:
    """Return the primary-only storage path for one agent's outstanding room invites."""
    return _records_dir(runtime_paths, agent_name) / "pending_room_invites.json"


def _records_dir(runtime_paths: RuntimePaths, agent_name: str) -> Path:
    key = (runtime_paths.storage_root, agent_name)
    if (directory := _RECORDS_DIRECTORIES.get(key)) is None:
        state_root = agent_state_root_path(runtime_paths.storage_root, agent_name)
        directory = _RECORDS_DIRECTORIES[key] = primary_records_dir(state_root, runtime_paths)
    return directory


def load_invited_rooms(path: Path) -> set[str]:
    """Load persisted room ownership, rejecting unreadable or invalid state."""
    if not path.exists():
        return set()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("failed_to_load_invited_rooms", path=str(path), exc_info=True)
        raise

    if not isinstance(raw, list) or any(not isinstance(room_id, str) for room_id in raw):
        msg = f"Invalid invited-room retention file: {path}"
        raise ValueError(msg)

    return set(cast("list[str]", raw))


def _save_invited_rooms(path: Path, room_ids: set[str]) -> bool:
    """Replace one entity's invited rooms atomically; read-modify-writes go through `remember_invited_room` and `forget_invited_room`."""
    return _save_json(path, sorted(room_ids))


def _update_invited_rooms(path: Path, change: Callable[[set[str]], bool], failure_message: str) -> None:
    """Apply one change to fresh durable state under the records lock, raising when its write fails."""
    with _RECORDS_LOCK:
        room_ids = load_invited_rooms(path)
        if not change(room_ids):
            return
        if not _save_invited_rooms(path, room_ids):
            raise OSError(failure_message)
        stat = path.stat()
        _INVITED_ROOMS_BY_PATH[path] = ((stat.st_mtime_ns, stat.st_size), frozenset(room_ids))


def read_invited_rooms(runtime_paths: RuntimePaths, config: Config, agent_name: str) -> frozenset[str]:
    """Return the rooms one entity keeps across restarts because it accepted their invites."""
    if not should_persist_invited_rooms(config, agent_name):
        return frozenset()
    path = invited_rooms_path(runtime_paths, agent_name)
    try:
        stat = path.stat()
    except FileNotFoundError:
        _INVITED_ROOMS_BY_PATH.pop(path, None)
        return frozenset()
    version = (stat.st_mtime_ns, stat.st_size)
    cached = _INVITED_ROOMS_BY_PATH.get(path)
    if cached is not None and cached[0] == version:
        return cached[1]
    room_ids = frozenset(load_invited_rooms(path))
    _INVITED_ROOMS_BY_PATH[path] = (version, room_ids)
    return room_ids


def load_pending_room_invites(path: Path) -> dict[str, str]:
    """Load outstanding room IDs and their inviters from durable state."""
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("failed_to_load_pending_room_invites", path=str(path), exc_info=True)
        return {}

    if not isinstance(raw, dict) or any(
        not isinstance(room_id, str) or not isinstance(sender_id, str) for room_id, sender_id in raw.items()
    ):
        logger.warning("invalid_pending_room_invites_file", path=str(path))
        return {}

    return raw


def _save_pending_room_invites(path: Path, pending_invites: dict[str, str]) -> bool:
    """Atomically replace one agent's outstanding room invites, keeping them oldest first."""
    return _save_json(path, pending_invites)


def update_pending_room_invites(
    path: Path,
    change: Callable[[dict[str, str]], bool],
    failure_message: str,
) -> dict[str, str]:
    """Apply one change to fresh durable state under the records lock, returning the state it leaves."""
    with _RECORDS_LOCK:
        pending_invites = load_pending_room_invites(path)
        if change(pending_invites) and not _save_pending_room_invites(path, pending_invites):
            raise OSError(failure_message)
        return pending_invites


def _save_json(path: Path, value: object) -> bool:
    """Atomically replace one JSON state file."""
    temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            f"{json.dumps(value, ensure_ascii=True, indent=2)}\n",
            encoding="utf-8",
        )
        safe_replace(temp_path, path)
    except OSError:
        logger.exception("failed_to_save_invited_rooms", path=str(path))
        return False
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def remember_invited_room(path: Path, room_id: str) -> None:
    """Keep one room across restarts."""

    def remember(room_ids: set[str]) -> bool:
        if room_id in room_ids:
            return False
        room_ids.add(room_id)
        return True

    _update_invited_rooms(path, remember, f"Failed to retain invited room {room_id}")


def forget_invited_room(path: Path, room_id: str) -> None:
    """Stop keeping one room across restarts."""

    def forget(room_ids: set[str]) -> bool:
        if room_id not in room_ids:
            return False
        room_ids.discard(room_id)
        return True

    _update_invited_rooms(path, forget, f"Failed to forget invited room {room_id}")


def _invite_acceptance_policy(config: Config, agent_name: str) -> InviteAcceptancePolicy | None:
    if agent_name == ROUTER_AGENT_NAME:
        return config.router.accept_invites

    agent_config = config.agents.get(agent_name)
    if agent_config is not None:
        return agent_config.accept_invites

    team_config = config.teams.get(agent_name)
    if team_config is not None:
        return team_config.accept_invites

    return None


def is_inviter_allowed(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    sender_id: str,
) -> bool:
    """Apply one configured entity's dedicated inbound invitation policy."""
    policy = _invite_acceptance_policy(config, agent_name)
    if isinstance(policy, bool):
        return policy
    if policy is None:
        return False
    canonical_sender = resolve_human_requester_alias(sender_id, config, runtime_paths)
    return any(fnmatchcase(canonical_sender, pattern) for pattern in policy)


def should_accept_invites(config: Config, agent_name: str) -> bool:
    """Return whether one configured entity has any enabled invitation policy."""
    policy = _invite_acceptance_policy(config, agent_name)
    return bool(policy)


def invited_room_entity_names(config: Config) -> tuple[str, ...]:
    """Return configured entity names that may own persisted invited rooms."""
    return (ROUTER_AGENT_NAME, *config.agents.keys(), *config.teams.keys())


def should_persist_invited_rooms(config: Config, agent_name: str) -> bool:
    """Return whether one entity should keep accepted invited rooms across restarts."""
    return should_accept_invites(config, agent_name)
