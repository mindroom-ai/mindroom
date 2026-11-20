"""A room's reply mode override: the `reply_mode` room setting, set by `!reply_mode`, plugin routes, or a client.

The setting is the room's `io.mindroom.room_settings` state with state key `reply_mode` and content `{"value": mode}`;
`{}` or no event leaves it unset. The runtime keeps a copy per room, so checks that cannot wait for the homeserver use
it, and a turn reads the room unless a read confirmed the copy recently.

One rule keeps the copy: every read and write is numbered when it starts, every write's acknowledgement and every live
event when it arrives, and a result never replaces one numbered after it. So a read that started before a write took
effect, or before a change arrived in sync, changes nothing once that write or change is in. Only a read confirms the
copy. A live event applies at once but stays unconfirmed, since each bot's sync delivers it on its own schedule and it
may be a late copy of an older change; the bot that saw it reads the room again.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import TYPE_CHECKING, NamedTuple, cast, get_args

from mindroom.config.agent import RoomReplyMode
from mindroom.constants import tracking_dir
from mindroom.durable_write import load_cached_override_records, write_override_records
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping
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
_UNCONFIRMED = float("-inf")
_order = itertools.count(1)


class _Copy(NamedTuple):
    mode: RoomReplyMode | None
    # The number of the read, acknowledgement, or event it came from.
    order: int
    # When the read it came from returned, or _UNCONFIRMED.
    confirmed_at: float


class ReplyModeRead(NamedTuple):
    """What one refresh found: the room's setting, or with ``ok`` False the runtime's copy when the room was unread."""

    ok: bool
    mode: RoomReplyMode | None


_known: dict[tuple[Path, str], _Copy] = {}


def _key(runtime_paths: RuntimePaths, room_id: str) -> tuple[Path, str]:
    return (runtime_paths.storage_root, room_id)


def _mode_from_content(content: object) -> RoomReplyMode | None:
    value = content.get("value") if isinstance(content, dict) else None
    return cast("RoomReplyMode", value) if value in ROOM_REPLY_MODES else None


def _is_setting(source: Mapping[str, object]) -> bool:
    return source.get("type") == ROOM_SETTINGS_EVENT_TYPE and source.get("state_key") == _SETTING_KEY


def _take(key: tuple[Path, str], mode: RoomReplyMode | None, order: int, *, confirmed: bool) -> None:
    """Keep a result unless one numbered after it is kept already."""
    known = _known.get(key)
    if known is None or known.order < order:
        _known[key] = _Copy(mode, order, time.monotonic() if confirmed else _UNCONFIRMED)


def resolve_room_reply_mode_override(runtime_paths: RuntimePaths, room_id: str) -> RoomReplyMode | None:
    """Return the room's reply mode setting as the runtime last knew it, None while unset or never read."""
    known = _known.get(_key(runtime_paths, room_id))
    return None if known is None else known.mode


def observe_room_settings_event(runtime_paths: RuntimePaths, room_id: str, source: Mapping[str, object]) -> bool:
    """Take a live `reply_mode` event's value, unconfirmed; return whether it was one, which the caller reads again."""
    if not _is_setting(source):
        return False
    _take(_key(runtime_paths, room_id), _mode_from_content(source.get("content")), next(_order), confirmed=False)
    return True


def seed_room_reply_mode(runtime_paths: RuntimePaths, room_id: str, events: Iterable[Mapping[str, object]]) -> None:
    """Take the setting from a full read of the room's state made for something else, unless the room is known already.

    A room without the setting keeps no copy, so its first turn reads it and moves any override an older runtime stored.
    """
    key = _key(runtime_paths, room_id)
    if key in _known:
        return
    for source in events:
        if _is_setting(source):
            _take(key, _mode_from_content(source.get("content")), next(_order), confirmed=True)
            return


