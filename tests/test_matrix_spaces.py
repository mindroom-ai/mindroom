"""Tests for optional root Matrix Space support."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import nio
import pytest
import yaml

from mindroom import constants
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, resolve_avatar_path
from mindroom.entity_resolution import mindroom_user_id
from mindroom.matrix import client_room_admin
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.state import MatrixState, matrix_state_for_runtime
from mindroom.matrix_identifiers import managed_space_alias_localpart
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    load_config_yaml,
    orchestrator_runtime_paths,
    published_config_load,
    runtime_paths_for,
)
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from pathlib import Path


def _config_with_runtime_paths(tmp_path, **config_data: object) -> Config:  # noqa: ANN001
    return bind_runtime_paths(
        Config(**config_data),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )


def _router_created_space_state(config: Config) -> nio.RoomGetStateResponse:
    alias = f"#{managed_space_alias_localpart(runtime_paths_for(config))}:localhost"
    return nio.RoomGetStateResponse(
        [
            {"type": "m.room.create", "state_key": "", "sender": "@router:localhost", "content": {}},
            {
                "type": "m.room.canonical_alias",
                "state_key": "",
                "sender": "@router:localhost",
                "content": {"alias": alias},
            },
        ],
        "!space:localhost",
    )


def test_matrix_space_defaults() -> None:
    """Matrix Space config should default to enabled with the standard name."""
    config = Config()

    assert config.matrix_space.enabled is True
    assert config.matrix_space.name == "MindRoom"


def test_matrix_space_yaml_null_uses_defaults(tmp_path) -> None:  # noqa: ANN001
    """`matrix_space: null` should be treated the same as omitting the block."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("matrix_space: null\n", encoding="utf-8")

    config = load_config_yaml(config_path)

    assert config.matrix_space.enabled is True
    assert config.matrix_space.name == "MindRoom"


def test_matrix_state_load_is_backward_compatible_without_space_room_id(tmp_path) -> None:  # noqa: ANN001
    """Older matrix state files without `space_room_id` should still load cleanly."""
    config_path = tmp_path / "config.yaml"
    state_path = tmp_path / "matrix_state.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    state_path.write_text(
        yaml.safe_dump(
            {
                "accounts": {"bot": {"username": "mindroom_bot", "password": "secret"}},
                "rooms": {"lobby": {"room_id": "!lobby:example.com", "alias": "#lobby:example.com", "name": "Lobby"}},
            },
        ),
        encoding="utf-8",
    )
    runtime_paths = constants.resolve_runtime_paths(config_path=config_path, storage_path=tmp_path)
    state = MatrixState.load(runtime_paths=runtime_paths)

    assert state.space_room_id is None
    assert state.rooms["lobby"].room_id == "!lobby:example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize("already_joined", [False, True])
async def test_ensure_user_in_rooms_joins_with_the_saved_internal_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_joined: bool,
) -> None:
    """The internal user joins missing rooms with its saved session and configured display name."""
    runtime_paths = orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml")
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account(
        "agent_user",
        "requested_internal",
        TEST_PASSWORD,
        domain="localhost",
        device_id="old_device",
        access_token=TEST_PASSWORD,
    )
    save_matrix_state(state, runtime_paths)

    client = AsyncMock()
    client.user_id = "@requested_internal:localhost"
    client.close = AsyncMock()
    login = AsyncMock(return_value=client)

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(matrix_rooms, "login_agent_http_client", login)
    monkeypatch.setattr(matrix_rooms, "join_room", join_room)
    monkeypatch.setattr(
        matrix_rooms,
        "get_joined_rooms",
        AsyncMock(
            return_value=["!lobby:localhost"] if already_joined else None,
        ),
    )

    await matrix_rooms.ensure_user_in_rooms(
        "http://localhost:8008",
        {"lobby": "!lobby:localhost"},
        runtime_paths,
        display_name="MindRoomUser",
    )

    login.assert_awaited_once()
    homeserver, user, login_runtime_paths = login.await_args.args
    assert (homeserver, login_runtime_paths) == ("http://localhost:8008", runtime_paths)
    assert (user.user_id, user.display_name, user.device_id, user.access_token) == (
        "@requested_internal:localhost",
        "MindRoomUser",
        "old_device",
        TEST_PASSWORD,
    )
    if already_joined:
        join_room.assert_not_awaited()
    else:
        join_room.assert_awaited_once_with(client, "!lobby:localhost")
    client.close.assert_awaited_once()


