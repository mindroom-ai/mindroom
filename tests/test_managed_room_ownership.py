"""Managed rooms are adopted and kept only while the router owns them and enforces their policy."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.access_policy import resolve_room_policy
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import is_configured_room
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix import state as matrix_state
from mindroom.matrix.client_room_admin import room_admin_problem, room_alias_problem, room_ownership_problem
from mindroom.matrix.room_reconciliation import RoomStateSnapshot, read_room_state
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.access_schema_support import membership_config
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.managed_room_helpers import router_owned_room_events

if TYPE_CHECKING:
    from pathlib import Path

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


def _lobby_refusal(snapshot: RoomStateSnapshot, admin_user_ids: list[str]) -> str | None:
    """Return the first problem an unrecorded lobby room would be refused for."""
    return (
        room_ownership_problem(snapshot, _ROUTER)
        or room_alias_problem(snapshot, _LOBBY_ALIAS)
        or room_admin_problem(snapshot, _ROUTER, admin_user_ids)
    )


@pytest.mark.asyncio
async def test_router_owned_room_with_configured_admins_is_controlled() -> None:
    """Configured admins may share the router's power without making the room foreign."""
    events = router_owned_room_events(_ROUTER, _LOBBY_ALIAS, users={"@admin:localhost": 100, "@agent:localhost": 50})
    assert _lobby_refusal(await _snapshot(events), ["@admin:localhost"]) is None


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
                "content": {"alias": "#dev:localhost", "alt_aliases": ["#other:localhost"]},
            },
            "does not publish #lobby:localhost",
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
            "every user holds admin power",
        ),
        (
            {
                "type": "m.room.power_levels",
                "state_key": "",
                "sender": _ROUTER,
                "content": {"users": {_ROUTER: 100, "@rival:localhost": "100"}},
            },
            "non-integer",
        ),
        (
            {
                "type": "m.room.power_levels",
                "state_key": "",
                "sender": _ROUTER,
                "content": {"users": {_ROUTER: 100}, "state_default": True},
            },
            "non-integer",
        ),
    ],
)
async def test_room_the_router_does_not_control_is_reported(override: dict[str, object], expected: str) -> None:
    """Creator, published alias, membership, integer power levels, and unmatched admin power are each required."""
    events = [*router_owned_room_events(_ROUTER, _LOBBY_ALIAS), override]
    problem = _lobby_refusal(await _snapshot(events), ["@admin:localhost"])
    assert problem is not None
    assert expected in problem


@pytest.mark.asyncio
async def test_room_still_publishing_the_alias_as_an_alternative_is_owned() -> None:
    """An admin may change the room's main address without orphaning the managed room."""
    events = [
        *router_owned_room_events(_ROUTER, _LOBBY_ALIAS),
        {
            "type": "m.room.canonical_alias",
            "state_key": "",
            "sender": _ROUTER,
            "content": {"alias": "#friendly:localhost", "alt_aliases": [_LOBBY_ALIAS]},
        },
    ]
    assert _lobby_refusal(await _snapshot(events), []) is None


def _v12_room_events(additional_creators: list[str], users: dict[str, int] | None = None) -> list[dict[str, object]]:
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
            "content": {"users": users or {"@admin:localhost": 100}, "users_default": 0, "state_default": 50},
        },
    ]


@pytest.mark.asyncio
async def test_room_v12_creator_controls_room_without_a_power_level_entry() -> None:
    """Room v12 creators outrank every listed power level, so the router needs no power-level entry."""
    problem = _lobby_refusal(await _snapshot(_v12_room_events([])), ["@admin:localhost"])
    assert problem is None


@pytest.mark.asyncio
async def test_room_v12_unconfigured_admin_is_reported() -> None:
    """Admin power outside the configured admins disqualifies a room in every room version."""
    events = _v12_room_events([], users={"@removed-admin:localhost": 100})
    problem = _lobby_refusal(await _snapshot(events), [])
    assert problem == "users outside the configured admins hold admin power: @removed-admin:localhost"


