"""Tests for the room settings the runtime keeps a copy of, beyond what each setting's own tests cover.

`tests/test_room_reply_modes.py` covers how a copy is kept: the order of reads, writes and live events.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, call, patch

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
    on_thread_mode_change,
    refresh_room_setting,
    refresh_room_settings,
    resolve_room_setting,
    seed_room_settings,
    write_room_setting,
)
from tests.conftest import test_runtime_paths
from tests.room_settings_helpers import RoomStateHomeserver

if TYPE_CHECKING:
    from aioresponses import aioresponses

    from mindroom.constants import RuntimePaths

ROOM_ID = "!room:localhost"
OTHER_ROOM = "!other:localhost"
_SETTINGS = pytest.mark.parametrize("setting", [REPLY_MODE, MODEL, THREAD_MODE], ids=lambda setting: setting.key)
_STATE_URL = re.compile(r"http://localhost:8008/_matrix/client/v3/rooms/.*/state/.*")
# A value of each setting.
_VALUES = {"reply_mode": "mentions", "model": "large@high", "thread_mode": "room"}


def _event(key: str, content: dict[str, object]) -> dict[str, object]:
    return {"type": ROOM_SETTINGS_EVENT_TYPE, "state_key": key, "content": content}


def _write_legacy_overrides(runtime_paths: RuntimePaths, setting: RoomSetting[Any], room_ids: tuple[str, ...]) -> Path:
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
    setting: RoomSetting[Any],
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
    setting: RoomSetting[Any],
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
async def test_an_override_file_that_cannot_be_rewritten_keeps_its_record_for_a_later_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The room's setting applies whatever becomes of the file: a turn's read never fails for it and a later one retries."""
    runtime_paths = test_runtime_paths(tmp_path)
    legacy = _write_legacy_overrides(runtime_paths, THREAD_MODE, (ROOM_ID,))
    homeserver = RoomStateHomeserver()
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "thread_mode"] = {}
    client = homeserver.client()
    with monkeypatch.context() as patched:
        patched.setattr("mindroom.legacy_room_settings.write_override_records", Mock(side_effect=OSError("read-only")))
        await refresh_room_settings(client, runtime_paths, ROOM_ID, put_state=client.room_put_state)

    assert resolve_room_setting(THREAD_MODE, runtime_paths, ROOM_ID) is None
    assert set(json.loads(legacy.read_text())) == {ROOM_ID}

    await refresh_room_setting(THREAD_MODE, client, runtime_paths, ROOM_ID, force=True, put_state=None)

    assert json.loads(legacy.read_text()) == {}


@pytest.mark.asyncio
@_SETTINGS
async def test_an_override_file_that_cannot_be_read_is_not_a_room_without_an_override(
    tmp_path: Path,
    setting: RoomSetting[Any],
) -> None:
    """While the file cannot be read, a room that refused the move keeps its override, which applies again after."""
    runtime_paths = test_runtime_paths(tmp_path)
    value = _VALUES[setting.key]
    legacy = _write_legacy_overrides(runtime_paths, setting, (ROOM_ID,))
    client = RoomStateHomeserver().client()
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")

    async def read() -> SettingRead[Any]:
        return await refresh_room_setting(
            setting,
            client,
            runtime_paths,
            ROOM_ID,
            force=True,
            put_state=client.room_put_state,
        )

    assert await read() == SettingRead(ok=True, value=value)
    # Reading a directory fails as a file without permission does, for any user.
    aside = legacy.rename(legacy.with_suffix(".aside"))
    legacy.mkdir()

    assert await read() == SettingRead(ok=False, value=value)
    assert resolve_room_setting(setting, runtime_paths, ROOM_ID) == value

    legacy.rmdir()
    aside.rename(legacy)
    assert await read() == SettingRead(ok=True, value=value)
    assert set(json.loads(legacy.read_text())) == {ROOM_ID}