def test_config_rejects_room_key_that_conflicts_with_root_space_alias() -> None:
    """Managed room keys must not map to the reserved root Space alias."""
    runtime_paths = constants.resolve_runtime_paths()
    colliding_room_key = "_mindroom_root_space"

    with pytest.raises(ValueError, match="reserved root Space alias"):
        Config.model_validate(
            {
                "agents": {"general": {"display_name": "General", "rooms": [colliding_room_key]}},
                "matrix_space": {"enabled": True},
            },
            context={"runtime_paths": runtime_paths},
        )


def test_config_allows_colliding_room_key_when_space_disabled() -> None:
    """Colliding room keys should be accepted when the space feature is disabled."""
    runtime_paths = constants.resolve_runtime_paths()
    colliding_room_key = "_mindroom_root_space"

    config = Config.model_validate(
        {
            "agents": {"general": {"display_name": "General", "rooms": [colliding_room_key]}},
            "matrix_space": {"enabled": False},
        },
        context={"runtime_paths": runtime_paths},
    )
    assert config.matrix_space.enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("child_cached", [True, False])
async def test_add_room_to_space_is_idempotent_when_child_link_matches(child_cached: bool) -> None:
    """Existing child links are verified against `m.space.child` state whether or not the cached space lists them."""
    client = AsyncMock()
    space = nio.MatrixRoom("!space:example.com", "@router:example.com")
    if child_cached:
        space.children.add("!room:example.com")
    client.rooms = {"!space:example.com": space}
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"via": ["example.com"], "suggested": True},
        event_type="m.space.child",
        state_key="!room:example.com",
        room_id="!space:example.com",
    )

    result = await client_room_admin.add_room_to_space(
        client,
        "!space:example.com",
        "!room:example.com",
        "example.com",
    )

    assert result is True
    client.room_get_state_event.assert_awaited_once_with(
        "!space:example.com",
        "m.space.child",
        "!room:example.com",
    )
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_room_to_space_writes_child_link_when_missing() -> None:
    """Missing child state should be written with the expected `via` payload."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventError(
        "missing",
        status_code="M_NOT_FOUND",
        room_id="!space:example.com",
    )
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!space:example.com",
    )

    result = await client_room_admin.add_room_to_space(
        client,
        "!space:example.com",
        "!room:example.com",
        "example.com",
    )

    assert result is True
    client.room_put_state.assert_awaited_once_with(
        room_id="!space:example.com",
        event_type="m.space.child",
        content={"via": ["example.com"], "suggested": True},
        state_key="!room:example.com",
    )


@pytest.mark.asyncio
async def test_ensure_room_admin_power_levels_promotes_missing_and_underpowered_users() -> None:
    """Root Space admins should receive PL100 without disturbing existing higher levels."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "users": {
                "@router:example.com": 100,
                "@existing:example.com": 150,
                "@underpowered:example.com": 50,
            },
            "state_default": 50,
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!space:example.com",
    )
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$power"},
        room_id="!space:example.com",
    )

    result = await client_room_admin.ensure_room_admin_power_levels(
        client,
        "!space:example.com",
        {
            "@existing:example.com",
            "@underpowered:example.com",
            "@owner:example.com",
        },
    )

    assert result is True
    client.room_put_state.assert_awaited_once_with(
        room_id="!space:example.com",
        event_type="m.room.power_levels",
        content={
            "users": {
                "@router:example.com": 100,
                "@existing:example.com": 150,
                "@underpowered:example.com": 100,
                "@owner:example.com": 100,
            },
            "state_default": 50,
        },
    )


