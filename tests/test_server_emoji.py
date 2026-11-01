"""Tests for the optional server custom emoji room."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom import constants
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.rooms import ensure_server_emoji_room
from mindroom.matrix.state import MatrixState
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import (
    bind_runtime_paths,
    load_config_yaml,
    orchestrator_runtime_paths,
    published_config_load,
    runtime_paths_for,
)
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from pathlib import Path

_ROUTER = "@router:localhost"
_ROOM = "!emoji:localhost"
_ALIAS = "#emoji:localhost"
# Power levels a homeserver gives a room created with the public_chat preset.
_SERVER_LEVELS = {
    "users": {_ROUTER: 100},
    "users_default": 0,
    "events": {"m.room.name": 50, "m.room.power_levels": 100},
    "events_default": 0,
    "state_default": 50,
    "ban": 50,
}


def _config(tmp_path: Path, **config_data: object) -> Config:
    return bind_runtime_paths(
        Config(**config_data),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )


def _emoji_room_state(levels: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Return the state of an emoji room the router created, keyed by event type and state key."""
    return {
        ("m.room.create", ""): {"type": "io.mindroom.emoji", "m.federate": False, "room_version": "11"},
        ("m.room.canonical_alias", ""): {"alias": _ALIAS},
        ("m.room.power_levels", ""): levels,
        ("m.room.member", _ROUTER): {"membership": "join"},
    }


def _router_client(
    state: dict[tuple[str, str], dict[str, Any]],
    *,
    joined: bool,
    alias: nio.RoomResolveAliasResponse | nio.RoomResolveAliasError,
) -> AsyncMock:
    """Return a router client whose state reads and writes go to one emoji room's state."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.user_id = _ROUTER
    client.rooms = {}
    client.joined_rooms.return_value = nio.JoinedRoomsResponse([_ROOM] if joined else [])
    client.room_resolve_alias.return_value = alias
    client.room_create.return_value = nio.RoomCreateResponse(_ROOM)
    client.join.return_value = nio.JoinResponse(_ROOM)

    async def get_state(room_id: str) -> nio.RoomGetStateResponse:
        events = [
            {"type": event_type, "state_key": state_key, "sender": _ROUTER, "content": content}
            for (event_type, state_key), content in state.items()
        ]
        return nio.RoomGetStateResponse(events, room_id)

    async def get_state_event(room_id: str, event_type: str, state_key: str = "") -> nio.RoomGetStateEventResponse:
        return nio.RoomGetStateEventResponse(dict(state[(event_type, state_key)]), event_type, state_key, room_id)

    async def put_state(
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> nio.RoomPutStateResponse:
        state[(event_type, state_key)] = content
        return nio.RoomPutStateResponse.from_dict({"event_id": "$state"}, room_id=room_id)

    client.room_get_state.side_effect = get_state
    client.room_get_state_event.side_effect = get_state_event
    client.room_put_state.side_effect = put_state
    return client


def _missing_alias() -> nio.RoomResolveAliasError:
    return nio.RoomResolveAliasError("missing", "M_NOT_FOUND")


def _alias_of(room_id: str) -> nio.RoomResolveAliasResponse:
    return nio.RoomResolveAliasResponse(_ALIAS, room_id, ["localhost"])


def _recorded_room(config: Config) -> str | None:
    return MatrixState.load(runtime_paths_for(config)).server_emoji_room_id


def _record_room(config: Config) -> None:
    state = MatrixState.load(runtime_paths_for(config))
    state.set_server_emoji_room_id(_ROOM)
    save_matrix_state(state, runtime_paths_for(config))


def test_server_emoji_defaults(tmp_path: Path) -> None:
    """The emoji room is off by default, and `server_emoji: null` means the defaults."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("server_emoji: null\n", encoding="utf-8")

    for config in (Config(), load_config_yaml(config_path)):
        assert config.server_emoji.model_dump() == {"enabled": False, "name": "Emoji", "moderators_only": False}


def test_server_emoji_name_is_trimmed_and_cannot_be_empty() -> None:
    """The room and pack name is validated like the root Space name."""
    assert Config(server_emoji={"name": "  Example  "}).server_emoji.name == "Example"
    with pytest.raises(ValidationError, match=r"server_emoji\.name cannot be empty"):
        Config(server_emoji={"name": "   "})


