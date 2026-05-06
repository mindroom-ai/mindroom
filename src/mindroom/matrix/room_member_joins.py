"""Helpers for Matrix room-member join hook emission."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.constants import safe_replace
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import extract_agent_name

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RoomMemberJoin:
    """One live human member join that should be exposed to hooks."""

    room_id: str
    event_id: str
    user_id: str
    sender_id: str
    display_name: str | None
    avatar_url: str | None
    membership: str
    prev_membership: str | None
    first_join: bool


def _room_member_join_tracking_path(storage_root: Path) -> Path:
    """Return the durable path for room-member join de-duplication."""
    return storage_root / "tracking" / "room_member_joins.json"


def _load_room_member_joins(path: Path) -> dict[str, set[str]]:
    """Load seen room-member joins, failing open on missing or invalid files."""
    if not path.exists():
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("failed_to_load_room_member_joins", path=str(path), exc_info=True)
        return {}

    if not isinstance(raw, dict):
        logger.warning("invalid_room_member_joins_file", path=str(path))
        return {}

    seen: dict[str, set[str]] = {}
    for room_id, user_ids in raw.items():
        if not isinstance(room_id, str) or not isinstance(user_ids, list):
            logger.warning("invalid_room_member_joins_file", path=str(path))
            return {}
        room_user_ids: set[str] = set()
        for user_id in user_ids:
            if not isinstance(user_id, str):
                logger.warning("invalid_room_member_joins_file", path=str(path))
                return {}
            room_user_ids.add(user_id)
        seen[room_id] = room_user_ids
    return seen


def _save_room_member_joins(path: Path, seen: dict[str, set[str]]) -> None:
    """Persist seen room-member joins atomically."""
    temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    payload = {room_id: sorted(user_ids) for room_id, user_ids in sorted(seen.items())}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            f"{json.dumps(payload, ensure_ascii=True, indent=2)}\n",
            encoding="utf-8",
        )
        safe_replace(temp_path, path)
    except OSError:
        logger.exception("failed_to_save_room_member_joins", path=str(path))
    finally:
        temp_path.unlink(missing_ok=True)


def _mark_room_member_join_seen(storage_root: Path, *, room_id: str, user_id: str) -> bool:
    """Record one room/user pair and return whether it was first seen."""
    path = _room_member_join_tracking_path(storage_root)
    seen = _load_room_member_joins(path)
    room_user_ids = seen.setdefault(room_id, set())
    if user_id in room_user_ids:
        return False

    room_user_ids.add(user_id)
    _save_room_member_joins(path, seen)
    return True


def room_member_join_from_event(
    room: nio.MatrixRoom,
    event: nio.RoomMemberEvent,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    storage_root: Path,
) -> RoomMemberJoin | None:
    """Return hook payload data for one live human join event, or None when ignored."""
    if event.membership != "join" or event.prev_membership == "join":
        return None

    user_id = event.state_key
    if extract_agent_name(user_id, config, runtime_paths) is not None or user_id in config.bot_accounts:
        return None

    first_join = _mark_room_member_join_seen(storage_root, room_id=room.room_id, user_id=user_id)
    if not first_join:
        return None

    return RoomMemberJoin(
        room_id=room.room_id,
        event_id=event.event_id,
        user_id=user_id,
        sender_id=event.sender,
        display_name=_optional_string(event.content, "displayname"),
        avatar_url=_optional_string(event.content, "avatar_url"),
        membership=event.membership,
        prev_membership=event.prev_membership,
        first_join=first_join,
    )


def _optional_string(content: dict[str, object], key: str) -> str | None:
    value = content.get(key)
    return value if isinstance(value, str) else None
