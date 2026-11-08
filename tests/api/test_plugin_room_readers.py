"""Plugin routes read a room's members' names, its power levels, and whether it is a direct message.

Each read happens only once the caller is found joined to the room, through a running bot in the room, the router
first, and answers 503 rather than a guess when Matrix cannot answer.
"""

import asyncio
import math
from collections.abc import Awaitable, Iterator
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock
from urllib.parse import quote

import nio
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import _client_session_store
from mindroom.matrix import rooms
from mindroom.matrix.rooms import is_dm_room
from mindroom.orchestration import plugin_http_runtime
from mindroom.orchestration.plugin_http_runtime import bind_room_members, unbind_room_members
from mindroom.plugin_http import ClientUser, RoomPower, RoomReader, client_rooms, require_room_reader

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
THIRD_ROOM = "!third:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"
CAROL = "@carol:example.org"
ROUTER = "@mindroom_router:example.org"


@pytest.fixture(autouse=True)
def _fresh_dm_caches() -> Iterator[None]:
    """Core's direct-message rule caches its answers per bot and room; each test starts without them."""
    rooms._DM_ROOM_CACHE.clear()
    rooms._DIRECT_ROOMS_CACHE.clear()
    yield
    rooms._DM_ROOM_CACHE.clear()
    rooms._DIRECT_ROOMS_CACHE.clear()


def _joined(room_id: str, names: dict[str, str | None]) -> nio.JoinedMembersResponse:
    """A joined-members answer listing each user with the display name they have in the room."""
    return nio.JoinedMembersResponse(
        members=[nio.RoomMember(user_id, name, None) for user_id, name in names.items()],
        room_id=room_id,
    )


def _state(
    room_id: str = ROOM,
    *,
    power_levels: dict[str, object] | None = None,
    room_version: str = "10",
    additional_creators: tuple[str, ...] = (),
    encrypted: bool = False,
    members: dict[str, dict[str, object]] | None = None,
) -> nio.RoomGetStateResponse:
    """A full state read of a room Alice created that anyone may join and whose history is world readable."""
    create: dict[str, object] = {"room_version": room_version}
    if additional_creators:
        create["additional_creators"] = list(additional_creators)
    events: list[dict[str, object]] = [
        {"type": "m.room.create", "state_key": "", "sender": ALICE, "content": create},
        {"type": "m.room.join_rules", "state_key": "", "sender": ALICE, "content": {"join_rule": "public"}},
        {
            "type": "m.room.history_visibility",
            "state_key": "",
            "sender": ALICE,
            "content": {"history_visibility": "world_readable"},
        },
        {
            "type": "m.room.power_levels",
            "state_key": "",
            "sender": ALICE,
            "content": power_levels if power_levels is not None else {"users": {ALICE: 100}},
        },
    ]
    if encrypted:
        events.append(
            {
                "type": "m.room.encryption",
                "state_key": "",
                "sender": ALICE,
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            },
        )
    for user_id, content in (members or {}).items():
        events.append({"type": "m.room.member", "state_key": user_id, "sender": user_id, "content": content})
    return nio.RoomGetStateResponse(events, room_id)


def _client(
    names: dict[str, str | None] | None = None,
    *,
    state: nio.RoomGetStateResponse | None = None,
    joined_rooms: tuple[str, ...] = (ROOM,),
) -> SimpleNamespace:
    """The router's Matrix client, joined to ``joined_rooms``, which answers ``names`` as the members of ``ROOM``."""
    return SimpleNamespace(
        user_id=ROUTER,
        rooms={room_id: nio.MatrixRoom(room_id, ROUTER) for room_id in joined_rooms},
        joined_members=AsyncMock(return_value=_joined(ROOM, names if names is not None else {ALICE: "Alice"})),
        room_get_state=AsyncMock(return_value=state or _state()),
        room_get_state_event=AsyncMock(),
        list_direct_rooms=AsyncMock(return_value=nio.DirectRoomsErrorResponse("No direct rooms", "M_NOT_FOUND")),
    )


def _bound(client: SimpleNamespace) -> FastAPI:
    """An app whose room readers ask only a running router bot with ``client``."""
    app = FastAPI()
    bind_room_members(app, {"router": SimpleNamespace(client=client, running=True)})  # type: ignore[dict-item]
    return app


def _request(app: FastAPI, user_id: str = ALICE) -> Request:
    """A request signed in with a client session of ``user_id``."""
    token = _client_session_store(app).create(user_id).token
    return Request({"type": "http", "app": app, "headers": [(b"authorization", f"Bearer {token}".encode())]})


async def _reader(client: SimpleNamespace, user_id: str = ALICE) -> RoomReader:
    return await require_room_reader(_request(_bound(client), user_id), ROOM)


