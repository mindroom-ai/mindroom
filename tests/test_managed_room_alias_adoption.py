"""Tests for adopting managed rooms and the root Space from their aliases."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import nio
import pytest
from nio.responses import RoomPutAliasError

from mindroom.access_policy import resolve_room_policy
from mindroom.matrix import rooms as matrix_rooms
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.state import MatrixState, load_rooms
from mindroom.matrix_identifiers import managed_room_alias_localpart, managed_space_alias_localpart
from tests.access_schema_support import membership_config
from tests.conftest import runtime_paths_for

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config

_ROUTER = "@router:localhost"
_SQUATTER = "@squatter:localhost"
_OURS = "!ours:localhost"
_THEIRS = "!theirs:localhost"
_FRESH = "!fresh:localhost"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Return a config with one managed room and the default root Space."""
    return membership_config(tmp_path, agent_rooms=["lobby"])


@pytest.fixture
def create_room(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Stub managed-room creation and its cosmetic follow-ups."""
    create = AsyncMock(return_value=_FRESH)
    monkeypatch.setattr(matrix_rooms, "create_room", create)
    monkeypatch.setattr(matrix_rooms, "generate_room_topic_ai", AsyncMock(return_value="topic"))
    monkeypatch.setattr(matrix_rooms, "_configure_managed_room_access", AsyncMock(return_value=True))
    monkeypatch.setattr(matrix_rooms, "_set_room_avatar_if_available", AsyncMock())
    return create


@pytest.fixture
def space_calls(monkeypatch: pytest.MonkeyPatch) -> tuple[AsyncMock, AsyncMock]:
    """Stub root Space creation and joins, returning both mocks."""
    create = AsyncMock(return_value=_FRESH)
    join = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(matrix_rooms, "create_space", create)
    monkeypatch.setattr(matrix_rooms, "join_room", join)
    monkeypatch.setattr(matrix_rooms, "get_joined_rooms", AsyncMock(return_value=[]))
    return create, join


def _lobby_alias(config: Config) -> str:
    return f"#{managed_room_alias_localpart('lobby', runtime_paths_for(config))}:localhost"


def _space_alias(config: Config) -> str:
    return f"#{managed_space_alias_localpart(runtime_paths_for(config))}:localhost"


def _client(alias: str, alias_room_id: str) -> AsyncMock:
    client = AsyncMock()
    client.homeserver = "http://localhost:8008"
    client.user_id = _ROUTER
    client.rooms = {}
    client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(alias, alias_room_id, ["localhost"])
    return client


def _room_state(
    room_id: str,
    alias: str,
    *,
    creator: str = _ROUTER,
    additional_creators: list[str] | None = None,
    canonical_sender: str | None = None,
    canonical_content: dict[str, object] | None = None,
    membership: str = "join",
) -> nio.RoomGetStateResponse:
    create_content: dict[str, object] = {"room_version": "12"}
    if additional_creators is not None:
        create_content["additional_creators"] = additional_creators
    return nio.RoomGetStateResponse(
        [
            {"type": "m.room.create", "state_key": "", "sender": creator, "content": create_content},
            {
                "type": "m.room.canonical_alias",
                "state_key": "",
                "sender": canonical_sender or creator,
                "content": canonical_content or {"alias": alias},
            },
            {"type": "m.room.member", "state_key": _ROUTER, "sender": _ROUTER, "content": {"membership": membership}},
        ],
        room_id,
    )


def _record_lobby(config: Config, room_id: str) -> None:
    state = MatrixState.load(runtime_paths_for(config))
    state.add_room("lobby", room_id, _lobby_alias(config), "Lobby")
    state.save(runtime_paths_for(config))


def _recorded_lobby(config: Config) -> str | None:
    room = load_rooms(runtime_paths_for(config)).get("lobby")
    return room.room_id if room is not None else None


async def _ensure_lobby(client: AsyncMock, config: Config) -> str | None:
    return await matrix_rooms._ensure_room_exists(
        client=client,
        room_key="lobby",
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_policy=resolve_room_policy(config, "lobby"),
    )


@pytest.mark.asyncio
async def test_recorded_room_is_kept_when_alias_points_elsewhere(config: Config, create_room: AsyncMock) -> None:
    """A repointed alias never replaces the recorded room, even one the router created."""
    _record_lobby(config, _OURS)
    client = _client(_lobby_alias(config), _THEIRS)
    client.room_get_state.return_value = _room_state(_THEIRS, _lobby_alias(config))

    assert await _ensure_lobby(client, config) == _OURS

    assert _recorded_lobby(config) == _OURS
    create_room.assert_not_awaited()
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrecorded_alias_target_created_by_router_is_adopted(config: Config, create_room: AsyncMock) -> None:
    """Lost local state recovers the router's own room from its alias."""
    client = _client(_lobby_alias(config), _OURS)
    client.room_get_state.return_value = _room_state(_OURS, _lobby_alias(config), additional_creators=[])

    assert await _ensure_lobby(client, config) == _OURS

    assert _recorded_lobby(config) == _OURS
    create_room.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state_response",
    [
        pytest.param(lambda alias: _room_state(_THEIRS, alias, creator=_SQUATTER), id="squatter-room"),
        pytest.param(
            lambda alias: _room_state(_THEIRS, alias, creator=_SQUATTER, canonical_sender=_ROUTER),
            id="other-creator-with-router-alias-event",
        ),
        pytest.param(
            lambda alias: _room_state(_THEIRS, alias, additional_creators=[_SQUATTER]),
            id="other-additional-creator",
        ),
        pytest.param(lambda _alias: _room_state(_THEIRS, "#dev:localhost"), id="router-room-of-another-alias"),
        pytest.param(
            lambda alias: _room_state(_THEIRS, alias, canonical_sender=_SQUATTER),
            id="canonical-alias-set-by-another-member",
        ),
        pytest.param(
            lambda alias: _room_state(
                _THEIRS,
                alias,
                canonical_content={"alias": "#dev:localhost", "alt_aliases": [alias]},
            ),
            id="alias-only-in-alt-aliases",
        ),
        pytest.param(
            lambda _alias: nio.RoomGetStateError("not in room", "M_FORBIDDEN", room_id=_THEIRS),
            id="state-denied",
        ),
        pytest.param(
            lambda _alias: nio.RoomGetStateError("unknown room", "M_NOT_FOUND", room_id=_THEIRS),
            id="state-not-found",
        ),
    ],
)
async def test_unrecorded_alias_target_not_created_by_router_gets_a_fresh_room(
    config: Config,
    create_room: AsyncMock,
    state_response: Callable[[str], nio.RoomGetStateResponse | nio.RoomGetStateError],
) -> None:
    """A squatted alias is refused, and a fresh room without it is created, recorded, and kept."""
    alias = _lobby_alias(config)
    client = _client(alias, _THEIRS)
    client.room_get_state.return_value = state_response(alias)

    assert await _ensure_lobby(client, config) == _FRESH

    assert _recorded_lobby(config) == _FRESH
    create_room.assert_awaited_once()
    assert create_room.await_args.kwargs["alias"] is None
    client.join.assert_not_awaited()

    assert await _ensure_lobby(client, config) == _FRESH

    assert _recorded_lobby(config) == _FRESH
    create_room.assert_awaited_once()
    client.room_get_state.assert_awaited_once_with(_THEIRS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(aiohttp.ClientConnectionError("reset"), id="network"),
        pytest.param(TimeoutError(), id="timeout"),
        pytest.param(nio.RoomGetStateError("slow down", "M_LIMIT_EXCEEDED", room_id=_OURS), id="rate-limited"),
        pytest.param(nio.RoomGetStateError("unknown error", room_id=_OURS), id="server-error"),
    ],
)
async def test_transient_alias_target_read_failure_neither_adopts_nor_creates(
    config: Config,
    create_room: AsyncMock,
    failure: BaseException | nio.RoomGetStateError,
) -> None:
    """An unreadable alias target leaves the key unresolved until a later pass can verify it."""
    alias = _lobby_alias(config)
    client = _client(alias, _OURS)
    if isinstance(failure, BaseException):
        client.room_get_state.side_effect = failure
    else:
        client.room_get_state.return_value = failure

    assert await _ensure_lobby(client, config) is None

    assert _recorded_lobby(config) is None
    create_room.assert_not_awaited()

    client.room_get_state.side_effect = None
    client.room_get_state.return_value = _room_state(_OURS, alias)
    assert await _ensure_lobby(client, config) == _OURS
    assert _recorded_lobby(config) == _OURS