@pytest.mark.asyncio
async def test_ensure_room_admin_power_levels_is_idempotent_when_admins_already_powered() -> None:
    """No power-level write should happen when every target user is already an admin."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"users": {"@owner:example.com": 100}},
        event_type="m.room.power_levels",
        state_key="",
        room_id="!space:example.com",
    )

    result = await client_room_admin.ensure_room_admin_power_levels(
        client,
        "!space:example.com",
        {"@owner:example.com"},
    )

    assert result is True
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_room_admin_power_levels_returns_false_when_power_levels_read_fails() -> None:
    """Power-level read failures should fail closed and avoid overwriting state."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventError(
        "forbidden",
        status_code="M_FORBIDDEN",
        room_id="!space:example.com",
    )

    result = await client_room_admin.ensure_room_admin_power_levels(
        client,
        "!space:example.com",
        {"@owner:example.com"},
    )

    assert result is False
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_room_admin_power_levels_returns_false_when_power_level_write_fails() -> None:
    """Power-level write failures should report reconciliation failure."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"users": {}},
        event_type="m.room.power_levels",
        state_key="",
        room_id="!space:example.com",
    )
    client.room_put_state.return_value = nio.RoomPutStateError(
        "forbidden",
        status_code="M_FORBIDDEN",
        room_id="!space:example.com",
    )

    result = await client_room_admin.ensure_room_admin_power_levels(
        client,
        "!space:example.com",
        {"@owner:example.com"},
    )

    assert result is False
    client.room_put_state.assert_awaited_once_with(
        room_id="!space:example.com",
        event_type="m.room.power_levels",
        content={"users": {"@owner:example.com": 100}},
    )


@pytest.mark.asyncio
async def test_ensure_root_space_creates_space_links_rooms_and_persists_state(tmp_path) -> None:  # noqa: ANN001
    """Enabled root Space support should create the Space, persist it, and link rooms."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.room_get_state.return_value = nio.RoomGetStateResponse([], "!space:localhost")
    client.rooms = {}
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("not found", status_code="M_NOT_FOUND")
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
    )

    with (
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch("mindroom.matrix.rooms.create_space", new=AsyncMock(return_value="!space:localhost")) as mock_create,
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=True)) as mock_name,
        patch("mindroom.matrix.rooms.ensure_room_admin_power_levels", new=AsyncMock(return_value=True)) as mock_admins,
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": "!lobby:localhost", "dev": "!dev:localhost"},
            admin_user_ids={"@owner:localhost"},
        )

    assert space_id == "!space:localhost"
    assert matrix_state_for_runtime(runtime_paths_for(config)).space_room_id == "!space:localhost"
    mock_create.assert_awaited_once()
    mock_name.assert_awaited_once_with(client, "!space:localhost", "MindRoom", snapshot=ANY)
    mock_admins.assert_awaited_once_with(client, "!space:localhost", {"@owner:localhost"}, snapshot=ANY)
    assert mock_add.await_args_list == [
        call(client, "!space:localhost", "!lobby:localhost", "localhost", snapshot=ANY),
        call(client, "!space:localhost", "!dev:localhost", "localhost", snapshot=ANY),
    ]
    mock_avatar.assert_awaited_once()
    assert mock_avatar.await_args.args == (client, "!space:localhost")
    assert mock_avatar.await_args.kwargs["context"] == "root_space"
    assert await mock_avatar.await_args.kwargs["resolve_avatar"]() == resolve_avatar_path(
        "spaces",
        "root_space",
        runtime_paths_for(config),
    )


