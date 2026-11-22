"""Tests for the room settings the runtime keeps a copy of, beyond what each setting's own tests cover.

`tests/test_room_reply_modes.py` covers how a copy is kept: the order of reads, writes and live events.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.constants import tracking_dir
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE
from mindroom.room_settings import (
    MODEL,
    REPLY_MODE,
    THREAD_MODE,
    RoomSetting,
    SettingRead,
    observe_room_settings_event,
    refresh_room_setting,
    refresh_room_settings,
    resolve_room_setting,
    seed_room_settings,
)
from tests.conftest import test_runtime_paths
from tests.room_settings_helpers import RoomStateHomeserver

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ROOM_ID = "!room:localhost"
OTHER_ROOM = "!other:localhost"
_SETTINGS = pytest.mark.parametrize("setting", [REPLY_MODE, MODEL, THREAD_MODE], ids=lambda setting: setting.key)
# A value of each setting.
_VALUES = {"reply_mode": "mentions", "model": "large@high", "thread_mode": "room"}


def _event(key: str, content: dict[str, object]) -> dict[str, object]:
    return {"type": ROOM_SETTINGS_EVENT_TYPE, "state_key": key, "content": content}


def _write_legacy_overrides(runtime_paths: RuntimePaths, setting: RoomSetting[str], room_ids: tuple[str, ...]) -> Path:
    """Store a value of ``setting`` for each room the way an older runtime did."""
    legacy = tracking_dir(runtime_paths) / setting.legacy_filename
    legacy.parent.mkdir(parents=True, exist_ok=True)
    record = {setting.legacy_field: _VALUES[setting.key], "set_at": "2026-11-01T00:00:00+00:00"}
    legacy.write_text(json.dumps(dict.fromkeys(room_ids, record)))
    return legacy


@pytest.mark.asyncio
@_SETTINGS
@pytest.mark.parametrize("refused", [False, True], ids=["moved", "room-refuses"])
async def test_an_older_runtimes_override_applies_until_it_moves_into_the_room(
    tmp_path: Path,
    setting: RoomSetting[str],
    *,
    refused: bool,
) -> None:
    """An override an older runtime stored applies before the room is read, and the first read moves it into the room.

    While the room refuses the write, the override still applies and stays on disk for the next read.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    value = _VALUES[setting.key]
    legacy = _write_legacy_overrides(runtime_paths, setting, (ROOM_ID, OTHER_ROOM))
    homeserver = RoomStateHomeserver()
    client = homeserver.client()
    if refused:
        client.room_put_state.side_effect = None
        client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    assert resolve_room_setting(setting, runtime_paths, ROOM_ID) == value

    read = await refresh_room_setting(setting, client, runtime_paths, ROOM_ID, put_state=client.room_put_state)

    assert read == SettingRead(ok=True, value=value)
    assert resolve_room_setting(setting, runtime_paths, ROOM_ID) == value
    stored = json.loads(legacy.read_text())
    if refused:
        assert set(stored) == {ROOM_ID, OTHER_ROOM}
        assert (ROOM_SETTINGS_EVENT_TYPE, setting.key) not in homeserver.events
    else:
        assert set(stored) == {OTHER_ROOM}
        assert homeserver.events[ROOM_SETTINGS_EVENT_TYPE, setting.key] == {"value": value}


@pytest.mark.asyncio
@_SETTINGS
async def test_a_setting_the_room_already_has_supersedes_an_older_runtimes_override(
    tmp_path: Path,
    setting: RoomSetting[str],
) -> None:
    """A room whose setting was cleared stays cleared: its `{}` decides, and the stored override leaves the file."""
    runtime_paths = test_runtime_paths(tmp_path)
    legacy = _write_legacy_overrides(runtime_paths, setting, (ROOM_ID,))
    homeserver = RoomStateHomeserver()
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, setting.key] = {}
    client = homeserver.client()

    read = await refresh_room_setting(setting, client, runtime_paths, ROOM_ID, put_state=client.room_put_state)

    assert read == SettingRead(ok=True, value=None)
    assert resolve_room_setting(setting, runtime_paths, ROOM_ID) is None
    assert json.loads(legacy.read_text()) == {}
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@_SETTINGS
async def test_an_unreadable_room_keeps_an_older_runtimes_override(tmp_path: Path, setting: RoomSetting[str]) -> None:
    """Only a room that answers it has no such setting takes the stored override; a failed read moves nothing."""
    runtime_paths = test_runtime_paths(tmp_path)
    legacy = _write_legacy_overrides(runtime_paths, setting, (ROOM_ID,))
    client = RoomStateHomeserver().client()
    client.room_get_state_event.side_effect = None
    client.room_get_state_event.return_value = nio.RoomGetStateEventError("unavailable", "M_UNKNOWN")

    read = await refresh_room_setting(setting, client, runtime_paths, ROOM_ID, put_state=client.room_put_state)

    assert read == SettingRead(ok=False, value=_VALUES[setting.key])
    assert set(json.loads(legacy.read_text())) == {ROOM_ID}
    client.room_put_state.assert_not_awaited()