@pytest.mark.asyncio
@pytest.mark.parametrize(("recorded", "expected"), [(_OURS, _OURS), (None, None)])
async def test_transient_alias_lookup_failure_never_forgets_or_creates(
    config: Config,
    create_room: AsyncMock,
    recorded: str | None,
    expected: str | None,
) -> None:
    """A failed alias lookup keeps any recorded room and otherwise waits for a later pass."""
    if recorded is not None:
        _record_lobby(config, recorded)
    client = _client(_lobby_alias(config), _OURS)
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("unknown error")

    assert await _ensure_lobby(client, config) == expected

    assert _recorded_lobby(config) == expected
    create_room.assert_not_awaited()
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "put_alias_response",
    [nio.RoomPutAliasResponse("#lobby:localhost", _OURS), RoomPutAliasError("taken", "M_UNKNOWN")],
    ids=["republished", "retaken"],
)
async def test_deleted_alias_of_recorded_room_is_republished_without_leaving_the_room(
    config: Config,
    create_room: AsyncMock,
    put_alias_response: nio.RoomPutAliasResponse | RoomPutAliasError,
) -> None:
    """Whoever deletes a managed alias cannot move MindRoom off the recorded room it is still joined to."""
    _record_lobby(config, _OURS)
    alias = _lobby_alias(config)
    client = _client(alias, _OURS)
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("missing", "M_NOT_FOUND")
    client.room_get_state.return_value = _room_state(_OURS, alias, creator=_SQUATTER)
    client.room_put_alias.return_value = put_alias_response

    assert await _ensure_lobby(client, config) == _OURS

    assert _recorded_lobby(config) == _OURS
    client.room_put_alias.assert_awaited_once_with(alias, _OURS)
    create_room.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state_response",
    [
        pytest.param(nio.RoomGetStateError("not in room", "M_FORBIDDEN", room_id=_OURS), id="state-denied"),
        pytest.param(_room_state(_OURS, "#lobby:localhost", membership="leave"), id="router-left"),
    ],
)
async def test_deleted_alias_of_lost_recorded_room_creates_an_aliased_room(
    config: Config,
    create_room: AsyncMock,
    state_response: nio.RoomGetStateResponse | nio.RoomGetStateError,
) -> None:
    """A recorded room the router can no longer use is replaced by a new room with the managed alias."""
    _record_lobby(config, _OURS)
    client = _client(_lobby_alias(config), _OURS)
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("missing", "M_NOT_FOUND")
    client.room_get_state.return_value = state_response

    assert await _ensure_lobby(client, config) == _FRESH

    assert _recorded_lobby(config) == _FRESH
    assert create_room.await_args.kwargs["alias"] == managed_room_alias_localpart("lobby", runtime_paths_for(config))
    client.room_put_alias.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_alias_of_recorded_room_is_kept_through_a_transient_read_failure(
    config: Config,
    create_room: AsyncMock,
) -> None:
    """An unreadable recorded room keeps its record, and the alias is republished on a later pass."""
    _record_lobby(config, _OURS)
    client = _client(_lobby_alias(config), _OURS)
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("missing", "M_NOT_FOUND")
    client.room_get_state.return_value = nio.RoomGetStateError("slow down", "M_LIMIT_EXCEEDED", room_id=_OURS)

    assert await _ensure_lobby(client, config) == _OURS

    assert _recorded_lobby(config) == _OURS
    client.room_put_alias.assert_not_awaited()
    create_room.assert_not_awaited()