@pytest.mark.asyncio
async def test_ensure_root_space_resolves_existing_alias_without_recreating(tmp_path) -> None:  # noqa: ANN001
    """Existing root Spaces should be resolved by alias and reused."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.rooms = {}
    client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#_mindroom_root_space:localhost",
        room_id="!space:localhost",
        servers=["localhost"],
    )
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True, "name": "Workspace"},
    )
    client.user_id = "@router:localhost"
    client.room_get_state.return_value = _router_created_space_state(config)

    with (
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch(
            "mindroom.matrix.rooms.join_room",
            new=AsyncMock(return_value=RoomJoinOutcome.JOINED),
        ) as mock_join,
        patch("mindroom.matrix.rooms.create_space", new=AsyncMock()) as mock_create,
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=True)) as mock_name,
        patch("mindroom.matrix.rooms.ensure_room_admin_power_levels", new=AsyncMock(return_value=True)) as mock_admins,
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": "!lobby:localhost"},
            admin_user_ids={"@owner:localhost"},
        )

    assert space_id == "!space:localhost"
    assert matrix_state_for_runtime(runtime_paths_for(config)).space_room_id == "!space:localhost"
    mock_join.assert_awaited_once_with(client, "!space:localhost")
    mock_create.assert_not_awaited()
    mock_name.assert_awaited_once_with(client, "!space:localhost", "Workspace", snapshot=ANY)
    mock_admins.assert_awaited_once_with(client, "!space:localhost", {"@owner:localhost"}, snapshot=ANY)
    mock_add.assert_awaited_once_with(client, "!space:localhost", "!lobby:localhost", "localhost", snapshot=ANY)
    mock_avatar.assert_awaited_once()
    assert mock_avatar.await_args.args == (client, "!space:localhost")
    assert mock_avatar.await_args.kwargs["context"] == "root_space"
    assert await mock_avatar.await_args.kwargs["resolve_avatar"]() == resolve_avatar_path(
        "spaces",
        "root_space",
        runtime_paths_for(config),
    )


@pytest.mark.asyncio
async def test_ensure_root_space_skips_existing_alias_when_router_cannot_join(tmp_path) -> None:  # noqa: ANN001
    """A private existing root Space should not be reused if the router cannot join it."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.rooms = {}
    client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#_mindroom_root_space:localhost",
        room_id="!space:localhost",
        servers=["localhost"],
    )
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True, "name": "Workspace"},
    )
    client.user_id = "@router:localhost"
    client.room_get_state.return_value = _router_created_space_state(config)

    with (
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch(
            "mindroom.matrix.rooms.join_room",
            new=AsyncMock(return_value=RoomJoinOutcome.RETRYABLE_FAILURE),
        ) as mock_join,
        patch("mindroom.matrix.rooms.create_space", new=AsyncMock()) as mock_create,
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=True)) as mock_name,
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": "!lobby:localhost"},
        )

    assert space_id is None
    assert matrix_state_for_runtime(runtime_paths_for(config)).space_room_id is None
    mock_join.assert_awaited_once_with(client, "!space:localhost")
    mock_create.assert_not_awaited()
    mock_name.assert_not_awaited()
    mock_add.assert_not_awaited()
    mock_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_root_space_returns_none_when_name_write_fails(tmp_path) -> None:  # noqa: ANN001
    """If the router lacks permission to set the space name, reconciliation should bail out."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.room_get_state.return_value = nio.RoomGetStateResponse([], "!space:localhost")
    client.rooms = {"!space:localhost": MagicMock()}
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
    )
    save_matrix_state(MatrixState(space_room_id="!space:localhost"), runtime_paths_for(config))

    with (
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=["!space:localhost"])),
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=False)),
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": "!lobby:localhost"},
        )

    assert space_id is None
    mock_add.assert_not_awaited()
    mock_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_root_space_returns_none_when_admin_power_reconciliation_fails(tmp_path) -> None:  # noqa: ANN001
    """If root Space admin promotion fails, reconciliation should skip child linking."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.room_get_state.return_value = nio.RoomGetStateResponse([], "!space:localhost")
    client.rooms = {"!space:localhost": MagicMock()}
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
    )
    save_matrix_state(MatrixState(space_room_id="!space:localhost"), runtime_paths_for(config))

    with (
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=["!space:localhost"])),
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=True)),
        patch("mindroom.matrix.rooms.ensure_room_admin_power_levels", new=AsyncMock(return_value=False)) as mock_admins,
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": "!lobby:localhost"},
            admin_user_ids={"@owner:localhost"},
        )

    assert space_id is None
    mock_admins.assert_awaited_once_with(client, "!space:localhost", {"@owner:localhost"}, snapshot=ANY)
    mock_add.assert_not_awaited()
    mock_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_root_space_returns_none_when_child_link_fails(tmp_path) -> None:  # noqa: ANN001
    """If the router cannot add child links, reconciliation should fail."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.room_get_state.return_value = nio.RoomGetStateResponse([], "!space:localhost")
    client.rooms = {"!space:localhost": MagicMock()}
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
    )
    save_matrix_state(MatrixState(space_room_id="!space:localhost"), runtime_paths_for(config))

    with (
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=["!space:localhost"])),
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=True)),
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=False)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": "!lobby:localhost"},
        )

    assert space_id is None
    mock_add.assert_awaited_once_with(client, "!space:localhost", "!lobby:localhost", "localhost", snapshot=ANY)
    mock_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrator_ensure_root_space_uses_effective_room_invite_users(tmp_path) -> None:  # noqa: ANN001
    """The root Space should use effective room invitations without implicit users."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=orchestrator_runtime_paths(tmp_path),
        config=_config_with_runtime_paths(
            tmp_path,
            agents={"general": {"display_name": "General", "rooms": ["lobby", "!external:example.com"]}},
            matrix_space={"enabled": True},
            mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
            room_defaults={"invite_users": ["@owner:example.com"]},
            rooms={
                "lobby": {"invite_users": ["@collaborator:example.com"]},
            },
        ),
    )
    router_bot = MagicMock()
    router_bot.client = AsyncMock()
    orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

    with (
        patch(
            "mindroom.orchestration.rooms.ensure_root_space",
            new=AsyncMock(return_value="!space:localhost"),
        ) as mock_space,
        patch(
            "mindroom.orchestration.rooms.get_room_members",
            new=AsyncMock(return_value={"@mindroom_router:localhost"}),
        ),
        patch("mindroom.orchestration.rooms.invite_to_room", new=AsyncMock(return_value=True)) as mock_invite,
    ):
        await orchestrator._rooms.link_root_space({"lobby": "!lobby:localhost"})

    mock_space.assert_awaited_once_with(
        router_bot.client,
        orchestrator.config,
        orchestrator.runtime_paths,
        {"lobby": "!lobby:localhost"},
        admin_user_ids=set(),
    )
    invited_user_ids = {c.args[2] for c in mock_invite.await_args_list}
    assert invited_user_ids == {"@collaborator:example.com"}


