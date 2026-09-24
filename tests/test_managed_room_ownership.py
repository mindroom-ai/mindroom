"""Managed rooms are adopted and kept only while the router owns them and enforces their policy."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.access_policy import resolve_room_policy
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import is_configured_room
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix import state as matrix_state
from mindroom.matrix.client_room_admin import room_control_problem
from mindroom.matrix.room_reconciliation import RoomStateSnapshot, read_room_state
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.access_schema_support import membership_config
from tests.conftest import runtime_paths_for
from tests.managed_room_helpers import router_owned_room_events

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config

_ROUTER = "@mindroom_router:localhost"
_SQUATTER = "@squatter:localhost"
_LOBBY_ALIAS = "#lobby:localhost"
_GENUINE_ROOM = "!genuine:localhost"
_SQUATTED_ROOM = "!squatted:localhost"


def _router_client(room_id: str, events: list[dict[str, object]] | None) -> AsyncMock:
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.user_id = _ROUTER
    client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(_LOBBY_ALIAS, room_id, ["localhost"])
    client.room_get_state.return_value = (
        nio.RoomGetStateResponse(events, room_id)
        if events is not None
        else nio.RoomGetStateError("not in room", "M_FORBIDDEN", room_id)
    )
    client.joined_rooms.return_value = nio.JoinedRoomsResponse([])
    return client


def _squatted_room_events() -> list[dict[str, object]]:
    """Return a world-readable room another account published under the lobby alias, inviting the router."""
    return [
        *router_owned_room_events(_SQUATTER, _LOBBY_ALIAS),
        {"type": "m.room.member", "state_key": _ROUTER, "sender": _SQUATTER, "content": {"membership": "invite"}},
        {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "world_readable"}},
    ]


def _record_lobby(config: Config, room_id: str) -> None:
    runtime_paths = runtime_paths_for(config)
    state = matrix_state.MatrixState.load(runtime_paths=runtime_paths)
    state.add_room("lobby", room_id, _LOBBY_ALIAS, "Lobby")
    state.save(runtime_paths=runtime_paths)


async def _ensure_lobby(client: AsyncMock, config: Config) -> str | None:
    policy = resolve_room_policy(config, "lobby")
    with (
        patch.object(matrix_rooms, "create_room", new=AsyncMock()) as create,
        patch.object(matrix_rooms, "generate_room_topic_ai", new=AsyncMock(return_value="topic")),
    ):
        room_id = await matrix_rooms._ensure_room_exists(
            client=client,
            room_key="lobby",
            config=config,
            runtime_paths=runtime_paths_for(config),
            room_policy=policy,
            admin_user_ids=matrix_rooms._room_admin_user_ids(policy),
        )
    create.assert_not_awaited()
    return room_id


async def _snapshot(events: list[dict[str, object]]) -> RoomStateSnapshot:
    client = AsyncMock()
    client.room_get_state.return_value = nio.RoomGetStateResponse(events, _GENUINE_ROOM)
    snapshot = await read_room_state(client, _GENUINE_ROOM)
    assert snapshot is not None
    return snapshot


@pytest.mark.asyncio
async def test_router_owned_room_with_configured_admins_is_controlled() -> None:
    """Configured admins may share the router's power without making the room foreign."""
    events = router_owned_room_events(_ROUTER, _LOBBY_ALIAS, users={"@admin:localhost": 100, "@agent:localhost": 50})
    assert room_control_problem(await _snapshot(events), _ROUTER, _LOBBY_ALIAS, ["@admin:localhost"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"type": "m.room.create", "state_key": "", "sender": _SQUATTER, "content": {}}, "created by @squatter"),
        (
            {
                "type": "m.room.canonical_alias",
                "state_key": "",
                "sender": _ROUTER,
                "content": {"alias": "#dev:localhost"},
            },
            "canonical alias",
        ),
        (
            {"type": "m.room.member", "state_key": _ROUTER, "sender": _SQUATTER, "content": {"membership": "invite"}},
            "not joined",
        ),
        (
            {"type": "m.room.power_levels", "state_key": "", "sender": _ROUTER, "content": {"users": {_ROUTER: 50}}},
            "below 100",
        ),
        (
            {
                "type": "m.room.power_levels",
                "state_key": "",
                "sender": _ROUTER,
                "content": {"users": {_ROUTER: 100}, "state_default": 101},
            },
            "below 101",
        ),
        (
            {
                "type": "m.room.power_levels",
                "state_key": "",
                "sender": _ROUTER,
                "content": {"users": {_ROUTER: 100, "@removed-admin:localhost": 100}},
            },
            "@removed-admin:localhost",
        ),
        (
            {
                "type": "m.room.power_levels",
                "state_key": "",
                "sender": _ROUTER,
                "content": {"users": {_ROUTER: 100}, "users_default": 100},
            },
            "every user",
        ),
    ],
)
async def test_room_the_router_does_not_control_is_reported(override: dict[str, object], expected: str) -> None:
    """Creator, alias binding, membership, and unmatched admin power are each required."""
    events = [*router_owned_room_events(_ROUTER, _LOBBY_ALIAS), override]
    problem = room_control_problem(await _snapshot(events), _ROUTER, _LOBBY_ALIAS, ["@admin:localhost"])
    assert problem is not None
    assert expected in problem


