"""Tests for optional root Matrix Space support."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import nio
import pytest
import yaml

from mindroom import constants
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix import client as matrix_client
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix.identity import managed_space_alias_localpart, mindroom_namespace
from mindroom.matrix.state import MatrixState
from mindroom.orchestrator import MultiAgentOrchestrator


def _config_with_runtime_paths(tmp_path, **config_data: object) -> Config:  # noqa: ANN001
    runtime_paths = constants.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    config = Config(**config_data)
    config._runtime_paths = runtime_paths
    return config


def test_matrix_space_defaults() -> None:
    """Matrix Space config should default to enabled with the standard name."""
    config = Config()

    assert config.matrix_space.enabled is True
    assert config.matrix_space.name == "MindRoom"


def test_matrix_space_yaml_null_uses_defaults(tmp_path) -> None:  # noqa: ANN001
    """`matrix_space: null` should be treated the same as omitting the block."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("matrix_space: null\n", encoding="utf-8")

    config = Config.from_yaml(config_path)

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


def test_config_rejects_room_key_that_conflicts_with_root_space_alias() -> None:
    """Managed room keys must not map to the reserved root Space alias."""
    runtime_paths = constants.resolve_runtime_paths()
    reserved_alias = managed_space_alias_localpart(runtime_paths)
    namespace = mindroom_namespace(runtime_paths)
    colliding_room_key = (
        reserved_alias.removesuffix(f"_{namespace}")
        if namespace and reserved_alias.endswith(f"_{namespace}")
        else reserved_alias
    )

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
    reserved_alias = managed_space_alias_localpart(runtime_paths)
    namespace = mindroom_namespace(runtime_paths)
    colliding_room_key = (
        reserved_alias.removesuffix(f"_{namespace}")
        if namespace and reserved_alias.endswith(f"_{namespace}")
        else reserved_alias
    )

    config = Config.model_validate(
        {
            "agents": {"general": {"display_name": "General", "rooms": [colliding_room_key]}},
            "matrix_space": {"enabled": False},
        },
        context={"runtime_paths": runtime_paths},
    )
    assert config.matrix_space.enabled is False