@pytest.mark.asyncio
async def test_orchestrator_ensure_root_space_invites_authorized_user_without_internal_user(tmp_path) -> None:  # noqa: ANN001
    """The root Space should still invite the owner when no internal user exists."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=orchestrator_runtime_paths(tmp_path),
        config=Config(
            agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
            matrix_space={"enabled": True},
            room_defaults={"invite_users": ["@owner:example.com"]},
        ),
    )
    router_bot = MagicMock()
    router_bot.client = AsyncMock()
    orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)

    with (
        patch(
            "mindroom.orchestration.rooms.ensure_root_space",
            new=AsyncMock(return_value="!space:localhost"),
        ) as mock_space,
        patch(
            "mindroom.orchestration.rooms.get_room_members",
            new=AsyncMock(return_value={"@mindroom_router:localhost"}),
        ),
        patch("mindroom.orchestration.rooms.invite_to_room", new=AsyncMock(return_value=True)) as mock_invite,
    ):
        await orchestrator._rooms.link_root_space({"lobby": "!lobby:localhost"})

    mock_space.assert_awaited_once_with(
        router_bot.client,
        orchestrator.config,
        orchestrator.runtime_paths,
        {"lobby": "!lobby:localhost"},
        admin_user_ids=set(),
    )
    mock_invite.assert_awaited_once_with(
        router_bot.client,
        "!space:localhost",
        "@owner:example.com",
    )


@pytest.mark.asyncio
async def test_orchestrator_ensure_root_space_skips_existing_members(tmp_path) -> None:  # noqa: ANN001
    """Root Space invitations should skip users already in the Space."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=orchestrator_runtime_paths(tmp_path),
        config=_config_with_runtime_paths(
            tmp_path,
            agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
            matrix_space={"enabled": True},
            mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
            room_defaults={"invite_users": ["@owner:example.com"]},
        ),
    )
    router_bot = MagicMock()
    router_bot.client = AsyncMock()
    orchestrator.fleet.add(ROUTER_AGENT_NAME, router_bot)
    internal_user_id = mindroom_user_id(orchestrator.config, orchestrator.runtime_paths)

    with (
        patch("mindroom.orchestration.rooms.ensure_root_space", new=AsyncMock(return_value="!space:localhost")),
        patch("mindroom.orchestration.rooms.get_room_members", new=AsyncMock(return_value={internal_user_id})),
        patch("mindroom.orchestration.rooms.invite_to_room", new=AsyncMock(return_value=True)) as mock_invite,
    ):
        await orchestrator._rooms.link_root_space({"lobby": "!lobby:localhost"})

    mock_invite.assert_awaited_once_with(router_bot.client, "!space:localhost", "@owner:example.com")


