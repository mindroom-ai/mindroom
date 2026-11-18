"""A room's reply mode override: the `reply_mode` room setting, set by `!reply_mode`, plugin routes, or a client.

The setting is the room's `io.mindroom.room_settings` state with state key `reply_mode` and content `{"value": mode}`;
`{}` or no event leaves it unset. The runtime keeps the value it last read or saw arrive in sync per room, so checks
that cannot wait for the homeserver use it, and reads the state again when its copy is missing or old.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import TYPE_CHECKING, cast, get_args

import nio

from mindroom.config.agent import RoomReplyMode
from mindroom.constants import tracking_dir
from mindroom.durable_write import load_cached_override_records, write_override_records
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

ROOM_REPLY_MODES: tuple[RoomReplyMode, ...] = get_args(RoomReplyMode)
_SETTING_KEY = "reply_mode"
# A change made while no bot's sync timeline showed it, such as during a sync gap, applies after this long.
_REREAD_AFTER_SECONDS = 600.0
# Runtimes before the room setting stored overrides in this file; each moves into its room's state when first read.
_LEGACY_FILENAME = "room_reply_modes.json"

_known: dict[tuple[Path, str], tuple[RoomReplyMode | None, float]] = {}
_changes: defaultdict[tuple[Path, str], int] = defaultdict(int)


def _key(runtime_paths: RuntimePaths, room_id: str) -> tuple[Path, str]:
    return (runtime_paths.storage_root, room_id)


def _mode_from_content(content: object) -> RoomReplyMode | None:
    value = content.get("value") if isinstance(content, dict) else None
    return cast("RoomReplyMode", value) if value in ROOM_REPLY_MODES else None


def _remember(runtime_paths: RuntimePaths, room_id: str, mode: RoomReplyMode | None) -> None:
    key = _key(runtime_paths, room_id)
    _changes[key] += 1
    _known[key] = (mode, time.monotonic())


def resolve_room_reply_mode_override(runtime_paths: RuntimePaths, room_id: str) -> RoomReplyMode | None:
    """Return the room's reply mode setting as the runtime last read or saw it, None while unset or never read."""
    known = _known.get(_key(runtime_paths, room_id))
    return None if known is None else known[0]


def observe_room_settings_event(runtime_paths: RuntimePaths, room_id: str, source: Mapping[str, object]) -> None:
    """Take the reply mode from a live `io.mindroom.room_settings` state event; ignore any other event."""
    if source.get("type") == ROOM_SETTINGS_EVENT_TYPE and source.get("state_key") == _SETTING_KEY:
        _remember(runtime_paths, room_id, _mode_from_content(source.get("content")))


async def refresh_room_reply_mode(client: nio.AsyncClient, runtime_paths: RuntimePaths, room_id: str) -> None:
    """Read the room's reply mode setting unless the runtime read or saw it recently; a failed read keeps the copy."""
    key = _key(runtime_paths, room_id)
    known = _known.get(key)
    if known is not None and time.monotonic() - known[1] < _REREAD_AFTER_SECONDS:
        return
    changes = _changes[key]
    try:
        response = await client.room_get_state_event(room_id, ROOM_SETTINGS_EVENT_TYPE, _SETTING_KEY)
    except Exception:
        logger.warning("room_reply_mode_read_failed", room_id=room_id, exc_info=True)
        return
    if isinstance(response, nio.RoomGetStateEventResponse):
        mode = _mode_from_content(response.content)
    elif isinstance(response, nio.RoomGetStateEventError) and response.status_code == "M_NOT_FOUND":
        mode = await _move_legacy_override(client, runtime_paths, room_id)
    else:
        logger.warning("room_reply_mode_read_failed", room_id=room_id, error=str(response))
        return
    # A value sync or a write brought meanwhile is newer than this read.
    if _changes[key] == changes:
        _known[key] = (mode, time.monotonic())


async def write_room_reply_mode(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
    mode: RoomReplyMode | None,
) -> bool:
    """Set the room's reply mode setting, or clear it given None, as ``client``; return whether the room took it."""
    content = {} if mode is None else {"value": mode}
    try:
        response = await client.room_put_state(room_id, ROOM_SETTINGS_EVENT_TYPE, content, state_key=_SETTING_KEY)
    except Exception:
        logger.warning("room_reply_mode_write_failed", room_id=room_id, exc_info=True)
        return False
    if not isinstance(response, nio.RoomPutStateResponse):
        logger.warning("room_reply_mode_write_failed", room_id=room_id, error=str(response))
        return False
    _remember(runtime_paths, room_id, mode)
    return True


async def _move_legacy_override(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
) -> RoomReplyMode | None:
    """Return the override an older runtime stored for a room without the setting, moving it into the room's state.

    While the room refuses the write, the stored override still applies and the next read tries again.
    """
    path = tracking_dir(runtime_paths) / _LEGACY_FILENAME
    overrides = load_cached_override_records(path, lambda _room_id, record: record.get("mode") in ROOM_REPLY_MODES)
    record = overrides.get(room_id)
    if record is None:
        return None
    mode = cast("RoomReplyMode", record["mode"])
    if await write_room_reply_mode(client, runtime_paths, room_id, mode):
        del overrides[room_id]
        write_override_records(path, overrides)
    return mode
