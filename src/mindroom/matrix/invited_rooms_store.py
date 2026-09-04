"""Shared helpers for persisted invited-room membership state."""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.constants import ROUTER_AGENT_NAME, safe_replace
from mindroom.logging_config import get_logger
from mindroom.requester_identity import resolve_human_requester_alias
from mindroom.tool_system.worker_routing import agent_state_root_path

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.access import InviteAcceptancePolicy
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


class _InvalidInvitedRoomsFileError(ValueError):
    """Persisted invited-room state has an invalid shape."""


def invited_rooms_path(storage_root: Path, agent_name: str) -> Path:
    """Return the storage path for one agent's persisted invited rooms."""
    return agent_state_root_path(storage_root, agent_name) / "invited_rooms.json"


def pending_room_invites_path(storage_root: Path, agent_name: str) -> Path:
    """Return the storage path for one agent's outstanding room invites."""
    return agent_state_root_path(storage_root, agent_name) / "pending_room_invites.json"


def load_invited_rooms(path: Path) -> set[str]:
    """Load persisted invited rooms, failing open on missing or invalid files."""
    if not path.exists():
        return set()

    try:
        return _read_invited_rooms(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("failed_to_load_invited_rooms", path=str(path), exc_info=True)
        return set()
    except _InvalidInvitedRoomsFileError:
        logger.warning("invalid_invited_rooms_file", path=str(path))
        return set()


def load_invited_room_claims(path: Path) -> set[str]:
    """Load ownership evidence, failing closed when persisted state is invalid."""
    try:
        return _read_invited_rooms(path)
    except FileNotFoundError:
        return set()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, _InvalidInvitedRoomsFileError) as exc:
        msg = f"Invalid invited-room claim file: {path}"
        raise RuntimeError(msg) from exc


def _read_invited_rooms(path: Path) -> set[str]:
    """Read and validate one persisted invited-room state file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        msg = f"Invited-room state must be a list of room IDs: {path}"
        raise _InvalidInvitedRoomsFileError(msg)
    room_ids = [room_id for room_id in raw if isinstance(room_id, str)]
    if len(room_ids) != len(raw):
        msg = f"Invited-room state must be a list of room IDs: {path}"
        raise _InvalidInvitedRoomsFileError(msg)
    return set(room_ids)


def save_invited_rooms(path: Path, room_ids: set[str]) -> bool:
    """Replace invited rooms atomically for one eligible entity.

    Callers replacing a cached set must first merge fresh durable state so a
    stale in-memory snapshot cannot discard another runtime component's write.
    """
    return _save_json(path, sorted(room_ids))


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


def save_pending_room_invites(path: Path, pending_invites: dict[str, str]) -> bool:
    """Atomically replace one agent's outstanding room invites."""
    return _save_json(path, dict(sorted(pending_invites.items())))


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
    """Add one room using fresh durable state."""
    room_ids = load_invited_rooms(path)
    if room_id in room_ids:
        return
    room_ids.add(room_id)
    save_invited_rooms(path, room_ids)


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
    return bool(policy) if policy is not None else False


def invited_room_entity_names(config: Config) -> tuple[str, ...]:
    """Return configured entity names that may own persisted invited rooms."""
    return (ROUTER_AGENT_NAME, *config.agents.keys(), *config.teams.keys())


def should_persist_invited_rooms(config: Config, agent_name: str) -> bool:
    """Return whether one entity should keep accepted invited rooms across restarts."""
    return should_accept_invites(config, agent_name)