@pytest.mark.asyncio
@_SETTINGS
@pytest.mark.parametrize("failure", [PermissionError("denied"), "{"], ids=["unreadable", "undecodable"])
async def test_an_override_file_read_that_fails_once_keeps_the_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setting: RoomSetting[Any],
    failure: OSError | str,
) -> None:
    """One read of the file decides: when it fails, the room is not taken for one without an override."""
    runtime_paths = test_runtime_paths(tmp_path)
    value = _VALUES[setting.key]
    legacy = _write_legacy_overrides(runtime_paths, setting, (ROOM_ID,))
    homeserver = RoomStateHomeserver()
    client = homeserver.client()
    read_text = Path.read_text

    def failing_read(path: Path, *args: Any, **kwargs: Any) -> str:  # noqa: ANN401
        if path != legacy:
            return read_text(path, *args, **kwargs)
        if isinstance(failure, OSError):
            raise failure
        return failure

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_text", failing_read)
        failed = await refresh_room_setting(setting, client, runtime_paths, ROOM_ID, put_state=client.room_put_state)
    assert not failed.ok
    client.room_put_state.assert_not_awaited()

    read = await refresh_room_setting(setting, client, runtime_paths, ROOM_ID, put_state=client.room_put_state)

    assert read == SettingRead(ok=True, value=value)
    assert homeserver.events[ROOM_SETTINGS_EVENT_TYPE, setting.key] == {"value": value}
    assert json.loads(legacy.read_text()) == {}


@pytest.mark.asyncio
@_SETTINGS
async def test_a_write_the_room_takes_replaces_an_older_runtimes_override(
    tmp_path: Path,
    setting: RoomSetting[Any],
) -> None:
    """A setting cleared before any read stays cleared after a restart: the stored override leaves the file."""
    runtime_paths = test_runtime_paths(tmp_path)
    legacy = _write_legacy_overrides(runtime_paths, setting, (ROOM_ID, OTHER_ROOM))
    client = RoomStateHomeserver().client()

    assert await write_room_setting(setting, client, runtime_paths, ROOM_ID, None) is True

    assert set(json.loads(legacy.read_text())) == {OTHER_ROOM}


@pytest.mark.asyncio
@_SETTINGS
async def test_an_unreadable_room_keeps_an_older_runtimes_override(tmp_path: Path, setting: RoomSetting[Any]) -> None:
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


@pytest.mark.asyncio
@_SETTINGS
@pytest.mark.parametrize(
    ("status", "answer"),
    [
        (503, {"payload": {"errcode": "M_UNKNOWN", "error": "Service unavailable"}}),
        (403, {"payload": {"errcode": "M_FORBIDDEN", "error": "Not in the room"}}),
        (502, {"body": "<html>Bad Gateway</html>", "content_type": "text/html"}),
        (200, {"body": "<html>Back soon</html>", "content_type": "text/html"}),
        (200, {"body": '{"value": ', "content_type": "application/json"}),
        (200, {"body": "", "content_type": "application/json"}),
        (200, {"body": "[]", "content_type": "application/json"}),
    ],
    ids=["unavailable", "forbidden", "not-json", "proxy-page", "malformed", "empty", "not-an-object"],
)
async def test_a_failed_read_nio_decodes_as_an_event_keeps_the_copy_and_the_override(
    tmp_path: Path,
    aioresponse: aioresponses,
    setting: RoomSetting[Any],
    status: int,
    answer: dict[str, Any],
) -> None:
    """Nio decodes every failed read but a 404 as the event's own response, and a body that is no JSON as `{}`.

    So the HTTP status and the body decide what was read: only a JSON object the homeserver answered with is the event.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    value = _VALUES[setting.key]
    legacy = _write_legacy_overrides(runtime_paths, setting, (ROOM_ID,))
    seed_room_settings(runtime_paths, OTHER_ROOM, [_event(setting.key, {"value": value})])
    aioresponse.get(_STATE_URL, status=status, repeat=True, **answer)
    client = nio.AsyncClient("http://localhost:8008", "@bot:localhost")
    client.access_token = "token"  # noqa: S105
    put_state = AsyncMock()
    try:
        assert isinstance(
            await client.room_get_state_event(ROOM_ID, ROOM_SETTINGS_EVENT_TYPE, setting.key),
            nio.RoomGetStateEventResponse,
        )
        unread = await refresh_room_setting(setting, client, runtime_paths, ROOM_ID, put_state=put_state)
        known = await refresh_room_setting(setting, client, runtime_paths, OTHER_ROOM, force=True, put_state=put_state)
    finally:
        await client.close()

    assert unread == known == SettingRead(ok=False, value=value)
    assert set(json.loads(legacy.read_text())) == {ROOM_ID}
    put_state.assert_not_awaited()


@pytest.mark.asyncio
@_SETTINGS
async def test_an_empty_event_the_homeserver_answers_with_supersedes_the_override(
    tmp_path: Path,
    aioresponse: aioresponses,
    setting: RoomSetting[Any],
) -> None:
    """A cleared setting, the JSON object `{}`, is the room's own event, unlike a page nio also decodes as `{}`."""
    runtime_paths = test_runtime_paths(tmp_path)
    legacy = _write_legacy_overrides(runtime_paths, setting, (ROOM_ID,))
    aioresponse.get(_STATE_URL, payload={})
    client = nio.AsyncClient("http://localhost:8008", "@bot:localhost")
    client.access_token = "token"  # noqa: S105
    try:
        read = await refresh_room_setting(setting, client, runtime_paths, ROOM_ID, put_state=AsyncMock())
    finally:
        await client.close()

    assert read == SettingRead(ok=True, value=None)
    assert json.loads(legacy.read_text()) == {}