def _v12_room_events(additional_creators: list[str]) -> list[dict[str, object]]:
    """Return a room v12 room the router created, where creators stay out of power_levels.users."""
    return [
        *router_owned_room_events(_ROUTER, _LOBBY_ALIAS),
        {
            "type": "m.room.create",
            "state_key": "",
            "sender": _ROUTER,
            "content": {"room_version": "12", "additional_creators": additional_creators},
        },
        {
            "type": "m.room.power_levels",
            "state_key": "",
            "sender": _ROUTER,
            "content": {"users": {"@admin:localhost": 100}, "users_default": 0, "state_default": 50},
        },
    ]


@pytest.mark.asyncio
async def test_room_v12_creator_controls_room_without_a_power_level_entry() -> None:
    """Room v12 creators outrank every listed power level, including configured admins."""
    problem = room_control_problem(await _snapshot(_v12_room_events([])), _ROUTER, _LOBBY_ALIAS, [])
    assert problem is None


@pytest.mark.asyncio
async def test_room_v12_unconfigured_co_creator_is_reported() -> None:
    """A room v12 co-creator shares the router's unbounded power."""
    events = _v12_room_events([_SQUATTER, "@admin:localhost"])
    problem = room_control_problem(await _snapshot(events), _ROUTER, _LOBBY_ALIAS, ["@admin:localhost"])
    assert problem == f"users outside the configured admins co-created the room: {_SQUATTER}"


@pytest.mark.asyncio
async def test_room_without_power_levels_is_not_controlled() -> None:
    """Missing power levels cannot prove the router's authority."""
    events = [
        event for event in router_owned_room_events(_ROUTER, _LOBBY_ALIAS) if event["type"] != "m.room.power_levels"
    ]
    assert room_control_problem(await _snapshot(events), _ROUTER, _LOBBY_ALIAS, []) == "power levels are missing"


@pytest.mark.asyncio
async def test_snapshot_records_the_server_stamped_creator() -> None:
    """The creator comes from the create event's sender, never from room content."""
    client = AsyncMock()
    client.room_get_state.return_value = nio.RoomGetStateResponse(
        [{"type": "m.room.create", "state_key": "", "sender": _SQUATTER, "content": {"creator": _ROUTER}}],
        _GENUINE_ROOM,
    )
    snapshot = await read_room_state(client, _GENUINE_ROOM)
    assert snapshot is not None
    assert snapshot.creator == _SQUATTER


@pytest.mark.asyncio
@pytest.mark.parametrize("previously_adopted", [False, True])
async def test_alias_published_by_another_account_is_never_adopted(tmp_path: Path, *, previously_adopted: bool) -> None:
    """A squatted alias leaves the room key unresolved, forgetting any earlier adoption."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    runtime_paths = runtime_paths_for(config)
    if previously_adopted:
        _record_lobby(config, _SQUATTED_ROOM)

    room_id = await _ensure_lobby(_router_client(_SQUATTED_ROOM, _squatted_room_events()), config)

    assert room_id is None
    assert matrix_state.load_rooms(runtime_paths=runtime_paths) == {}
    assert matrix_state.resolve_room_aliases(["lobby"], runtime_paths) == ["lobby"]
    assert not is_configured_room(config, _SQUATTED_ROOM, runtime_paths)
    assert "created by @squatter:localhost" in matrix_rooms.rejected_managed_rooms()[_LOBBY_ALIAS]


@pytest.mark.asyncio
async def test_alias_naming_another_rooms_key_is_never_adopted(tmp_path: Path) -> None:
    """A router-owned room made for another key cannot be adopted through a second alias."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    client = _router_client(_GENUINE_ROOM, router_owned_room_events(_ROUTER, "#dev:localhost"))

    assert await _ensure_lobby(client, config) is None
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config)) == {}
    assert "canonical alias" in matrix_rooms.rejected_managed_rooms()[_LOBBY_ALIAS]


@pytest.mark.asyncio
async def test_unreadable_room_the_router_has_not_joined_is_forgotten(tmp_path: Path) -> None:
    """A room whose state the router cannot read proves nothing and is not kept."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_lobby(config, _SQUATTED_ROOM)

    assert await _ensure_lobby(_router_client(_SQUATTED_ROOM, None), config) is None
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config)) == {}
    assert _LOBBY_ALIAS in matrix_rooms.rejected_managed_rooms()


@pytest.mark.asyncio
async def test_unreadable_joined_room_keeps_its_record(tmp_path: Path) -> None:
    """A joined room is always readable, so a failed read is transient and must not abandon the room."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_lobby(config, _GENUINE_ROOM)
    client = _router_client(_GENUINE_ROOM, None)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse([_GENUINE_ROOM])

    assert await _ensure_lobby(client, config) is None
    assert matrix_state.get_room_id("lobby", runtime_paths_for(config)) == _GENUINE_ROOM
    assert matrix_rooms.rejected_managed_rooms() == {}