@pytest.mark.asyncio
async def test_add_room_to_space_is_idempotent_when_child_link_matches() -> None:
    """Matching `m.space.child` state should not trigger another state write."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"via": ["example.com"], "suggested": True},
        event_type="m.space.child",
        state_key="!room:example.com",
        room_id="!space:example.com",
    )

    result = await matrix_client.add_room_to_space(
        client,
        "!space:example.com",
        "!room:example.com",
        "example.com",
    )

    assert result is True
    client.room_put_state.assert_not_called()


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

    result = await matrix_client.add_room_to_space(
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
async def test_ensure_root_space_creates_space_links_rooms_and_persists_state(tmp_path) -> None:  # noqa: ANN001
    """Enabled root Space support should create the Space, persist it, and link rooms."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.rooms = {}
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("not found", status_code="M_NOT_FOUND")
    state = MatrixState()
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
    )

    with (
        patch("mindroom.matrix.rooms.MatrixState.load", return_value=state),
        patch.object(MatrixState, "save", autospec=True) as mock_save,
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch("mindroom.matrix.rooms.create_space", new=AsyncMock(return_value="!space:localhost")) as mock_create,
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=True)) as mock_name,
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar_if_available", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(
            client,
            config,
            {"lobby": "!lobby:localhost", "dev": "!dev:localhost"},
        )

    assert space_id == "!space:localhost"
    assert state.space_room_id == "!space:localhost"
    mock_save.assert_called_once_with(state, runtime_paths=config.runtime_paths)
    mock_create.assert_awaited_once()
    mock_name.assert_awaited_once_with(client, "!space:localhost", "MindRoom")
    assert mock_add.await_args_list == [
        call(client, "!space:localhost", "!lobby:localhost", "localhost"),
        call(client, "!space:localhost", "!dev:localhost", "localhost"),
    ]
    mock_avatar.assert_awaited_once_with(
        client,
        "!space:localhost",
        avatar_category="spaces",
        avatar_name="root_space",
        context="root_space",
        runtime_paths=config.runtime_paths,
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
    state = MatrixState()
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True, "name": "Workspace"},
    )

    with (
        patch("mindroom.matrix.rooms.MatrixState.load", return_value=state),
        patch.object(MatrixState, "save", autospec=True) as mock_save,
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch("mindroom.matrix.rooms.join_room", new=AsyncMock(return_value=True)) as mock_join,
        patch("mindroom.matrix.rooms.create_space", new=AsyncMock()) as mock_create,
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=True)) as mock_name,
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar_if_available", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(client, config, {"lobby": "!lobby:localhost"})

    assert space_id == "!space:localhost"
    assert state.space_room_id == "!space:localhost"
    mock_save.assert_called_once_with(state, runtime_paths=config.runtime_paths)
    mock_join.assert_awaited_once_with(client, "!space:localhost")
    mock_create.assert_not_awaited()
    mock_name.assert_awaited_once_with(client, "!space:localhost", "Workspace")
    mock_add.assert_awaited_once_with(client, "!space:localhost", "!lobby:localhost", "localhost")
    mock_avatar.assert_awaited_once_with(
        client,
        "!space:localhost",
        avatar_category="spaces",
        avatar_name="root_space",
        context="root_space",
        runtime_paths=config.runtime_paths,
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
    state = MatrixState()
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True, "name": "Workspace"},
    )

    with (
        patch("mindroom.matrix.rooms.MatrixState.load", return_value=state),
        patch.object(MatrixState, "save", autospec=True) as mock_save,
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch("mindroom.matrix.rooms.join_room", new=AsyncMock(return_value=False)) as mock_join,
        patch("mindroom.matrix.rooms.create_space", new=AsyncMock()) as mock_create,
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=True)) as mock_name,
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar_if_available", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(client, config, {"lobby": "!lobby:localhost"})

    assert space_id is None
    assert state.space_room_id is None
    mock_save.assert_not_called()
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
    client.rooms = {"!space:localhost": MagicMock()}
    state = MatrixState(space_room_id="!space:localhost")
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
    )

    with (
        patch("mindroom.matrix.rooms.MatrixState.load", return_value=state),
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=["!space:localhost"])),
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=False)),
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=True)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar_if_available", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(client, config, {"lobby": "!lobby:localhost"})

    assert space_id is None
    mock_add.assert_not_awaited()
    mock_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_root_space_returns_none_when_child_link_fails(tmp_path) -> None:  # noqa: ANN001
    """If the router cannot add child links, reconciliation should fail."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.rooms = {"!space:localhost": MagicMock()}
    state = MatrixState(space_room_id="!space:localhost")
    config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
    )

    with (
        patch("mindroom.matrix.rooms.MatrixState.load", return_value=state),
        patch("mindroom.matrix.rooms.get_joined_rooms", new=AsyncMock(return_value=["!space:localhost"])),
        patch("mindroom.matrix.rooms.ensure_room_name", new=AsyncMock(return_value=True)),
        patch("mindroom.matrix.rooms.add_room_to_space", new=AsyncMock(return_value=False)) as mock_add,
        patch("mindroom.matrix.rooms._set_room_avatar_if_available", new=AsyncMock()) as mock_avatar,
    ):
        space_id = await matrix_rooms.ensure_root_space(client, config, {"lobby": "!lobby:localhost"})

    assert space_id is None
    mock_add.assert_awaited_once_with(client, "!space:localhost", "!lobby:localhost", "localhost")
    mock_avatar.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrator_ensure_root_space_invites_internal_and_authorized_users(tmp_path) -> None:  # noqa: ANN001
    """The orchestrator should invite both the internal user and authorized users to the root Space."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.config = _config_with_runtime_paths(
        tmp_path,
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
        mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
        authorization={
            "global_users": ["@owner:example.com"],
            "room_permissions": {"lobby": ["@collaborator:example.com"]},
        },
    )
    router_bot = MagicMock()
    router_bot.client = AsyncMock()
    orchestrator.agent_bots[ROUTER_AGENT_NAME] = router_bot

    with (
        patch("mindroom.orchestrator.ensure_root_space", new=AsyncMock(return_value="!space:localhost")) as mock_space,
        patch("mindroom.orchestrator.get_room_members", new=AsyncMock(return_value={"@mindroom_router:localhost"})),
        patch("mindroom.orchestrator.invite_to_room", new=AsyncMock(return_value=True)) as mock_invite,
    ):
        await orchestrator._ensure_root_space({"lobby": "!lobby:localhost"})

    mock_space.assert_awaited_once_with(
        router_bot.client,
        orchestrator.config,
        {"lobby": "!lobby:localhost"},
    )
    # Should have invited both the internal user and the authorized owner
    invited_user_ids = {c.args[2] for c in mock_invite.await_args_list}
    assert orchestrator.config.get_mindroom_user_id() in invited_user_ids
    assert "@owner:example.com" in invited_user_ids
    assert "@collaborator:example.com" not in invited_user_ids