def test_a_full_state_read_seeds_every_setting(tmp_path: Path) -> None:
    """The room state a restore reads gives each setting before any turn, and an older runtime's override until then."""
    runtime_paths = test_runtime_paths(tmp_path)
    _write_legacy_overrides(runtime_paths, THREAD_MODE, (ROOM_ID, OTHER_ROOM))
    state = [
        {"type": "m.room.create", "state_key": "", "content": {}},
        _event("reply_mode", {"value": "mentions"}),
        _event("model", {"value": " large@high "}),
        _event("thread_mode", {}),
        _event("thread_summary_model", {"value": "large"}),
    ]

    seed_room_settings(runtime_paths, ROOM_ID, state)
    seed_room_settings(runtime_paths, OTHER_ROOM, state[:1])

    assert resolve_room_setting(REPLY_MODE, runtime_paths, ROOM_ID) == "mentions"
    assert resolve_room_setting(MODEL, runtime_paths, ROOM_ID) == "large@high"
    assert resolve_room_setting(THREAD_MODE, runtime_paths, ROOM_ID) is None
    assert resolve_room_setting(THREAD_MODE, runtime_paths, OTHER_ROOM) == "room"


@pytest.mark.parametrize(
    ("content", "expected"),
    [({"value": "large"}, "large"), ({"value": "  "}, None), ({"value": 4}, None), ({}, None)],
    ids=["name", "blank", "not-a-string", "cleared"],
)
def test_a_model_setting_is_any_non_blank_string(
    tmp_path: Path,
    content: dict[str, object],
    expected: str | None,
) -> None:
    """The `model` setting keeps whatever reference the room holds; a blank or non-string value leaves it unset."""
    runtime_paths = test_runtime_paths(tmp_path)
    observe_room_settings_event(runtime_paths, ROOM_ID, _event("model", {"value": "earlier"}))

    assert observe_room_settings_event(runtime_paths, ROOM_ID, _event("model", content)) is MODEL
    assert resolve_room_setting(MODEL, runtime_paths, ROOM_ID) == expected


@pytest.mark.asyncio
async def test_a_turn_refreshes_every_setting_at_once(tmp_path: Path) -> None:
    """A turn reads the room's reply mode, model and thread mode together, and decides by the reply mode's read."""
    runtime_paths = test_runtime_paths(tmp_path)
    homeserver = RoomStateHomeserver()
    for key, value in _VALUES.items():
        homeserver.events[ROOM_SETTINGS_EVENT_TYPE, key] = {"value": value}
    client = homeserver.client()

    for _ in range(2):
        read = await refresh_room_settings(client, runtime_paths, ROOM_ID, put_state=client.room_put_state)
        assert read == SettingRead(ok=True, value="mentions")
        assert client.room_get_state_event.await_count == 3
    for setting in (REPLY_MODE, MODEL, THREAD_MODE):
        assert resolve_room_setting(setting, runtime_paths, ROOM_ID) == _VALUES[setting.key]


@pytest.mark.asyncio
@_SETTINGS
async def test_a_live_thread_mode_change_tells_the_bots_steward_duties(
    tmp_path: Path, setting: RoomSetting[str]
) -> None:
    """A bot reads a changed setting again, and hands a changed thread mode to its steward duties to republish."""
    runtime_paths = test_runtime_paths(tmp_path)
    homeserver = RoomStateHomeserver()
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, setting.key] = {"value": _VALUES[setting.key]}
    bot = SimpleNamespace(
        runtime_paths=runtime_paths,
        orchestrator=SimpleNamespace(room_steward=lambda _room_id: None),
        steward_duties=SimpleNamespace(thread_mode_setting_changed=AsyncMock()),
    )

    await AgentBot._reread_room_setting(bot, homeserver.client(), ROOM_ID, setting)

    assert resolve_room_setting(setting, runtime_paths, ROOM_ID) == _VALUES[setting.key]
    assert bot.steward_duties.thread_mode_setting_changed.await_count == (setting is THREAD_MODE)
