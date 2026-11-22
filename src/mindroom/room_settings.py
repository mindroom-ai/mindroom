"""Room settings the runtime keeps a copy of: `reply_mode`, `model` and `thread_mode`, set by commands, plugin routes, or a client.

Each setting is the room's `io.mindroom.room_settings` state under its own state key with content `{"value": value}`;
`{}`, no event, or a value the setting does not take leaves it unset. The runtime keeps a copy per room and setting,
so checks that cannot wait for the homeserver use it, and a turn reads the room unless a read confirmed the copy
recently.

One rule keeps each copy: every read and write is numbered when it starts, every write's acknowledgement and every
live event when it arrives, and a result never replaces one numbered after it. So a read that started before a write
took effect, or before a change arrived in sync, changes nothing once that write or change is in. Only a read confirms
the copy. A live event applies at once but stays unconfirmed, since each bot's sync delivers it on its own schedule and
it may be a late copy of an older change; the bot that saw it reads the room again.

Runtimes before a setting stored its override in a file. That override applies while the runtime has no copy, and moves
into the room's state on the first read that finds the room without the setting.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeGuard, cast, get_args
from weakref import WeakValueDictionary

from mindroom.config.agent import RoomReplyMode
from mindroom.legacy_room_settings import drop_legacy_room_override, legacy_room_override
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Container, Iterable, Mapping
    from pathlib import Path

    import nio

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

RoomThreadMode = Literal["thread", "room"]
ROOM_REPLY_MODES: tuple[RoomReplyMode, ...] = get_args(RoomReplyMode)
ROOM_THREAD_MODES: tuple[RoomThreadMode, ...] = get_args(RoomThreadMode)
# A change made while no bot's sync timeline showed it, such as during a sync gap, applies after this long.
_REREAD_AFTER_SECONDS = 600.0
# A turn waits for the read and a command for the write, so a stalled homeserver costs them a few seconds at most.
_REQUEST_TIMEOUT_SECONDS = 5.0
_UNCONFIRMED = float("-inf")
_order = itertools.count(1)

type _PutState = Callable[..., Awaitable[nio.RoomPutStateResponse | nio.RoomPutStateError | None]]


@dataclass(frozen=True)
class RoomSetting[T: str]:
    """One setting: its state key, how a stored value reads, and the file and record field an older runtime kept it in."""

    key: str
    parse: Callable[[object], T | None]
    legacy_filename: str
    legacy_field: str


def _one_of[T: str](values: tuple[T, ...]) -> Callable[[object], T | None]:
    return lambda value: cast("T", value) if value in values else None


def _model_reference(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


REPLY_MODE: RoomSetting[RoomReplyMode] = RoomSetting(
    "reply_mode",
    _one_of(ROOM_REPLY_MODES),
    "room_reply_modes.json",
    "mode",
)
# A model reference, `name` or `name@level`, kept whatever it names: whether this runtime configures it is decided
# each time it is used, so a config reload can bring it back.
MODEL: RoomSetting[str] = RoomSetting("model", _model_reference, "room_model_overrides.json", "model")
THREAD_MODE: RoomSetting[RoomThreadMode] = RoomSetting(
    "thread_mode",
    _one_of(ROOM_THREAD_MODES),
    "room_thread_modes.json",
    "mode",
)
_SETTINGS: tuple[RoomSetting[Any], ...] = (REPLY_MODE, MODEL, THREAD_MODE)


class _Copy(NamedTuple):
    value: str | None
    # The number of the read, acknowledgement, or event it came from.
    order: int
    # When the read it came from returned, or _UNCONFIRMED.
    confirmed_at: float


class SettingRead[T: str](NamedTuple):
    """What one refresh found: the room's setting, or with ``ok`` False what the runtime knew when the room was unread."""

    ok: bool
    value: T | None


class _RoomModelOverride(NamedTuple):
    """A room's `model` setting: ``active`` when this runtime configures the model it names, else ``stale``."""

    active: str | None
    stale: str | None


type _Key = tuple[Path, str, str]
_known: dict[_Key, _Copy] = {}
_NEVER_READ = _Copy(None, 0, _UNCONFIRMED)
# One setting's writes run one at a time, so an older runtime's override moves into it once and after no newer write.
_writes: WeakValueDictionary[_Key, asyncio.Lock] = WeakValueDictionary()
# Each model setting that names no configured model, which is logged once.
_stale_models_logged: set[_Key] = set()


def _key(setting: RoomSetting[Any], runtime_paths: RuntimePaths, room_id: str) -> _Key:
    return (runtime_paths.storage_root, room_id, setting.key)


def _value_from_content[T: str](setting: RoomSetting[T], content: object) -> T | None:
    return setting.parse(content.get("value")) if isinstance(content, dict) else None


def _setting_of(source: Mapping[str, object]) -> RoomSetting[Any] | None:
    if source.get("type") != ROOM_SETTINGS_EVENT_TYPE:
        return None
    return next((setting for setting in _SETTINGS if setting.key == source.get("state_key")), None)