@pytest.mark.asyncio
async def test_orchestrator_ensure_root_space_invites_authorized_user_without_internal_user(tmp_path) -> None:  # noqa: ANN001
    """The root Space should still invite the owner when no internal user exists."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.config = Config(
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
        authorization={"global_users": ["@owner:example.com"]},
    )
    router_bot = MagicMock()
    router_bot.client = AsyncMock()
    orchestrator.agent_bots[ROUTER_AGENT_NAME] = router_bot

    with (
        patch("mindroom.orchestrator.ensure_root_space", new=AsyncMock(return_value="!space:localhost")),
        patch("mindroom.orchestrator.get_room_members", new=AsyncMock(return_value={"@mindroom_router:localhost"})),
        patch("mindroom.orchestrator.invite_to_room", new=AsyncMock(return_value=True)) as mock_invite,
    ):
        await orchestrator._ensure_root_space({"lobby": "!lobby:localhost"})

    mock_invite.assert_awaited_once_with(
        router_bot.client,
        "!space:localhost",
        "@owner:example.com",
    )


@pytest.mark.asyncio
async def test_setup_rooms_and_memberships_runs_root_space_after_each_room_reconciliation(tmp_path) -> None:  # noqa: ANN001
    """Space reconciliation should happen as a post-room phase during startup."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.config = Config(
        agents={"general": {"display_name": "General", "rooms": ["lobby"]}},
        matrix_space={"enabled": True},
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
            orchestrator,
            "_ensure_rooms_exist",
            new=AsyncMock(side_effect=[{"lobby": "!room1:localhost"}, {"lobby": "!room1:localhost"}]),
        ),
        patch.object(orchestrator, "_ensure_root_space", new=AsyncMock()) as mock_root_space,
        patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
        patch("mindroom.orchestrator.get_rooms_for_entity", return_value=["lobby"]),
        patch("mindroom.orchestrator.resolve_room_aliases", return_value=["!room1:localhost"]),
        patch("mindroom.orchestrator.load_rooms", return_value={"lobby": MagicMock(room_id="!room1:localhost")}),
        patch("mindroom.orchestrator.ensure_user_in_rooms", new=AsyncMock()),
    ):
        await orchestrator._setup_rooms_and_memberships([router_bot, general_bot])

    assert mock_root_space.await_args_list == [
        call({"lobby": "!room1:localhost"}),
        call({"lobby": "!room1:localhost"}),
    ]


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

    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.config = initial_config

    general_bot = MagicMock()
    general_bot.config = initial_config
    general_bot.enable_streaming = True
    general_bot._set_presence_with_model_info = AsyncMock()
    orchestrator.agent_bots["general"] = general_bot

    router_bot = MagicMock()
    router_bot.config = initial_config
    router_bot.enable_streaming = True
    router_bot._set_presence_with_model_info = AsyncMock()
    orchestrator.agent_bots[ROUTER_AGENT_NAME] = router_bot

    with (
        patch.object(Config, "from_yaml", return_value=updated_config),
        patch("mindroom.orchestration.config_updates._identify_entities_to_restart", return_value=set()),
        patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()) as mock_setup,
        patch.object(
            orchestrator,
            "_ensure_rooms_exist",
            new=AsyncMock(return_value={"lobby": "!room1:localhost"}),
        ) as mock_rooms,
        patch.object(orchestrator, "_ensure_root_space", new=AsyncMock()) as mock_root_space,
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
    ):
        updated = await orchestrator.update_config()

    assert updated is True
    assert general_bot.config == updated_config
    assert router_bot.config == updated_config
    mock_setup.assert_not_awaited()
    mock_rooms.assert_awaited_once_with()
    mock_root_space.assert_awaited_once_with({"lobby": "!room1:localhost"})