@pytest.mark.parametrize("enabled", [True, False])
def test_managed_room_key_conflicts_with_the_enabled_emoji_room_alias(tmp_path: Path, enabled: bool) -> None:
    """A managed room key cannot take the namespaced emoji room alias while the emoji room is enabled."""
    runtime_paths = constants.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_NAMESPACE": "team1234"},
    )
    data = {
        "agents": {"general": {"display_name": "General", "rooms": ["emoji"]}},
        "server_emoji": {"enabled": enabled},
    }

    if enabled:
        with pytest.raises(ValueError, match="reserved server emoji room alias 'emoji_team1234': emoji"):
            Config.model_validate(data, context={"runtime_paths": runtime_paths})
    else:
        Config.model_validate(data, context={"runtime_paths": runtime_paths})


@pytest.mark.asyncio
async def test_disabled_server_emoji_sends_no_requests(tmp_path: Path) -> None:
    """A disabled emoji room is neither created nor touched."""
    client = AsyncMock()
    config = _config(tmp_path)

    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) is None

    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_missing_emoji_room_is_created_with_the_script_shape(tmp_path: Path) -> None:
    """A missing alias gets a hidden, local-only room with the alias, the name, and one empty pack."""
    client = _router_client(_emoji_room_state(dict(_SERVER_LEVELS)), joined=False, alias=_missing_alias())
    config = _config(tmp_path, server_emoji={"enabled": True, "name": "Example"})

    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) == _ROOM

    client.room_resolve_alias.assert_awaited_once_with(_ALIAS)
    client.room_create.assert_awaited_once()
    _method, _path, body = nio.Api.room_create("token", **client.room_create.await_args.kwargs)
    assert json.loads(body) == {
        "visibility": "private",
        "preset": "public_chat",
        "room_alias_name": "emoji",
        "name": "Example",
        "creation_content": {"type": "io.mindroom.emoji", "m.federate": False},
        "power_level_content_override": {"events_default": 50},
        "is_direct": False,
        "initial_state": [
            {
                "type": "im.ponies.room_emotes",
                "state_key": "",
                "content": {"pack": {"display_name": "Example", "usage": ["emoticon"]}, "images": {}},
            },
        ],
    }
    assert _recorded_room(config) == _ROOM


@pytest.mark.asyncio
async def test_existing_emoji_room_is_joined_and_reused(tmp_path: Path) -> None:
    """The router joins the room it created for the alias instead of creating another."""
    client = _router_client(_emoji_room_state(dict(_SERVER_LEVELS)), joined=False, alias=_alias_of(_ROOM))
    config = _config(tmp_path, server_emoji={"enabled": True})

    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) == _ROOM

    client.join.assert_awaited_once_with(_ROOM)
    client.room_create.assert_not_awaited()
    assert _recorded_room(config) == _ROOM


@pytest.mark.asyncio
@pytest.mark.parametrize(("moderators_only", "emoji_level"), [(False, 0), (True, 50)])
async def test_emoji_room_power_levels_are_reconciled(
    tmp_path: Path,
    moderators_only: bool,
    emoji_level: int,
) -> None:
    """Members or only moderators may edit packs, nobody below moderator may post, and room admins get 100."""
    levels = {
        **_SERVER_LEVELS,
        "users": {_ROUTER: 100, "@owner:localhost": 150},
        "events": {"m.room.name": 50, "m.room.message": 0, "m.room.encrypted": 75},
    }
    state = _emoji_room_state(levels)
    client = _router_client(state, joined=True, alias=_alias_of(_ROOM))
    config = _config(
        tmp_path,
        server_emoji={"enabled": True, "moderators_only": moderators_only},
        room_defaults={"admins": ["@owner:localhost", "@admin:localhost"]},
    )
    _record_room(config)

    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) == _ROOM

    assert state["m.room.power_levels", ""] == {
        "users": {_ROUTER: 100, "@owner:localhost": 150, "@admin:localhost": 100},
        "users_default": 0,
        "events": {
            "m.room.name": 50,
            "m.room.message": 50,
            "m.room.encrypted": 75,
            "im.ponies.room_emotes": emoji_level,
        },
        "events_default": 50,
        "state_default": 50,
        "ban": 50,
    }
    client.room_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciled_emoji_room_gets_no_power_level_write(tmp_path: Path) -> None:
    """A room whose levels already match is left alone, keeping a higher events_default."""
    levels = {
        **_SERVER_LEVELS,
        "users": {_ROUTER: 100, "@admin:localhost": 100},
        "events": {"im.ponies.room_emotes": 0},
        "events_default": 100,
    }
    client = _router_client(_emoji_room_state(levels), joined=True, alias=_alias_of(_ROOM))
    config = _config(tmp_path, server_emoji={"enabled": True}, room_defaults={"admins": ["@admin:localhost"]})
    _record_room(config)

    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) == _ROOM

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_alias_lookup_failure_retries_without_a_duplicate_room(tmp_path: Path) -> None:
    """A failed alias lookup creates nothing, and the next pass adopts the room the alias names."""
    client = _router_client(
        _emoji_room_state(dict(_SERVER_LEVELS)),
        joined=False,
        alias=nio.RoomResolveAliasError("slow down", "M_LIMIT_EXCEEDED"),
    )
    config = _config(tmp_path, server_emoji={"enabled": True})

    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) is None
    assert _recorded_room(config) is None

    client.room_resolve_alias.return_value = _alias_of(_ROOM)
    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) == _ROOM

    client.room_create.assert_not_awaited()
    assert _recorded_room(config) == _ROOM