def _take(key: _Key, value: str | None, order: int, *, confirmed: bool) -> None:
    """Keep a result unless one numbered after it is kept already."""
    known = _known.get(key)
    if known is None or known.order < order:
        _known[key] = _Copy(value, order, time.monotonic() if confirmed else _UNCONFIRMED)


def _legacy_override[T: str](setting: RoomSetting[T], runtime_paths: RuntimePaths, room_id: str) -> T | None:
    return setting.parse(legacy_room_override(runtime_paths, room_id, setting.legacy_filename, setting.legacy_field))


def _drop_legacy_override(setting: RoomSetting[Any], runtime_paths: RuntimePaths, room_id: str) -> None:
    """Remove the override the room's state replaced; a file that cannot be rewritten keeps it for a later read."""
    try:
        drop_legacy_room_override(runtime_paths, room_id, setting.legacy_filename, setting.legacy_field)
    except OSError:
        logger.warning("room_setting_legacy_override_kept", room_id=room_id, setting=setting.key, exc_info=True)


def state_event_was_read(response: object) -> TypeGuard[nio.RoomGetStateEventResponse]:
    """Return whether a read of one state event found it; nio decodes every failure but a 404 as the event's response."""
    import nio  # noqa: PLC0415 - config reads the copy below without loading the Matrix client

    return (
        isinstance(response, nio.RoomGetStateEventResponse)
        and getattr(response.transport_response, "status", 200) == 200
    )


def resolve_room_setting[T: str](setting: RoomSetting[T], runtime_paths: RuntimePaths, room_id: str | None) -> T | None:
    """Return a room's setting as the runtime last knew it, None while unset.

    A room the runtime has not read yet uses the override an older runtime stored for it, if any.
    """
    if room_id is None:
        return None
    known = _known.get(_key(setting, runtime_paths, room_id))
    return _legacy_override(setting, runtime_paths, room_id) if known is None else cast("T | None", known.value)


def classify_room_model(model: str | None, configured_models: Container[str]) -> _RoomModelOverride:
    """Split a room's `model` setting by whether ``configured_models`` has the model it names."""
    return (
        _RoomModelOverride(None, model)
        if model is not None and model not in configured_models
        else _RoomModelOverride(model, None)
    )


def resolve_room_model_override(
    runtime_paths: RuntimePaths,
    room_id: str | None,
    *,
    configured_models: Container[str],
) -> _RoomModelOverride:
    """Classify a room's `model` setting as the runtime last knew it; a model this runtime lacks is ignored."""
    override = classify_room_model(resolve_room_setting(MODEL, runtime_paths, room_id), configured_models)
    if override.stale is not None and room_id is not None:
        seen = (runtime_paths.storage_root, room_id, override.stale)
        if seen not in _stale_models_logged:
            _stale_models_logged.add(seen)
            logger.warning("room_model_setting_ignored", room_id=room_id, model=override.stale)
    return override


def observe_room_settings_event(
    runtime_paths: RuntimePaths,
    room_id: str,
    source: Mapping[str, object],
) -> RoomSetting[Any] | None:
    """Take a live setting event's value, unconfirmed; return the setting it was, which the caller reads again."""
    setting = _setting_of(source)
    if setting is not None:
        value = _value_from_content(setting, source.get("content"))
        _take(_key(setting, runtime_paths, room_id), value, next(_order), confirmed=False)
    return setting


def seed_room_settings(runtime_paths: RuntimePaths, room_id: str, events: Iterable[Mapping[str, object]]) -> None:
    """Take each setting from a full read of the room's state made for something else, unless it is known already.

    A room without a setting keeps no copy of it, so its first turn reads it and moves any override an older runtime
    stored, which applies until then. A setting the room has replaces that override, which leaves the file.
    """
    for source in events:
        setting = _setting_of(source)
        if setting is None:
            continue
        if (key := _key(setting, runtime_paths, room_id)) not in _known:
            _take(key, _value_from_content(setting, source.get("content")), next(_order), confirmed=True)
        _drop_legacy_override(setting, runtime_paths, room_id)