@pytest.mark.asyncio
async def test_room_v12_unconfigured_co_creator_is_reported() -> None:
    """A room v12 co-creator shares the router's unbounded power."""
    events = _v12_room_events([_SQUATTER, "@admin:localhost"])
    problem = _lobby_refusal(await _snapshot(events), ["@admin:localhost"])
    assert problem == f"users outside the configured admins hold admin power: {_SQUATTER}"


@pytest.mark.asyncio
async def test_room_without_power_levels_is_not_controlled() -> None:
    """Missing power levels cannot prove the router's authority."""
    events = [
        event for event in router_owned_room_events(_ROUTER, _LOBBY_ALIAS) if event["type"] != "m.room.power_levels"
    ]
    assert _lobby_refusal(await _snapshot(events), []) == "power levels are missing"


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
    assert matrix_rooms.router_retained_room_ids() == set()


@pytest.mark.asyncio
async def test_router_keeps_its_own_refused_room(tmp_path: Path) -> None:
    """An unrecorded router room with unconfigured admins is refused, but the router stays for a later fix."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    events = router_owned_room_events(_ROUTER, _LOBBY_ALIAS, users={"@removed-admin:localhost": 100})

    assert await _ensure_lobby(_router_client(_GENUINE_ROOM, events), config) is None
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config)) == {}
    assert matrix_rooms.router_retained_room_ids() == {_GENUINE_ROOM}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    [
        {
            "type": "m.room.power_levels",
            "state_key": "",
            "content": {"users": {_ROUTER: 100, "@removed:localhost": 100}},
        },
        {"type": "m.room.canonical_alias", "state_key": "", "content": {"alias": "#renamed:localhost"}},
    ],
)
async def test_recorded_room_drift_is_reported_not_refused(tmp_path: Path, drift: dict[str, object]) -> None:
    """A removed admin or a renamed alias in the recorded room is reported, since the router still owns it."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_lobby(config, _GENUINE_ROOM)
    events = [*router_owned_room_events(_ROUTER, _LOBBY_ALIAS), drift]

    assert await _ensure_lobby(_router_client(_GENUINE_ROOM, events), config) == _GENUINE_ROOM
    assert matrix_state.get_room_id("lobby", runtime_paths_for(config)) == _GENUINE_ROOM
    assert matrix_rooms.rejected_managed_rooms() == {}


@pytest.mark.asyncio
async def test_alias_naming_another_rooms_key_is_never_adopted(tmp_path: Path) -> None:
    """A router-owned room made for another key cannot be adopted through a second alias."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    client = _router_client(_GENUINE_ROOM, router_owned_room_events(_ROUTER, "#dev:localhost"))

    assert await _ensure_lobby(client, config) is None
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config)) == {}
    assert "does not publish" in matrix_rooms.rejected_managed_rooms()[_LOBBY_ALIAS]


@pytest.mark.asyncio
async def test_unreadable_room_the_router_has_not_joined_is_forgotten(tmp_path: Path) -> None:
    """A room whose state the router cannot read proves nothing and is not kept."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_lobby(config, _SQUATTED_ROOM)

    assert await _ensure_lobby(_router_client(_SQUATTED_ROOM, None), config) is None
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config)) == {}
    assert _LOBBY_ALIAS in matrix_rooms.rejected_managed_rooms()


@pytest.mark.asyncio
@pytest.mark.parametrize("joined_rooms", [nio.JoinedRoomsResponse([_GENUINE_ROOM]), nio.JoinedRoomsError("down")])
async def test_unreadable_verified_room_keeps_its_record(tmp_path: Path, joined_rooms: nio.Response) -> None:
    """A failed read of a room this process verified is transient and must not abandon the room."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    assert await _ensure_lobby(_router_client(_GENUINE_ROOM, router_owned_room_events(_ROUTER, _LOBBY_ALIAS)), config)
    client = _router_client(_GENUINE_ROOM, None)
    client.joined_rooms.return_value = joined_rooms

    assert await _ensure_lobby(client, config) is None
    assert matrix_state.get_room_id("lobby", runtime_paths_for(config)) == _GENUINE_ROOM
    assert matrix_rooms.rejected_managed_rooms() == {}


def _record_legacy_lobby(config: Config, room_id: str) -> None:
    """Write a lobby record the way releases before ownership checks did, without router_verified."""
    state_file = matrix_state.constants.matrix_state_file(runtime_paths=runtime_paths_for(config))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        f"rooms:\n  lobby:\n    room_id: '{room_id}'\n    alias: '{_LOBBY_ALIAS}'\n    name: Lobby\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_unreadable_room_recorded_before_ownership_checks_is_forgotten(tmp_path: Path) -> None:
    """A record adopted before ownership checks existed is not trusted just because its state is unreadable."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_legacy_lobby(config, _SQUATTED_ROOM)
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config))["lobby"].router_verified is False
    client = _router_client(_SQUATTED_ROOM, None)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse([_SQUATTED_ROOM])

    assert await _ensure_lobby(client, config) is None
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config)) == {}
    assert "unreadable" in matrix_rooms.rejected_managed_rooms()[_LOBBY_ALIAS]