async def _ensure_space(client: AsyncMock, config: Config) -> str | None:
    return await matrix_rooms._ensure_root_space_exists(client, config, runtime_paths_for(config))


def _recorded_space(config: Config) -> str | None:
    return MatrixState.load(runtime_paths_for(config)).space_room_id


@pytest.mark.asyncio
async def test_recorded_root_space_is_kept_when_alias_points_elsewhere(
    config: Config,
    space_calls: tuple[AsyncMock, AsyncMock],
) -> None:
    """A repointed Space alias never replaces or joins anything but the recorded Space."""
    create_space, join = space_calls
    state = MatrixState.load(runtime_paths_for(config))
    state.set_space_room_id(_OURS)
    state.save(runtime_paths_for(config))
    client = _client(_space_alias(config), _THEIRS)

    assert await _ensure_space(client, config) == _OURS

    assert _recorded_space(config) == _OURS
    join.assert_awaited_once_with(client, _OURS)
    create_space.assert_not_awaited()
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_unrecorded_root_space_created_by_router_is_adopted(
    config: Config,
    space_calls: tuple[AsyncMock, AsyncMock],
) -> None:
    """Lost local state recovers the router's own Space from its alias."""
    create_space, join = space_calls
    client = _client(_space_alias(config), _OURS)
    client.room_get_state.return_value = _room_state(_OURS, _space_alias(config))

    assert await _ensure_space(client, config) == _OURS

    assert _recorded_space(config) == _OURS
    join.assert_awaited_once_with(client, _OURS)
    create_space.assert_not_awaited()