async def refresh_room_reply_mode(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
    *,
    force: bool = False,
    put_state: Callable[..., Awaitable[nio.RoomPutStateResponse | nio.RoomPutStateError | None]] | None = None,
) -> ReplyModeRead:
    """Read the room's reply mode setting unless a read confirmed the copy recently, and return what it found.

    ``force`` reads it whatever the copy's age. A caller deciding now uses the result, not the copy, which a later
    event may already have changed. A failed read keeps the copy. ``put_state``, the room steward's, writes an older
    runtime's override into a room without the setting, which the reading client itself never publishes.
    """
    import nio  # noqa: PLC0415 - config reads the copy above without loading the Matrix client

    key = _key(runtime_paths, room_id)
    known = _known.get(key)
    if not force and known is not None and time.monotonic() - known.confirmed_at < _REREAD_AFTER_SECONDS:
        return ReplyModeRead(ok=True, mode=known.mode)
    started = next(_order)
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
            response = await client.room_get_state_event(room_id, ROOM_SETTINGS_EVENT_TYPE, _SETTING_KEY)
    except Exception:
        logger.warning("room_reply_mode_read_failed", room_id=room_id, exc_info=True)
        return ReplyModeRead(ok=False, mode=resolve_room_reply_mode_override(runtime_paths, room_id))
    if isinstance(response, nio.RoomGetStateEventResponse):
        mode = _mode_from_content(response.content)
    elif isinstance(response, nio.RoomGetStateEventError) and response.status_code == "M_NOT_FOUND":
        # A room without the setting takes the override an older runtime stored on disk, which then leaves the file.
        # While the room refuses the write, the stored override still applies and the next read tries again.
        mode = _legacy_override(runtime_paths, room_id)
        if mode is not None and await write_room_reply_mode(client, runtime_paths, room_id, mode, put_state=put_state):
            _drop_legacy_override(runtime_paths, room_id)
            return ReplyModeRead(ok=True, mode=mode)
    else:
        logger.warning("room_reply_mode_read_failed", room_id=room_id, error=str(response))
        return ReplyModeRead(ok=False, mode=resolve_room_reply_mode_override(runtime_paths, room_id))
    _take(key, mode, started, confirmed=True)
    return ReplyModeRead(ok=True, mode=mode)


async def write_room_reply_mode(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
    mode: RoomReplyMode | None,
    *,
    put_state: Callable[..., Awaitable[nio.RoomPutStateResponse | nio.RoomPutStateError | None]] | None = None,
) -> bool | None:
    """Set the room's reply mode setting, or clear it given None, and return whether the room took it.

    It is written as ``client``, or through ``put_state``, the room steward's, for a client that only answers. None
    means refused: no steward, or the writer lacks the power, which only moderator rights can fix.
    The room's acknowledgement is the newest the runtime knows, unconfirmed, unless something arrived while the write
    was pending: then which is newer is unknown, so reads started before the acknowledgement change nothing and the
    room is read again.
    """
    import nio  # noqa: PLC0415 - config reads the copy above without loading the Matrix client

    key = _key(runtime_paths, room_id)
    started = next(_order)
    content = {} if mode is None else {"value": mode}
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
            write = put_state or client.room_put_state
            response = await write(room_id, ROOM_SETTINGS_EVENT_TYPE, content, state_key=_SETTING_KEY)
    except Exception:
        logger.warning("room_reply_mode_write_failed", room_id=room_id, exc_info=True)
        return False
    if not isinstance(response, nio.RoomPutStateResponse):
        refused = response is None or (
            isinstance(response, nio.RoomPutStateError) and response.status_code == "M_FORBIDDEN"
        )
        # A refusal is the room's rule, which a command's answer names; any other failure is worth a warning.
        log = logger.debug if refused else logger.warning
        log("room_reply_mode_write_failed", room_id=room_id, error=str(response))
        return None if refused else False
    known = _known.get(key)
    if known is None or known.order < started:
        _take(key, mode, next(_order), confirmed=False)
    else:
        _known[key] = known._replace(order=next(_order), confirmed_at=_UNCONFIRMED)
        await refresh_room_reply_mode(client, runtime_paths, room_id, force=True, put_state=put_state)
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