@pytest.mark.asyncio
async def test_alias_held_by_another_account_creates_no_room(tmp_path: Path) -> None:
    """MindRoom Chat finds the room by its alias, so a room without it would be useless."""
    state = _emoji_room_state(dict(_SERVER_LEVELS))
    client = _router_client(state, joined=False, alias=_alias_of("!theirs:localhost"))
    client.room_get_state.side_effect = None
    client.room_get_state.return_value = nio.RoomGetStateError(
        "not a member",
        "M_FORBIDDEN",
        room_id="!theirs:localhost",
    )
    config = _config(tmp_path, server_emoji={"enabled": True})

    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) is None

    client.room_create.assert_not_awaited()
    client.join.assert_not_awaited()
    assert _recorded_room(config) is None


@pytest.mark.asyncio
async def test_router_room_that_is_not_an_emoji_room_is_left_alone(tmp_path: Path) -> None:
    """A room the router made for the alias for another purpose, such as an old managed room, is not adopted."""
    state = _emoji_room_state(dict(_SERVER_LEVELS))
    state["m.room.create", ""] = {"room_version": "11"}
    client = _router_client(state, joined=True, alias=_alias_of(_ROOM))
    config = _config(tmp_path, server_emoji={"enabled": True})

    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) is None

    client.room_put_state.assert_not_awaited()
    client.room_create.assert_not_awaited()
    assert _recorded_room(config) is None


@pytest.mark.asyncio
async def test_deleted_alias_of_the_joined_emoji_room_is_published_again(tmp_path: Path) -> None:
    """MindRoom Chat finds the room by its alias, so the router restores a deleted alias."""
    client = _router_client(_emoji_room_state(dict(_SERVER_LEVELS)), joined=True, alias=_missing_alias())
    client.room_put_alias.return_value = nio.RoomPutAliasResponse(_ALIAS, _ROOM)
    config = _config(tmp_path, server_emoji={"enabled": True})
    _record_room(config)

    assert await ensure_server_emoji_room(client, config, runtime_paths_for(config)) == _ROOM

    client.room_put_alias.assert_awaited_once_with(_ALIAS, _ROOM)
    client.room_create.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_setup_ensures_the_emoji_room_through_the_router(tmp_path: Path) -> None:
    """Room setup maintains the emoji room with the router's client."""
    config = _config(tmp_path, server_emoji={"enabled": True})
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config), config=config)
    router_bot = MagicMock()
    router_bot.client = AsyncMock()
    orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

    with patch("mindroom.orchestration.rooms.ensure_server_emoji_room", new=AsyncMock()) as mock_ensure:
        await orchestrator._rooms.ensure_server_emoji_room()

    mock_ensure.assert_awaited_once_with(router_bot.client, orchestrator.config, orchestrator.runtime_paths)


@pytest.mark.asyncio
async def test_server_emoji_change_alone_ensures_the_emoji_room_without_room_setup(tmp_path: Path) -> None:
    """Turning the emoji room on in a reload creates it without the full room membership flow."""
    initial_config = Config(agents={"general": {"display_name": "General", "rooms": ["lobby"]}})
    updated_config = Config(
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        server_emoji={"enabled": True},
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path), config=initial_config)
    general_bot = MagicMock()
    general_bot.refresh_presence = AsyncMock()
    orchestrator.fleet.add("general", general_bot)
    router_bot = MagicMock()
    router_bot.running = True
    router_bot.refresh_presence = AsyncMock()
    router_bot.recover_pending_turn_journal_events = AsyncMock()
    orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

    with (
        patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(updated_config)),
        patch.object(orchestrator._rooms, "setup", new=AsyncMock()) as mock_setup,
        patch.object(orchestrator._rooms, "link_root_space", new=AsyncMock()) as mock_root_space,
        patch.object(orchestrator._rooms, "ensure_server_emoji_room", new=AsyncMock()) as mock_emoji_room,
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
    ):
        await orchestrator.config_reload._update_config()

    assert orchestrator.config == updated_config
    mock_setup.assert_not_awaited()
    mock_root_space.assert_not_awaited()
    mock_emoji_room.assert_awaited_once_with()
