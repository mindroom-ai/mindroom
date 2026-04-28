"""Shared helpers for persisted invited-room membership state."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

from mindroom.constants import ROUTER_AGENT_NAME, safe_replace
from mindroom.logging_config import get_logger
from mindroom.tool_system.worker_routing import agent_state_root_path

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config

logger = get_logger(__name__)


def invited_rooms_path(storage_root: Path, agent_name: str) -> Path:
    """Return the storage path for one agent's persisted invited rooms."""
    return agent_state_root_path(storage_root, agent_name) / "invited_rooms.json"


def load_invited_rooms(path: Path) -> set[str]:
    """Load persisted invited rooms, failing open on missing or invalid files."""
    if not path.exists():
        return set()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("failed_to_load_invited_rooms", path=str(path), exc_info=True)
        return set()

    if not isinstance(raw, list):
        logger.warning("invalid_invited_rooms_file", path=str(path))
        return set()

    room_ids = [room_id for room_id in raw if isinstance(room_id, str)]
    if len(room_ids) != len(raw):
        logger.warning("invalid_invited_rooms_file", path=str(path))
        return set()

    return set(room_ids)


def save_invited_rooms(path: Path, room_ids: set[str]) -> None:
    """Persist invited rooms atomically for one eligible entity."""
    temp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(
            f"{json.dumps(sorted(room_ids), ensure_ascii=True, indent=2)}\n",
            encoding="utf-8",
        )
        safe_replace(temp_path, path)
    except OSError:
        logger.exception("failed_to_save_invited_rooms", path=str(path))
    finally:
        temp_path.unlink(missing_ok=True)


def should_persist_invited_rooms(config: Config, agent_name: str) -> bool:
    """Return whether one entity should keep invited rooms across restarts."""
    if agent_name == ROUTER_AGENT_NAME:
        return False

    agent_config = config.agents.get(agent_name)
    if agent_config is None:
        return False

    return agent_config.accept_invites