@pytest.mark.asyncio
async def test_legacy_record_of_the_routers_room_becomes_verified(tmp_path: Path) -> None:
    """Re-verifying a record written before ownership checks persists the verification."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_legacy_lobby(config, _GENUINE_ROOM)
    client = _router_client(_GENUINE_ROOM, router_owned_room_events(_ROUTER, _LOBBY_ALIAS))

    assert await _ensure_lobby(client, config) == _GENUINE_ROOM
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config))["lobby"].router_verified is True


@pytest.mark.asyncio
async def test_router_owned_alias_is_adopted(tmp_path: Path) -> None:
    """The router's own room is still recovered by alias when local state is missing."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    client = _router_client(_GENUINE_ROOM, router_owned_room_events(_ROUTER, _LOBBY_ALIAS))

    assert await _ensure_lobby(client, config) == _GENUINE_ROOM
    assert matrix_state.get_room_id("lobby", runtime_paths_for(config)) == _GENUINE_ROOM
    assert matrix_rooms.rejected_managed_rooms() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("failing", "protected"), [("power_levels", True), ("encryption", False), ("access", False)])
async def test_policy_reconciliation_gates_on_protection_not_power_levels(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failing: str,
    *,
    protected: bool,
) -> None:
    """Encryption and access gate reconciliation; a failed power-level write only leaves the room stricter."""
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

    assert enforced is protected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_join_rule", "listed", "join_rule_ok", "visibility_ok", "protected"),
    [
        ("public", False, False, True, False),
        ("invite", False, True, False, False),
        ("invite", True, True, False, True),
        (None, False, False, True, False),
    ],
)
async def test_failed_access_write_gates_only_when_the_room_stays_more_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    current_join_rule: str | None,
    *,
    listed: bool,
    join_rule_ok: bool,
    visibility_ok: bool,
    protected: bool,
) -> None:
    """Failing to publish or open a room leaves it stricter; failing to close or unpublish it does not."""
    config = membership_config(tmp_path, agent_rooms=["lobby"], room_defaults={"listed": listed})
    monkeypatch.setattr(matrix_rooms, "ensure_room_join_rule", AsyncMock(return_value=join_rule_ok))
    monkeypatch.setattr(matrix_rooms, "ensure_room_directory_visibility", AsyncMock(return_value=visibility_ok))
    events = {} if current_join_rule is None else {("m.room.join_rules", ""): {"join_rule": current_join_rule}}

    result = await matrix_rooms._configure_managed_room_access(
        client=AsyncMock(),
        room_key="lobby",
        room_id=_GENUINE_ROOM,
        room_policy=resolve_room_policy(config, "lobby"),
        context="test",
        snapshot=RoomStateSnapshot(_GENUINE_ROOM, events),
    )

    assert result is protected