def test_a_full_state_read_seeds_every_setting(tmp_path: Path) -> None:
    """The room state a restore reads gives each setting before any turn, and an older runtime's override until then."""
    runtime_paths = test_runtime_paths(tmp_path)
    legacy = _write_legacy_overrides(runtime_paths, THREAD_MODE, (ROOM_ID, OTHER_ROOM))
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
    # The setting the read found replaces the override, even after a restart.
    assert set(json.loads(legacy.read_text())) == {OTHER_ROOM}


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
@pytest.mark.parametrize("steward", [False, True], ids=["other-bot", "steward"])
async def test_a_live_setting_change_is_read_again_once(
    tmp_path: Path,
    setting: RoomSetting[Any],
    *,
    steward: bool,
) -> None:
    """A bot reads a changed setting again, and whichever bot sees a changed thread mode tells the room's steward.

    The steward itself leaves that read to its republish.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    homeserver = RoomStateHomeserver()
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, setting.key] = {"value": _VALUES[setting.key]}
    client = homeserver.client()
    elected = SimpleNamespace(steward_duties=Mock())
    bot = SimpleNamespace(
        runtime_paths=runtime_paths,
        orchestrator=SimpleNamespace(room_steward=lambda _room_id: elected),
        steward_duties=Mock(is_steward=Mock(return_value=steward)),
    )

    await AgentBot._reread_room_setting(bot, client, ROOM_ID, setting)

    assert elected.steward_duties._thread_mode_setting_changed.call_count == (setting is THREAD_MODE)
    assert client.room_get_state_event.await_count == (not (steward and setting is THREAD_MODE))


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [None, "thread", "room"], ids=["unset", "as-known", "changed"])
async def test_a_read_that_finds_the_thread_mode_changed_tells_the_runtime(tmp_path: Path, held: str | None) -> None:
    """A change no sync showed is told when a read finds it; a room's first read and an unchanged one tell nothing."""
    runtime_paths = test_runtime_paths(tmp_path)
    homeserver = RoomStateHomeserver()
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "thread_mode"] = {"value": "thread"}
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": "auto"}
    client = homeserver.client()
    changed = Mock()
    on_thread_mode_change(runtime_paths, changed)
    await refresh_room_settings(client, runtime_paths, ROOM_ID, put_state=None)
    changed.assert_not_called()
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "thread_mode"] = {} if held is None else {"value": held}
    homeserver.events[ROOM_SETTINGS_EVENT_TYPE, "reply_mode"] = {"value": "mentions"}

    with patch("mindroom.room_settings.time.monotonic", return_value=time.monotonic() + 3600):
        await refresh_room_settings(client, runtime_paths, ROOM_ID, put_state=None)

    assert changed.call_args_list == ([] if held == "thread" else [call(ROOM_ID)])
    assert resolve_room_setting(THREAD_MODE, runtime_paths, ROOM_ID) == held