async def refresh_room_setting[T: str](
    setting: RoomSetting[T],
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
    *,
    force: bool = False,
    put_state: _PutState | None,
) -> SettingRead[T]:
    """Read one of the room's settings unless a read confirmed the copy recently, and return what it found.

    ``force`` reads it whatever the copy's age. A caller deciding now uses the result, not the copy, which a later
    event may already have changed. A failed read keeps the copy. ``put_state``, the room steward's, writes an older
    runtime's override into a room without the setting; the read a write ends with passes None and moves nothing.
    """
    import nio  # noqa: PLC0415 - config reads the copy above without loading the Matrix client

    key = _key(setting, runtime_paths, room_id)
    known = _known.get(key)
    if not force and known is not None and time.monotonic() - known.confirmed_at < _REREAD_AFTER_SECONDS:
        return SettingRead(ok=True, value=cast("T | None", known.value))
    started = next(_order)
    try:
        async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
            response = await client.room_get_state_event(room_id, ROOM_SETTINGS_EVENT_TYPE, setting.key)
    except Exception:
        logger.warning("room_setting_read_failed", room_id=room_id, setting=setting.key, exc_info=True)
        return SettingRead(ok=False, value=resolve_room_setting(setting, runtime_paths, room_id))
    if state_event_was_read(response):
        # Only a read the homeserver answered with the event counts: a failed one keeps the copy and the override.
        value = _value_from_content(setting, response.content)
    elif isinstance(response, nio.RoomGetStateEventError) and response.status_code == "M_NOT_FOUND":
        # A room without the setting takes the override an older runtime stored on disk, which then leaves the file.
        # While the room refuses the write, the stored override still applies and the next read tries again.
        value = _legacy_override(setting, runtime_paths, room_id)
        move = None if value is None or put_state is None else write_room_setting
        if move and await move(setting, client, runtime_paths, room_id, value, put_state=put_state, moves_read=started):
            return SettingRead(ok=True, value=value)
        # A write, a move or a change that came in meanwhile is the room's setting: this answer is older than it.
        newer = _known.get(key, _NEVER_READ)
        if newer.order > started:
            return SettingRead(ok=True, value=cast("T | None", newer.value))
    else:
        logger.warning("room_setting_read_failed", room_id=room_id, setting=setting.key, error=str(response))
        return SettingRead(ok=False, value=resolve_room_setting(setting, runtime_paths, room_id))
    _take(key, value, started, confirmed=True)
    if state_event_was_read(response):
        # The room's own event, even an empty one, decides over the override an older runtime stored.
        _drop_legacy_override(setting, runtime_paths, room_id)
    return SettingRead(ok=True, value=value)


async def refresh_room_settings(
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
    *,
    put_state: _PutState | None,
) -> SettingRead[RoomReplyMode]:
    """Refresh every setting a turn uses, at once, and return what the reply mode's read found."""
    reply_mode, _model, _thread_mode = await asyncio.gather(
        refresh_room_setting(REPLY_MODE, client, runtime_paths, room_id, put_state=put_state),
        refresh_room_setting(MODEL, client, runtime_paths, room_id, put_state=put_state),
        refresh_room_setting(THREAD_MODE, client, runtime_paths, room_id, put_state=put_state),
    )
    return reply_mode


async def write_room_setting[T: str](
    setting: RoomSetting[T],
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    room_id: str,
    value: T | None,
    *,
    put_state: _PutState | None = None,
    moves_read: int | None = None,
) -> bool | None:
    """Set one of the room's settings, or clear it given None, and return whether the room took it.

    It is written as ``client``, or through ``put_state``, the room steward's, for a client that only answers. None
    means refused: no steward, or the writer lacks the power, which only moderator rights can fix.
    The room's acknowledgement is the newest the runtime knows, unconfirmed, unless something arrived while the write
    was pending: then which is newer is unknown, so reads started before the acknowledgement change nothing and the
    room is read again. ``moves_read`` numbers the read that moves an older runtime's override: once its turn comes,
    it writes nothing if the override left the file or the runtime knows a newer result.
    """
    import nio  # noqa: PLC0415 - config reads the copy above without loading the Matrix client

    key = _key(setting, runtime_paths, room_id)
    async with _writes.setdefault(key, asyncio.Lock()):
        if moves_read is not None and (
            _legacy_override(setting, runtime_paths, room_id) != value
            or _known.get(key, _NEVER_READ).order > moves_read
        ):
            return False
        started = next(_order)
        content = {} if value is None else {"value": value}
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT_SECONDS):
                write = put_state or client.room_put_state
                response = await write(room_id, ROOM_SETTINGS_EVENT_TYPE, content, state_key=setting.key)
        except Exception:
            logger.warning("room_setting_write_failed", room_id=room_id, setting=setting.key, exc_info=True)
            return False
        if not isinstance(response, nio.RoomPutStateResponse):
            refused = response is None or (
                isinstance(response, nio.RoomPutStateError) and response.status_code == "M_FORBIDDEN"
            )
            # A refusal is the room's rule, which a command's answer names; any other failure is worth a warning.
            log = logger.debug if refused else logger.warning
            log("room_setting_write_failed", room_id=room_id, setting=setting.key, error=str(response))
            return None if refused else False
        known = _known.get(key)
        if known is None or known.order < started:
            _take(key, value, next(_order), confirmed=False)
        else:
            _known[key] = known._replace(order=next(_order), confirmed_at=_UNCONFIRMED)
            await refresh_room_setting(setting, client, runtime_paths, room_id, force=True, put_state=None)
        # The room now has the setting, so the override an older runtime stored never applies again.
        _drop_legacy_override(setting, runtime_paths, room_id)
        return True