@pytest.mark.asyncio
async def test_router_owned_alias_is_adopted(tmp_path: Path) -> None:
    """The router's own room is still recovered by alias when local state is missing."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    client = _router_client(_GENUINE_ROOM, router_owned_room_events(_ROUTER, _LOBBY_ALIAS))

    assert await _ensure_lobby(client, config) == _GENUINE_ROOM
    assert matrix_state.get_room_id("lobby", runtime_paths_for(config)) == _GENUINE_ROOM
    assert matrix_rooms.rejected_managed_rooms() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["power_levels", "encryption", "access"])
async def test_policy_reconciliation_reports_each_unenforced_component(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failing: str,
) -> None:
    """Power levels, encryption, and access policy each gate the reconciliation result."""
    config = membership_config(tmp_path, agent_rooms=["lobby"], room_defaults={"encrypted": True})
    monkeypatch.setattr(matrix_rooms, "ensure_room_has_topic", AsyncMock())
    for name, component in (
        ("ensure_managed_room_power_levels", "power_levels"),
        ("ensure_room_encryption_enabled", "encryption"),
        ("_configure_managed_room_access", "access"),
    ):
        monkeypatch.setattr(matrix_rooms, name, AsyncMock(return_value=component != failing))

    enforced = await matrix_rooms._reconcile_joined_existing_room(
        AsyncMock(),
        "lobby",
        _GENUINE_ROOM,
        config,
        runtime_paths_for(config),
        explicit_room_name=None,
        room_policy=resolve_room_policy(config, "lobby"),
    )

    assert enforced is False


async def _reconcile_with_lobby_policy(config: Config, *, lobby_enforced: bool) -> dict[str, RoomStateSnapshot]:
    """Reconcile a router-joined lobby and dev whose policy application succeeds except, optionally, lobby's."""
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.user_id = _ROUTER
    client.room_get_state.side_effect = lambda room_id: nio.RoomGetStateResponse(
        [{"type": "m.room.member", "state_key": _ROUTER, "content": {"membership": "join"}}],
        room_id,
    )

    async def policy(_client: nio.AsyncClient, room_key: str, *_args: object, **_kwargs: object) -> bool:
        return lobby_enforced or room_key != "lobby"

    with patch.object(matrix_rooms, "_reconcile_joined_existing_room", side_effect=policy):
        return await matrix_rooms.reconcile_managed_rooms(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": _GENUINE_ROOM, "dev": "!dev:localhost"},
        )


@pytest.mark.asyncio
async def test_room_whose_policy_cannot_be_enforced_is_no_longer_managed(tmp_path: Path) -> None:
    """Failed reconciliation removes the room from routing, grants, and invitations."""
    config = membership_config(
        tmp_path,
        agent_rooms=["lobby", "dev"],
        room_defaults={"invite_users": ["@owner:localhost"]},
    )
    runtime_paths = runtime_paths_for(config)
    _record_lobby(config, _GENUINE_ROOM)
    state = matrix_state.MatrixState.load(runtime_paths=runtime_paths)
    state.add_room("dev", "!dev:localhost", "#dev:localhost", "Dev")
    state.save(runtime_paths=runtime_paths)

    snapshots = await _reconcile_with_lobby_policy(config, lobby_enforced=False)

    assert set(snapshots) == {"!dev:localhost"}
    assert not is_configured_room(config, _GENUINE_ROOM, runtime_paths)
    assert is_configured_room(config, "!dev:localhost", runtime_paths)
    assert "policy failed" in matrix_rooms.rejected_managed_rooms()[_LOBBY_ALIAS]

    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = config
    router_bot = MagicMock()
    router_bot.client = AsyncMock()
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot}
    invite = AsyncMock(return_value=True)
    with (
        patch("mindroom.orchestrator.get_joined_rooms", new=AsyncMock(return_value=[_GENUINE_ROOM, "!dev:localhost"])),
        patch("mindroom.orchestrator.get_room_members", new=AsyncMock(return_value={_ROUTER})),
        patch("mindroom.orchestrator.invite_to_room", invite),
    ):
        await orchestrator._ensure_room_invitations()

    assert {call.args[1] for call in invite.await_args_list} == {"!dev:localhost"}


@pytest.mark.asyncio
async def test_room_pass_reports_only_its_own_rejections(tmp_path: Path) -> None:
    """A later pass that adopts the room clears the earlier dashboard rejection."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    await matrix_rooms.ensure_all_rooms_exist(
        _router_client(_SQUATTED_ROOM, _squatted_room_events()),
        config,
        runtime_paths_for(config),
    )
    assert _LOBBY_ALIAS in matrix_rooms.rejected_managed_rooms()

    room_ids = await matrix_rooms.ensure_all_rooms_exist(
        _router_client(_GENUINE_ROOM, router_owned_room_events(_ROUTER, _LOBBY_ALIAS)),
        config,
        runtime_paths_for(config),
    )

    assert room_ids == {"lobby": _GENUINE_ROOM}
    assert matrix_rooms.rejected_managed_rooms() == {}