@pytest.mark.asyncio
async def test_setup_rooms_and_memberships_reconciles_root_space_once(tmp_path) -> None:  # noqa: ANN001
    """Space and server emoji room reconciliation should happen as a post-room phase during startup."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=orchestrator_runtime_paths(tmp_path),
        config=Config(
            agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
            matrix_space={"enabled": True},
        ),
    )

    router_bot = AsyncMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    router_bot.rooms = []
    router_bot.ensure_rooms = AsyncMock()

    general_bot = AsyncMock()
    general_bot.agent_name = "general"
    general_bot.rooms = []
    general_bot.ensure_rooms = AsyncMock()

    with (
        patch.object(
            orchestrator._rooms,
            "ensure_rooms_exist",
            new=AsyncMock(side_effect=[{"lobby": "!room1:localhost"}, {"lobby": "!room1:localhost"}]),
        ),
        patch.object(orchestrator._rooms, "link_root_space", new=AsyncMock()) as mock_root_space,
        patch.object(orchestrator._rooms, "ensure_server_emoji_room", new=AsyncMock()) as mock_emoji_room,
        patch.object(orchestrator._rooms, "_ensure_room_invitations", new=AsyncMock()),
        patch("mindroom.orchestration.rooms.get_rooms_for_entity", return_value=["lobby"]),
        patch("mindroom.orchestration.rooms.resolve_room_aliases", return_value=["!room1:localhost"]),
        patch("mindroom.orchestration.rooms.load_rooms", return_value={"lobby": MagicMock(room_id="!room1:localhost")}),
        patch("mindroom.orchestration.rooms.ensure_user_in_rooms", new=AsyncMock()),
    ):
        await orchestrator._rooms.setup([router_bot, general_bot])

    assert mock_root_space.await_args_list == [
        call({"lobby": "!room1:localhost"}),
    ]
    mock_emoji_room.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_update_config_matrix_space_change_reconciles_without_room_membership_setup(tmp_path) -> None:  # noqa: ANN001
    """Space-only config changes should avoid the full room membership flow."""
    initial_config = Config(
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": False},
    )
    updated_config = Config(
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True, "name": "Workspace"},
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
        patch.object(
            orchestrator._rooms,
            "ensure_rooms_exist",
            new=AsyncMock(return_value={"lobby": "!room1:localhost"}),
        ) as mock_rooms,
        patch.object(orchestrator._rooms, "link_root_space", new=AsyncMock()) as mock_root_space,
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
    ):
        updated = await orchestrator.config_reload._update_config()

    assert updated is False
    assert orchestrator.config == updated_config
    general_bot.apply_published_config.assert_called_once_with()
    router_bot.apply_published_config.assert_called_once_with()
    mock_setup.assert_not_awaited()
    mock_rooms.assert_awaited_once_with()
    mock_root_space.assert_awaited_once_with({"lobby": "!room1:localhost"})