@pytest.mark.asyncio
async def test_policy_enforcement_error_forgets_the_room(tmp_path: Path) -> None:
    """An exception part-way through enforcement leaves protection unknown, so the room is refused."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_lobby(config, _GENUINE_ROOM)
    client = _router_client(
        _GENUINE_ROOM,
        [{"type": "m.room.member", "state_key": _ROUTER, "content": {"membership": "join"}}],
    )

    with patch.object(matrix_rooms, "_reconcile_joined_existing_room", side_effect=TimeoutError):
        snapshots = await matrix_rooms.reconcile_managed_rooms(
            client,
            config,
            runtime_paths_for(config),
            {"lobby": _GENUINE_ROOM},
        )

    assert snapshots == {}
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config)) == {}
    assert "raised" in matrix_rooms.rejected_managed_rooms()[_LOBBY_ALIAS]


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
    assert "less protected" in matrix_rooms.rejected_managed_rooms()[_LOBBY_ALIAS]

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


@pytest.mark.asyncio
async def test_router_leaves_squatted_rooms_but_not_its_own_refused_rooms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup keeps the router only in refused rooms it may have created."""
    config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
    router = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id=_ROUTER,
            display_name="Router",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    router.client = AsyncMock()
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=[_GENUINE_ROOM, _SQUATTED_ROOM]),
    )
    matrix_rooms._reject_managed_room(_LOBBY_ALIAS, _GENUINE_ROOM, "rival admin", router_stays=True)
    matrix_rooms._reject_managed_room("#dev:localhost", _SQUATTED_ROOM, "created by squatter")

    assert await router._room_lifecycle._rooms_to_leave() == [_SQUATTED_ROOM]


@pytest.mark.asyncio
async def test_agents_skip_rooms_whose_policy_reconciliation_failed(tmp_path: Path) -> None:
    """Bots resolve their rooms again after reconciliation forgets a room, so they never join it."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_lobby(config, _GENUINE_ROOM)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config
    router_bot = AsyncMock()
    router_bot.agent_name = ROUTER_AGENT_NAME
    talent_bot = AsyncMock()
    talent_bot.agent_name = "talent"
    rooms_when_talent_joins: list[list[str]] = []
    talent_bot.ensure_rooms.side_effect = lambda: rooms_when_talent_joins.append(list(talent_bot.rooms))

    async def reconcile(_room_ids: dict[str, str]) -> dict[str, RoomStateSnapshot]:
        matrix_rooms._remove_room("lobby", runtime_paths_for(config))
        return {}

    with (
        patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock(return_value={"lobby": _GENUINE_ROOM})),
        patch.object(orchestrator, "_ensure_root_space", new=AsyncMock()),
        patch.object(orchestrator, "_reconcile_managed_rooms", new=AsyncMock(side_effect=reconcile)),
        patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
        patch.object(orchestrator, "refresh_agent_reply_memberships", new=AsyncMock()),
    ):
        await orchestrator._setup_rooms_and_memberships([router_bot, talent_bot])

    assert router_bot.rooms == ["lobby"]
    assert rooms_when_talent_joins == [["lobby"]]


@pytest.mark.asyncio
async def test_internal_user_joins_only_configured_room_records(tmp_path: Path) -> None:
    """A record left under a removed room key is never re-verified, so the internal user skips it."""
    config = bind_runtime_paths(
        Config(
            agents={"talent": {"display_name": "Talent", "rooms": ["lobby"]}},
            mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    _record_lobby(config, _GENUINE_ROOM)
    state = matrix_state.MatrixState.load(runtime_paths=runtime_paths)
    state.add_room("retired", _SQUATTED_ROOM, "#retired:localhost", "Retired")
    state.save(runtime_paths=runtime_paths)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = config

    with (
        patch.object(orchestrator, "_ensure_rooms_exist", new=AsyncMock(return_value={"lobby": _GENUINE_ROOM})),
        patch.object(orchestrator, "_ensure_root_space", new=AsyncMock()),
        patch.object(orchestrator, "_reconcile_managed_rooms", new=AsyncMock(return_value={})),
        patch.object(orchestrator, "_ensure_room_invitations", new=AsyncMock()),
        patch.object(orchestrator, "refresh_agent_reply_memberships", new=AsyncMock()),
        patch("mindroom.orchestrator.ensure_user_in_rooms", new=AsyncMock()) as ensure_user,
    ):
        await orchestrator._setup_rooms_and_memberships([])

    assert ensure_user.await_args.args[1] == {"lobby": _GENUINE_ROOM}


@pytest.mark.asyncio
async def test_refused_room_the_router_stays_in_gets_no_invitations(tmp_path: Path) -> None:
    """Not even the internal user is invited into a refused room the router remains joined to."""
    config = bind_runtime_paths(
        Config(
            agents={"talent": {"display_name": "Talent", "rooms": ["lobby"]}},
            mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
        ),
        test_runtime_paths(tmp_path),
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config
    router_bot = MagicMock()
    router_bot.client = AsyncMock()
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot}
    matrix_rooms._reject_managed_room(_LOBBY_ALIAS, _GENUINE_ROOM, "rival admin", router_stays=True)
    invite = AsyncMock(return_value=True)

    with (
        patch(
            "mindroom.orchestrator.get_joined_rooms",
            new=AsyncMock(return_value=[_GENUINE_ROOM, "!adhoc:localhost"]),
        ),
        patch("mindroom.orchestrator.get_room_members", new=AsyncMock(return_value={_ROUTER})),
        patch("mindroom.orchestrator.invite_to_room", invite),
    ):
        await orchestrator._ensure_room_invitations()

    assert {call.args[1] for call in invite.await_args_list} == {"!adhoc:localhost"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("forgets_record", "expected_invalidations"), [(True, 1), (False, 0)])
async def test_forgetting_a_managed_room_revokes_room_grants_at_once(
    tmp_path: Path,
    *,
    forgets_record: bool,
    expected_invalidations: int,
) -> None:
    """Members of a forgotten room lose reply grants before the pass's closing refresh."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_lobby(config, _GENUINE_ROOM)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths_for(config))
    orchestrator.config = config
    router_bot = MagicMock()
    router_bot.client = AsyncMock()
    orchestrator.agent_bots = {ROUTER_AGENT_NAME: router_bot}

    async def ensure_rooms(*_args: object) -> dict[str, str]:
        if forgets_record:
            matrix_rooms._remove_room("lobby", runtime_paths_for(config))
        return {}

    with (
        patch("mindroom.orchestrator.ensure_all_rooms_exist", new=AsyncMock(side_effect=ensure_rooms)),
        patch.object(orchestrator, "invalidate_agent_reply_memberships") as invalidate,
    ):
        await orchestrator._ensure_rooms_exist()

    assert invalidate.call_count == expected_invalidations


