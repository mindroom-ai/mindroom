"""A room's reply mode override: the `reply_mode` room setting, set by `!reply_mode`, plugin routes, or a client.

The setting is the room's `io.mindroom.room_settings` state with state key `reply_mode` and content `{"value": mode}`;
`{}` or no event leaves it unset. The runtime keeps the newest value it read, wrote, or saw arrive in sync per room, so
checks that cannot wait for the homeserver use it, and reads the state again when its copy is missing, old, or a change
arrived in sync.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import TYPE_CHECKING, NamedTuple, cast, get_args

from mindroom.config.agent import RoomReplyMode
from mindroom.constants import tracking_dir
from mindroom.durable_write import load_cached_override_records, write_override_records
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    import nio

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

ROOM_REPLY_MODES: tuple[RoomReplyMode, ...] = get_args(RoomReplyMode)
_SETTING_KEY = "reply_mode"
# A change made while no bot's sync timeline showed it, such as during a sync gap, applies after this long.
_REREAD_AFTER_SECONDS = 600.0
# A turn waits for the read and a command for the write, so a stalled homeserver costs them a few seconds at most.
_REQUEST_TIMEOUT_SECONDS = 5.0
# Runtimes before the room setting stored overrides in this file; each moves into its room's state when first read.
_LEGACY_FILENAME = "room_reply_modes.json"
_STALE = float("-inf")


class _Copy(NamedTuple):
    """What the runtime knows of one room's setting.

    ``mode`` is the newest value it knows: from the newest live event by server time, or from its own read or write.
    ``read_at`` is when a read or write that no other change overlapped confirmed it, ``_STALE`` otherwise, so the next
    refresh reads the room. ``event_ts`` is the server time of the newest live event taken, to drop older copies.
    """

    mode: RoomReplyMode | None
    read_at: float
    event_ts: int | None


_known: dict[tuple[Path, str], _Copy] = {}
_changes: defaultdict[tuple[Path, str], int] = defaultdict(int)


def _key(runtime_paths: RuntimePaths, room_id: str) -> tuple[Path, str]:
    return (runtime_paths.storage_root, room_id)


def _mode_from_content(content: object) -> RoomReplyMode | None:
    value = content.get("value") if isinstance(content, dict) else None
    return cast("RoomReplyMode", value) if value in ROOM_REPLY_MODES else None


def resolve_room_reply_mode_override(runtime_paths: RuntimePaths, room_id: str) -> RoomReplyMode | None:
    """Return the room's reply mode setting as the runtime last knew it, None while unset or never read."""
    known = _known.get(_key(runtime_paths, room_id))
    return None if known is None else known.mode


def observe_room_settings_event(runtime_paths: RuntimePaths, room_id: str, source: Mapping[str, object]) -> None:
    """Take a live `reply_mode` event's value until the next refresh reads the room; ignore any other event.

    Each bot's sync delivers the event on its own schedule, so a copy no newer than one already taken changes nothing,
    and only a read of the room's state makes the value fresh again.
    """
    if source.get("type") != ROOM_SETTINGS_EVENT_TYPE or source.get("state_key") != _SETTING_KEY:
        return
    key = _key(runtime_paths, room_id)
    known = _known.get(key)
    ts = source.get("origin_server_ts")
    ts = ts if isinstance(ts, int) else None
    if known is not None and known.event_ts is not None and (ts is None or ts <= known.event_ts):
        return
    _changes[key] += 1
    _known[key] = _Copy(_mode_from_content(source.get("content")), _STALE, ts)


def _confirm(key: tuple[Path, str], mode: RoomReplyMode | None, changes: int, *, wrote: bool) -> bool:
    """Keep what a read or write found, fresh only if no change or write came in while it was in flight.

    When one did, which of the two is newer is unknown: the copy stays as it is, stale, and the next refresh reads.
    A write counts as a change for reads still in flight; reads of the same state do not unsettle each other.
    Returns whether the copy is fresh.
    """
    known = _known.get(key)
    if _changes[key] != changes:
        _known[key] = _Copy(mode, _STALE, None) if known is None else known._replace(read_at=_STALE)
        return False
    if wrote:
        _changes[key] += 1
    _known[key] = _Copy(mode, time.monotonic(), None if known is None else known.event_ts)
    return True


async def refresh_room_reply_mode(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
    *,
    force: bool = False,
) -> bool:
    """Read the room's reply mode setting unless the copy is fresh; return whether the copy is confirmed current.

    ``force`` reads it whatever the copy's age. A failed read keeps the copy.
    """
    import nio  # noqa: PLC0415 - config reads the copy above without loading the Matrix client

    key = _key(runtime_paths, room_id)
    known = _known.get(key)
    if not force and known is not None and time.monotonic() - known.read_at < _REREAD_AFTER_SECONDS:
        return True
    changes = _changes[key]
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
            response = await client.room_get_state_event(room_id, ROOM_SETTINGS_EVENT_TYPE, _SETTING_KEY)
    except Exception:
        logger.warning("room_reply_mode_read_failed", room_id=room_id, exc_info=True)
        return False
    if isinstance(response, nio.RoomGetStateEventResponse):
        mode = _mode_from_content(response.content)
    elif isinstance(response, nio.RoomGetStateEventError) and response.status_code == "M_NOT_FOUND":
        # A room without the setting takes the override an older runtime stored on disk, which then leaves the file.
        # While the room refuses the write, the stored override still applies and the next read tries again.
        mode = _legacy_override(runtime_paths, room_id)
        if mode is not None and await write_room_reply_mode(client, runtime_paths, room_id, mode):
            _drop_legacy_override(runtime_paths, room_id)
            return _known[key].read_at != _STALE
    else:
        logger.warning("room_reply_mode_read_failed", room_id=room_id, error=str(response))
        return False
    return _confirm(key, mode, changes, wrote=False)


async def write_room_reply_mode(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
    mode: RoomReplyMode | None,
) -> bool:
    """Set the room's reply mode setting, or clear it given None, as ``client``; return whether the room took it."""
    import nio  # noqa: PLC0415 - config reads the copy above without loading the Matrix client

    key = _key(runtime_paths, room_id)
    changes = _changes[key]
    content = {} if mode is None else {"value": mode}
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
            response = await client.room_put_state(room_id, ROOM_SETTINGS_EVENT_TYPE, content, state_key=_SETTING_KEY)
    except Exception:
        logger.warning("room_reply_mode_write_failed", room_id=room_id, exc_info=True)
        return False
    if not isinstance(response, nio.RoomPutStateResponse):
        logger.warning("room_reply_mode_write_failed", room_id=room_id, error=str(response))
        return False
    _confirm(key, mode, changes, wrote=True)
    return True


def _legacy_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _LEGACY_FILENAME


def _is_legacy_record(_room_id: str, record: dict[object, object]) -> bool:
    return record.get("mode") in ROOM_REPLY_MODES


def _legacy_override(runtime_paths: RuntimePaths, room_id: str) -> RoomReplyMode | None:
    record = load_cached_override_records(_legacy_path(runtime_paths), _is_legacy_record).get(room_id)
    return None if record is None else cast("RoomReplyMode", record["mode"])


def _drop_legacy_override(runtime_paths: RuntimePaths, room_id: str) -> None:
    # Read the file now, not before the write: another room's move may have rewritten it meanwhile.
    path = _legacy_path(runtime_paths)
    overrides = load_cached_override_records(path, _is_legacy_record)
    if overrides.pop(room_id, None) is not None:
        write_override_records(path, overrides)