async def _status(read: Awaitable[object]) -> int:
    """The status a read answers: 200 when it returns, else its HTTP error's status."""
    try:
        await read
    except HTTPException as error:
        return error.status_code
    return 200


def _never_answers() -> AsyncMock:
    async def wait_forever(*_args: object) -> None:
        await asyncio.Event().wait()

    return AsyncMock(side_effect=wait_forever)


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [CAROL, BOB, "@left:example.org"])
async def test_only_joined_users_get_a_reader_and_nothing_else_is_read_first(user_id: str) -> None:
    """A user who never joined, is only invited, or left gets 403, though anyone may join and read the history.

    Nothing of the room but its joined members is read for them.
    """
    state = _state(
        members={
            ALICE: {"membership": "join"},
            BOB: {"membership": "invite"},
            "@left:example.org": {"membership": "leave"},
        },
    )
    client = _client({ALICE: "Alice"}, state=state)
    with pytest.raises(HTTPException) as caught:
        await _reader(client, user_id)
    assert (caught.value.status_code, caught.value.detail) == (403, "Not a member of this room.")
    client.joined_members.assert_awaited_once_with(ROOM)
    client.room_get_state.assert_not_awaited()
    client.room_get_state_event.assert_not_awaited()
    client.list_direct_rooms.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_session_401_before_any_read() -> None:
    """Without a client session the answer is 401, and no bot is asked anything about the room."""
    client = _client()
    with pytest.raises(HTTPException) as caught:
        await require_room_reader(Request({"type": "http", "app": _bound(client), "headers": []}), ROOM)
    assert caught.value.status_code == 401
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_503() -> None:
    """Served without MindRoom's bots, a reader answers 503 like the membership check, and so does `client_rooms`."""
    only_membership = FastAPI()

    async def room_members(_room_id: str) -> dict[str, str | None]:
        return {ALICE: None}

    ensure_app_state(only_membership).room_members = room_members
    unbound = (
        require_room_reader(_request(FastAPI()), ROOM),
        client_rooms(_request(FastAPI()), [ROOM]),
        require_room_reader(_request(only_membership), ROOM),
    )
    for read in unbound:
        with pytest.raises(HTTPException) as caught:
            await read
        assert (caught.value.status_code, caught.value.detail) == (503, "Room membership is unavailable.")


def test_unbinding_withdraws_every_reader() -> None:
    """Once the API stops serving, none of the room readers stays bound."""
    app = _bound(_client())
    unbind_room_members(app)
    state = ensure_app_state(app)
    assert (state.room_members, state.room_state, state.room_direct) == (None, None, None)


@pytest.mark.asyncio
async def test_members_are_room_display_names_from_the_call_that_proved_membership() -> None:
    """Each joined user maps to their display name in the room, None without one, read by the one membership call."""
    client = _client({ALICE: "Alice in Wonderland", BOB: None, CAROL: ""})
    reader = await _reader(client)
    assert reader.user == ClientUser(ALICE)
    assert dict(reader.members) == {ALICE: "Alice in Wonderland", BOB: None, CAROL: None}
    with pytest.raises(TypeError):
        reader.members[BOB] = "Bobby"  # type: ignore[index]
    client.joined_members.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
async def test_concurrent_readers_share_the_membership_call() -> None:
    """Readers asked for at once share one membership call, each answered for their own user."""
    client = _client({ALICE: "Alice", BOB: "Bob"})
    app = _bound(client)
    users = (ALICE, BOB, CAROL)
    asked = [asyncio.create_task(_status(require_room_reader(_request(app, user_id), ROOM))) for user_id in users]
    assert await asyncio.gather(*asked) == [200, 200, 403]
    client.joined_members.assert_awaited_once_with(ROOM)


@pytest.mark.asyncio
async def test_power_reads_levels_admins_and_creators() -> None:
    """A listed level, `users_default` for anyone unlisted, and admin power from level 100."""
    client = _client(state=_state(power_levels={"users": {ALICE: 100, BOB: 50}, "users_default": 10}))
    power = await (await _reader(client)).power()
    assert (power.level(ALICE), power.level(BOB), power.level(CAROL)) == (100, 50, 10)
    assert (power.is_admin(ALICE), power.is_admin(BOB)) == (True, False)
    assert power.creators == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("room_version", ["12", "org.matrix.hydra.11"])
async def test_power_gives_version_12_creators_unlimited_power(room_version: str) -> None:
    """In a version 12 room the creator and each additional creator, never listed in `users`, hold unlimited power."""
    state = _state(power_levels={"users": {BOB: 50}}, room_version=room_version, additional_creators=(CAROL,))
    power = await (await _reader(_client(state=state))).power()
    assert (power.level(ALICE), power.level(CAROL), power.level(BOB)) == (math.inf, math.inf, 50)
    assert (power.is_admin(ALICE), power.is_admin(CAROL), power.is_admin(BOB)) == (True, True, False)
    assert power.creators == frozenset({ALICE, CAROL})