@pytest.mark.asyncio
async def test_legacy_record_naming_another_keys_room_is_refused(tmp_path: Path) -> None:
    """A record adopted before ownership checks through another key's alias is refused on upgrade."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_legacy_lobby(config, _GENUINE_ROOM)
    client = _router_client(_GENUINE_ROOM, router_owned_room_events(_ROUTER, "#dev:localhost"))

    assert await _ensure_lobby(client, config) is None
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config)) == {}
    assert "does not publish" in matrix_rooms.rejected_managed_rooms()[_LOBBY_ALIAS]


@pytest.mark.asyncio
async def test_legacy_record_with_leftover_admins_stays_managed(tmp_path: Path) -> None:
    """Admins left over from earlier configuration are reported, not refused, when the router owns the room."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    _record_legacy_lobby(config, _GENUINE_ROOM)
    events = router_owned_room_events(_ROUTER, _LOBBY_ALIAS, users={"@former-admin:localhost": 100})

    assert await _ensure_lobby(_router_client(_GENUINE_ROOM, events), config) == _GENUINE_ROOM
    assert matrix_state.load_rooms(runtime_paths=runtime_paths_for(config))["lobby"].router_verified is True
    assert matrix_rooms.rejected_managed_rooms() == {}


@pytest.mark.asyncio
async def test_room_without_power_levels_is_refused_without_error(tmp_path: Path) -> None:
    """Admin drift is only evaluated for an owned room, so missing power levels refuse cleanly."""
    config = membership_config(tmp_path, agent_rooms=["lobby"])
    events = [
        event for event in router_owned_room_events(_ROUTER, _LOBBY_ALIAS) if event["type"] != "m.room.power_levels"
    ]

    assert await _ensure_lobby(_router_client(_GENUINE_ROOM, events), config) is None
    assert "power levels are missing" in matrix_rooms.rejected_managed_rooms()[_LOBBY_ALIAS]