@pytest.mark.asyncio
async def test_squatted_root_space_alias_gets_a_fresh_space(
    config: Config,
    space_calls: tuple[AsyncMock, AsyncMock],
) -> None:
    """A Space alias held by another account is refused, and a fresh Space without it is kept."""
    create_space, join = space_calls
    client = _client(_space_alias(config), _THEIRS)
    client.room_get_state.return_value = _room_state(_THEIRS, _space_alias(config), creator=_SQUATTER)

    assert await _ensure_space(client, config) == _FRESH

    assert _recorded_space(config) == _FRESH
    create_space.assert_awaited_once()
    assert create_space.await_args.kwargs["alias"] is None
    join.assert_not_awaited()

    assert await _ensure_space(client, config) == _FRESH

    assert _recorded_space(config) == _FRESH
    create_space.assert_awaited_once()
    join.assert_awaited_once_with(client, _FRESH)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_response", "expected"),
    [
        pytest.param(_room_state(_OURS, "#space:localhost"), _OURS, id="still-joined"),
        pytest.param(nio.RoomGetStateError("not in room", "M_FORBIDDEN", room_id=_OURS), _FRESH, id="lost"),
    ],
)
async def test_deleted_alias_of_recorded_root_space(
    config: Config,
    space_calls: tuple[AsyncMock, AsyncMock],
    state_response: nio.RoomGetStateResponse | nio.RoomGetStateError,
    expected: str,
) -> None:
    """A deleted Space alias is republished on a Space the router still holds, and recreated otherwise."""
    create_space, _join = space_calls
    state = MatrixState.load(runtime_paths_for(config))
    state.set_space_room_id(_OURS)
    state.save(runtime_paths_for(config))
    alias = _space_alias(config)
    client = _client(alias, _OURS)
    client.room_resolve_alias.return_value = nio.RoomResolveAliasError("missing", "M_NOT_FOUND")
    client.room_get_state.return_value = state_response
    client.room_put_alias.return_value = nio.RoomPutAliasResponse(alias, _OURS)

    assert await _ensure_space(client, config) == expected

    assert _recorded_space(config) == expected
    if expected == _OURS:
        client.room_put_alias.assert_awaited_once_with(alias, _OURS)
        create_space.assert_not_awaited()
    else:
        client.room_put_alias.assert_not_awaited()
        assert create_space.await_args.kwargs["alias"] == managed_space_alias_localpart(runtime_paths_for(config))


@pytest.mark.asyncio
async def test_transient_root_space_read_failure_neither_adopts_nor_creates(
    config: Config,
    space_calls: tuple[AsyncMock, AsyncMock],
) -> None:
    """An unreadable Space alias target is retried on a later pass."""
    create_space, join = space_calls
    client = _client(_space_alias(config), _OURS)
    client.room_get_state.return_value = nio.RoomGetStateError("slow down", "M_LIMIT_EXCEEDED", room_id=_OURS)

    assert await _ensure_space(client, config) is None

    assert _recorded_space(config) is None
    create_space.assert_not_awaited()
    join.assert_not_awaited()