@pytest.mark.asyncio
async def test_may_send_uses_the_events_level_then_events_default() -> None:
    """A type listed in `events` needs its own level, any other type `events_default`, and 0 without either."""
    levels = {"users": {ALICE: 100, BOB: 50}, "events": {"m.room.message": 50}, "events_default": 75}
    power = await (await _reader(_client(state=_state(power_levels=levels)))).power()
    assert (power.may_send(BOB, "m.room.message"), power.may_send(CAROL, "m.room.message")) == (True, False)
    assert (power.may_send(BOB, "m.reaction"), power.may_send(ALICE, "m.reaction")) == (False, True)
    open_room = RoomPower({"users": {}}, frozenset(), encrypted=False)
    assert open_room.may_send(CAROL, "m.room.message")


@pytest.mark.asyncio
async def test_may_send_in_an_encrypted_room_needs_the_encrypted_level_too() -> None:
    """In an encrypted room a message goes out as `m.room.encrypted`, so that type's level must allow it as well."""
    levels = {"users": {BOB: 50}, "events": {"m.room.message": 0, "m.room.encrypted": 50}}
    power = await (await _reader(_client(state=_state(power_levels=levels, encrypted=True)))).power()
    assert power.encrypted
    assert (power.may_send(BOB, "m.room.message"), power.may_send(CAROL, "m.room.message")) == (True, False)
    plain = await (await _reader(_client(state=_state(power_levels=levels)))).power()
    assert (plain.encrypted, plain.may_send(CAROL, "m.room.message")) == (False, True)
    # The message type's own level still counts in an encrypted room.
    strict = {"users": {BOB: 50}, "events": {"m.room.message": 75, "m.room.encrypted": 0}}
    power = await (await _reader(_client(state=_state(power_levels=strict, encrypted=True)))).power()
    assert (power.may_send(BOB, "m.room.message"), power.may_send(BOB, "m.reaction")) == (False, True)


@pytest.mark.asyncio
async def test_a_failed_power_read_answers_503_never_no_power() -> None:
    """A state read that raises or answers an error is 503, never a False that would read as "not an admin"."""
    client = _client()
    reader = await _reader(client)
    client.room_get_state.side_effect = nio.exceptions.ProtocolError("connection reset")
    with pytest.raises(HTTPException) as caught:
        await reader.power()
    assert (caught.value.status_code, caught.value.detail) == (503, "Room state read failed; try again.")
    client.room_get_state.side_effect = None
    client.room_get_state.return_value = nio.RoomGetStateError("M_FORBIDDEN", "M_FORBIDDEN")
    with pytest.raises(HTTPException) as caught:
        await reader.power()
    assert (caught.value.status_code, caught.value.detail) == (503, "Room state read failed; try again.")


@pytest.mark.asyncio
async def test_a_power_read_that_never_answers_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """A state read that hangs gives up after the lookup timeout with 503, never holding the request open."""
    monkeypatch.setattr(plugin_http_runtime, "_LOOKUP_TIMEOUT_SECONDS", 0.01)
    client = _client()
    reader = await _reader(client)
    client.room_get_state = _never_answers()
    with pytest.raises(HTTPException) as caught:
        async with asyncio.timeout(5):
            await reader.power()
    assert (caught.value.status_code, caught.value.detail) == (503, "Room state read failed; try again.")


