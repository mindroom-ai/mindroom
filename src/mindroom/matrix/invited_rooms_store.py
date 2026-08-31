"""Shared helpers for persisted invited-room membership state."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from mindroom.constants import ROUTER_AGENT_NAME, safe_replace
from mindroom.logging_config import get_logger
from mindroom.tool_system.worker_routing import agent_state_root_path

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config

logger = get_logger(__name__)

_LEGACY_PENDING_ROOM_INVITES_FILENAME = "pending_room_invites.json"


class PendingRoomInvitePhase(StrEnum):
    """Durable progress of one accepted-or-pending invite transaction."""

    OBSERVED = "observed"
    AUTHORIZED = "authorized"
    LEAVING = "leaving"


@dataclass(frozen=True, slots=True)
class PendingRoomInvite:
    """Durable inviter identity and authorization progress for one invite."""

    inviter_id: str
    phase: PendingRoomInvitePhase


@dataclass(frozen=True, slots=True)
class RoomInviteState:
    """Accepted membership and optional unfinished work for one invited room."""

    accepted: bool = False
    pending: PendingRoomInvite | None = None


def invited_rooms_path(storage_root: Path, agent_name: str) -> Path:
    """Return the storage path for one agent's persisted invited rooms."""
    return agent_state_root_path(storage_root, agent_name) / "invited_rooms.json"


def load_invited_rooms(path: Path) -> set[str]:
    """Load the accepted-room projection of one invited-room ledger."""
    return {room_id for room_id, state in load_room_invite_states(path).items() if state.accepted}


def load_room_invite_states(path: Path) -> dict[str, RoomInviteState]:
    """Load one invited-room ledger, failing closed on invalid content."""
    if not path.exists():
        legacy_pending_path = path.with_name(_LEGACY_PENDING_ROOM_INVITES_FILENAME)
        if not legacy_pending_path.exists():
            return {}
        return _migrate_legacy_room_invite_states(path, [], legacy_pending_path)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("failed_to_load_invited_rooms", path=str(path), exc_info=True)
        return {}

    if isinstance(raw, list):
        return _migrate_legacy_room_invite_states(
            path,
            raw,
            path.with_name(_LEGACY_PENDING_ROOM_INVITES_FILENAME),
        )

    if not isinstance(raw, dict):
        logger.warning("invalid_invited_rooms_file", path=str(path))
        return {}

    return _parse_room_invite_states(path, raw)


def _parse_room_invite_states(path: Path, raw: dict[object, object]) -> dict[str, RoomInviteState]:
    """Parse the current strict ledger shape."""
    states: dict[str, RoomInviteState] = {}
    for room_id, value in raw.items():
        state = _parse_room_invite_state(value)
        if not isinstance(room_id, str) or state is None:
            break
        states[room_id] = state
    else:
        return states

    logger.warning("invalid_invited_rooms_file", path=str(path))
    return {}


def _migrate_legacy_room_invite_states(
    path: Path,
    accepted_values: list[object],
    pending_path: Path,
) -> dict[str, RoomInviteState]:
    """Combine the two legacy invite files without trusting pending authorization."""
    if any(not isinstance(room_id, str) for room_id in accepted_values):
        logger.warning("invalid_invited_rooms_file", path=str(path))
        return {}

    states = {room_id: RoomInviteState(accepted=True) for room_id in cast("list[str]", accepted_values)}
    pending_invites = _load_legacy_pending_room_invites(pending_path)
    if pending_invites is None:
        return states
    for room_id, inviter_id in pending_invites.items():
        prior = states.get(room_id, RoomInviteState())
        states[room_id] = RoomInviteState(
            accepted=prior.accepted,
            pending=PendingRoomInvite(
                inviter_id=inviter_id,
                phase=PendingRoomInvitePhase.OBSERVED,
            ),
        )

    save_room_invite_states(path, states)
    return states


def _load_legacy_pending_room_invites(path: Path) -> dict[str, str] | None:
    """Load the strict legacy pending-invite map for one-time migration."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("failed_to_load_pending_room_invites", path=str(path), exc_info=True)
        return None
    if not isinstance(raw, dict) or any(
        not isinstance(room_id, str) or not isinstance(inviter_id, str) for room_id, inviter_id in raw.items()
    ):
        logger.warning("invalid_pending_room_invites_file", path=str(path))
        return None
    return cast("dict[str, str]", raw)


def _parse_room_invite_state(value: object) -> RoomInviteState | None:
    """Parse one strict ledger value without accepting partial state."""
    if not isinstance(value, dict) or set(value) != {"accepted", "pending"}:
        return None
    state_value = cast("dict[str, object]", value)
    accepted = state_value["accepted"]
    if not isinstance(accepted, bool):
        return None
    pending_is_valid, pending = _parse_pending_room_invite(state_value["pending"])
    if not pending_is_valid:
        return None
    return RoomInviteState(accepted=accepted, pending=pending)


def _parse_pending_room_invite(value: object) -> tuple[bool, PendingRoomInvite | None]:
    """Parse the optional pending portion of one ledger value."""
    if value is None:
        return True, None
    if not isinstance(value, dict) or set(value) != {"inviter_id", "phase"}:
        return False, None
    pending_value = cast("dict[str, object]", value)
    inviter_id = pending_value["inviter_id"]
    phase_value = pending_value["phase"]
    if not isinstance(inviter_id, str) or not isinstance(phase_value, str):
        return False, None
    try:
        phase = PendingRoomInvitePhase(phase_value)
    except ValueError:
        return False, None
    return True, PendingRoomInvite(inviter_id=inviter_id, phase=phase)


def save_room_invite_states(path: Path, states: dict[str, RoomInviteState]) -> bool:
    """Atomically replace one agent's complete invited-room ledger."""
    return _save_json(
        path,
        {
            room_id: {
                "accepted": state.accepted,
                "pending": (
                    {
                        "inviter_id": state.pending.inviter_id,
                        "phase": state.pending.phase.value,
                    }
                    if state.pending is not None
                    else None
                ),
            }
            for room_id, state in sorted(states.items())
        },
    )


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
    states = load_room_invite_states(path)
    state = states.get(room_id, RoomInviteState())
    if state.accepted:
        return
    states[room_id] = RoomInviteState(accepted=True, pending=state.pending)
    save_room_invite_states(path, states)


def should_accept_invites(config: Config, agent_name: str) -> bool:
    """Return whether one configured entity accepts authorized room invites."""
    if agent_name == ROUTER_AGENT_NAME:
        return config.router.accept_invites

    agent_config = config.agents.get(agent_name)
    if agent_config is not None:
        return agent_config.accept_invites

    return agent_name in config.teams


def invited_room_entity_names(config: Config) -> tuple[str, ...]:
    """Return configured entity names that may own persisted invited rooms."""
    return (ROUTER_AGENT_NAME, *config.agents.keys(), *config.teams.keys())


def should_persist_invited_rooms(config: Config, agent_name: str) -> bool:
    """Return whether one entity should keep accepted invited rooms across restarts."""
    return should_accept_invites(config, agent_name)
