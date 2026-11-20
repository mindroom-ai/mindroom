"""Tests for who may invite by email and which rooms are refused."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.email_invites.policy import may_invite, refused_room_kind
from mindroom.matrix.personal_room_store import (
    PersonalRoomRecord,
    is_personal_room,
    personal_room_record_path,
    write_personal_room,
)
from mindroom.matrix.room_power import invite_power_level
from mindroom.matrix.state import MatrixState, _load_matrix_state_file_cached
from tests.conftest import test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:example.org"
USER = "@carol:example.org"


def _client(
    *,
    membership: str = "join",
    power_levels: dict[str, object] | None = None,
    room_version: str = "11",
    state_events: list[dict[str, object]] | None = None,
) -> AsyncMock:
    contents: dict[str, dict[str, object]] = {
        "m.room.member": {"membership": membership},
        "m.room.power_levels": power_levels if power_levels is not None else {"users": {"@bas:example.org": 100}},
        "m.room.create": {"room_version": room_version},
    }

    async def state_event(
        room_id: str,
        event_type: str,
        state_key: str = "",
    ) -> nio.RoomGetStateEventResponse | nio.RoomGetStateEventError:
        content = contents.get(event_type)
        if content is None:
            return nio.RoomGetStateEventError("unreadable", "M_FORBIDDEN", room_id=room_id)
        return nio.RoomGetStateEventResponse(content, event_type, state_key, room_id)

    client = AsyncMock()
    client.room_get_state_event = AsyncMock(side_effect=state_event)
    client.room_get_state = AsyncMock(return_value=nio.RoomGetStateResponse(state_events or [], ROOM))
    client._contents = contents
    return client


@pytest.mark.asyncio
async def test_default_invite_level_lets_members_invite() -> None:
    """Without an `invite` entry, members at the default level may invite."""
    assert invite_power_level({}) == 0
    assert await may_invite(_client(), ROOM, USER) is True


@pytest.mark.asyncio
async def test_raised_invite_level() -> None:
    """A raised `invite` level needs a member at that level."""
    levels: dict[str, object] = {"invite": 50, "users": {}}
    assert invite_power_level(levels) == 50
    assert await may_invite(_client(power_levels=levels), ROOM, USER) is False
    levels_at_50: dict[str, object] = {"invite": 50, "users": {USER: 50}}
    assert await may_invite(_client(power_levels=levels_at_50), ROOM, USER) is True


@pytest.mark.asyncio
async def test_legacy_string_power_levels() -> None:
    """Integer strings of older room versions are read as their numbers, and anything else fails closed."""
    assert invite_power_level({"invite": "50"}) == 50
    assert invite_power_level({"invite": "fifty"}) is None
    levels: dict[str, object] = {"invite": "50", "users": {USER: "0"}}
    assert await may_invite(_client(power_levels=levels), ROOM, USER) is False
    levels = {"invite": "50", "users": {USER: " 50 "}}
    assert await may_invite(_client(power_levels=levels), ROOM, USER) is True
    levels = {"invite": "50", "users_default": "50"}
    assert await may_invite(_client(power_levels=levels), ROOM, USER) is True
    assert await may_invite(_client(power_levels={"invite": 1.5}), ROOM, USER) is None


@pytest.mark.asyncio
async def test_creator_in_privileged_room_version() -> None:
    """A creator of a version 12 room may invite without a `users` entry."""
    create = {
        "type": "m.room.create",
        "state_key": "",
        "sender": USER,
        "content": {"room_version": "12"},
        "event_id": "$create",
        "origin_server_ts": 0,
    }
    client = _client(power_levels={"invite": 100, "users": {}}, room_version="12", state_events=[create])
    assert await may_invite(client, ROOM, USER) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["leave", "invite", "ban"])
async def test_not_joined(membership: str) -> None:
    """Only a joined member may invite."""
    assert await may_invite(_client(membership=membership), ROOM, USER) is False


@pytest.mark.asyncio
async def test_unreadable_state() -> None:
    """Unreadable membership, power levels, or creators give None."""
    client = _client()
    del client._contents["m.room.power_levels"]
    assert await may_invite(client, ROOM, USER) is None
    client = _client()
    del client._contents["m.room.member"]
    assert await may_invite(client, ROOM, USER) is None
    client = _client()
    del client._contents["m.room.create"]
    assert await may_invite(client, ROOM, USER) is None
    client = _client()
    client.room_get_state_event = AsyncMock(side_effect=TimeoutError)
    assert await may_invite(client, ROOM, USER) is None


@pytest.mark.asyncio
async def test_listed_approver() -> None:
    """A listed approver may invite without any state read."""
    client = _client(membership="leave")
    assert await may_invite(client, ROOM, "@ops:example.org", ["@ops:example.org"]) is True
    client.room_get_state_event.assert_not_called()
    client.room_get_state.assert_not_called()


@pytest.mark.asyncio
async def test_refused_rooms(tmp_path: Path) -> None:
    """Personal rooms and configured onboarding rooms are refused."""
    paths = test_runtime_paths(tmp_path)
    record = PersonalRoomRecord(
        user_id=USER,
        alias="#personal-carol:example.org",
        source_room_id="!lobby:example.org",
        room_id="!personal:example.org",
    )
    write_personal_room(personal_room_record_path(paths, "mind", USER), record)
    state = MatrixState()
    state.add_room("lobby", room_id="!lobby:example.org", alias="#lobby:localhost", name="Lobby")
    save_matrix_state(state, paths)
    _load_matrix_state_file_cached.cache_clear()
    config = Config(
        agents={"mind": {"display_name": "Mind"}},
        rooms={"lobby": {}},
        personal_rooms={"agent": "mind", "onboarding_rooms": ["lobby"]},
        router=RouterConfig(model="default"),
    )

    assert is_personal_room(paths, "!personal:example.org") is True
    assert is_personal_room(paths, ROOM) is False
    client = _client()
    assert await refused_room_kind(client, config, paths, "!personal:example.org") == "personal"
    assert await refused_room_kind(client, config, paths, "!lobby:example.org") == "onboarding"
    assert await refused_room_kind(client, config, paths, ROOM) is None
    plain = Config(router=RouterConfig(model="default"))
    assert await refused_room_kind(client, plain, paths, "!lobby:example.org") is None
    client._contents["m.room.create"] = {"room_version": "11", "type": "m.space"}
    assert await refused_room_kind(client, config, paths, ROOM) == "space"
    del client._contents["m.room.create"]
    assert await refused_room_kind(client, config, paths, ROOM) == "unknown"