@pytest.mark.asyncio
async def test_power_read_asks_a_bot_still_in_the_room() -> None:
    """A power read once no running bot is in the room any more is 503, and asks no bot about a room it left."""
    client = _client()
    reader = await _reader(client)
    del client.rooms[ROOM]
    with pytest.raises(HTTPException) as caught:
        await reader.power()
    assert (caught.value.status_code, caught.value.detail) == (503, "No running MindRoom bot is in this room.")
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_names_and_levels_show_on_the_next_read() -> None:
    """No answer outlives its read: a new display name shows on the next request, a new level on the next power read."""
    client = _client({ALICE: "Alice", BOB: "Bob"})
    app = _bound(client)
    reader = await require_room_reader(_request(app), ROOM)
    assert not (await reader.power()).is_admin(BOB)
    client.joined_members.return_value = _joined(ROOM, {ALICE: "Alice", BOB: "Robert"})
    client.room_get_state.return_value = _state(power_levels={"users": {ALICE: 100, BOB: 100}})
    assert (await reader.power()).is_admin(BOB)
    assert reader.members[BOB] == "Bob"
    assert (await require_room_reader(_request(app), ROOM)).members[BOB] == "Robert"
    assert client.room_get_state.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member_content", "direct"),
    [({"membership": "join", "is_direct": True}, True), ({"membership": "join"}, False)],
)
async def test_is_direct_follows_core_direct_message_rule(member_content: dict[str, object], direct: bool) -> None:
    """A room with an `is_direct` member event reads as direct, a group room does not, exactly as core decides."""
    state = _state(members={ALICE: member_content, BOB: {"membership": "join"}, ROUTER: {"membership": "join"}})
    client = _client({ALICE: "Alice", BOB: "Bob", ROUTER: "Router"}, state=state)
    assert await (await _reader(client)).is_direct() is direct
    rooms._DM_ROOM_CACHE.clear()
    assert await is_dm_room(client, ROOM) is direct  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_failed_direct_message_read_answers_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct-message read that raises or hangs is 503, never a guess."""
    monkeypatch.setattr(plugin_http_runtime, "_LOOKUP_TIMEOUT_SECONDS", 0.01)
    client = _client()
    reader = await _reader(client)
    client.list_direct_rooms.side_effect = nio.exceptions.ProtocolError("connection reset")
    with pytest.raises(HTTPException) as caught:
        await reader.is_direct()
    assert (caught.value.status_code, caught.value.detail) == (503, "Direct message lookup failed; try again.")
    client.list_direct_rooms = _never_answers()
    with pytest.raises(HTTPException) as caught:
        async with asyncio.timeout(5):
            await reader.is_direct()
    assert (caught.value.status_code, caught.value.detail) == (503, "Direct message lookup failed; try again.")


@pytest.mark.asyncio
async def test_client_rooms_keeps_the_joined_candidates_in_order() -> None:
    """Only candidates the session's user is joined to now come back, in the given order; a failed lookup leaves its room out."""
    client = _client(joined_rooms=(ROOM, OTHER_ROOM, THIRD_ROOM))
    answers = {
        ROOM: _joined(ROOM, {ALICE: None, BOB: None}),
        OTHER_ROOM: _joined(OTHER_ROOM, {ALICE: None}),
        THIRD_ROOM: nio.JoinedMembersError("boom"),
    }
    client.joined_members.side_effect = answers.__getitem__
    app = _bound(client)
    candidates = iter([THIRD_ROOM, OTHER_ROOM, "!nobot:example.org", ROOM])
    assert await client_rooms(_request(app), candidates) == [OTHER_ROOM, ROOM]
    assert await client_rooms(_request(app, BOB), [OTHER_ROOM, ROOM, THIRD_ROOM]) == [ROOM]
    with pytest.raises(HTTPException) as caught:
        await client_rooms(Request({"type": "http", "app": app, "headers": []}), [ROOM])
    assert caught.value.status_code == 401


def test_routes_read_a_room_through_the_dependency() -> None:
    """A route that depends on `require_room_reader` takes its room from the path and its user from the session."""
    client = _client({ALICE: "Alice", BOB: None}, state=_state(power_levels={"users": {ALICE: 100}}))
    app = _bound(client)

    @app.get("/rooms/{room_id}/roster")
    async def roster(reader: Annotated[RoomReader, Depends(require_room_reader)]) -> dict[str, object]:
        power = await reader.power()
        return {
            "members": {user_id: [name, power.level(user_id)] for user_id, name in reader.members.items()},
            "admin": power.is_admin(reader.user.user_id),
            "may_post": power.may_send(reader.user.user_id, "m.room.message"),
            "direct": await reader.is_direct(),
        }

    @app.get("/my-rooms")
    async def my_rooms(request: Request) -> list[str]:
        return await client_rooms(request, [OTHER_ROOM, ROOM])

    http = TestClient(app)
    alice = {"Authorization": f"Bearer {_client_session_store(app).create(ALICE).token}"}
    carol = {"Authorization": f"Bearer {_client_session_store(app).create(CAROL).token}"}
    room = quote(ROOM, safe="")
    response = http.get(f"/rooms/{room}/roster?user_id={quote(CAROL)}", headers=alice)
    assert (response.status_code, response.json()) == (
        200,
        {"members": {ALICE: ["Alice", 100], BOB: [None, 0]}, "admin": True, "may_post": True, "direct": False},
    )
    assert http.get(f"/rooms/{room}/roster", headers=carol).status_code == 403
    assert http.get(f"/rooms/{room}/roster").status_code == 401
    assert http.get("/my-rooms", headers=alice).json() == [ROOM]
    unbind_room_members(app)
    assert http.get(f"/rooms/{room}/roster", headers=alice).status_code == 503
