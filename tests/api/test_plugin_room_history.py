"""Plugin routes read where a room member's view of its history begins, as the room's history visibility sets it.

The read happens only once the caller is found joined, finds them still joined or answers 403, and answers 503 rather
than a guess when Matrix cannot answer; where the walk back to the join stops early, the caller sees less, never more.
"""

from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock
from urllib.parse import quote

import nio
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from mindroom.api.client_sessions import client_session_store
from mindroom.matrix import history_visibility
from mindroom.orchestration import plugin_http_runtime
from mindroom.plugin_http import RoomHistory, RoomReader, require_room_reader
from tests.api.test_plugin_room_readers import (
    ALICE,
    BOB,
    CAROL,
    ROOM,
    ROUTER,
    _answer,
    _bound,
    _joined,
    _never_answers,
    _request,
)

DAVE = "@dave:example.org"
NOT_A_MEMBER = (403, "Not a member of this room.")
READ_FAILED = (503, "Room history read failed; try again.")
INVITED_AT, JOINED_AT = 2_000, 3_000
# Bob was invited, joined, and then changed his display name and his avatar.
BOB_INVITED_JOINED_RENAMED = (
    ("invite", INVITED_AT, {}),
    ("join", JOINED_AT, {}),
    ("join", 4_000, {"displayname": "Bobby"}),
    ("join", 5_000, {"displayname": "Bobby", "avatar_url": "mxc://example.org/bob"}),
)


def _chain(user_id: str, *steps: tuple[str, int, dict[str, str]]) -> list[dict[str, object]]:
    """``user_id``'s member events, oldest first, each replacing the one before it in the room's state."""
    events: list[dict[str, object]] = []
    for index, (membership, timestamp, profile) in enumerate(steps):
        event: dict[str, object] = {
            "type": "m.room.member",
            "state_key": user_id,
            "sender": user_id,
            "event_id": f"${user_id}-{index}",
            "origin_server_ts": timestamp,
            "content": {"membership": membership, **profile},
        }
        if events:
            event["unsigned"] = {"replaces_state": events[-1]["event_id"]}
        events.append(event)
    return events


ALICE_JOINED = _chain(ALICE, ("join", 1_000, {}))


def _room_state(visibility: object, *chains: list[dict[str, object]]) -> nio.RoomGetStateResponse:
    """The room's state: its history visibility, None for none, Alice joined, and each chain's latest member event."""
    events: list[dict[str, object]] = [
        {"type": "m.room.create", "state_key": "", "sender": ALICE, "content": {"room_version": "10"}},
    ]
    if visibility is not None:
        events.append(
            {
                "type": "m.room.history_visibility",
                "state_key": "",
                "sender": ALICE,
                "content": {"history_visibility": visibility},
            },
        )
    events.extend(chain[-1] for chain in (ALICE_JOINED, *chains))
    return nio.RoomGetStateResponse(events, ROOM)


def _client(visibility: object, *chains: list[dict[str, object]]) -> SimpleNamespace:
    """The router's client in ``_room_state(visibility, *chains)``; it reads any of their events by ID."""
    events = {event["event_id"]: event for chain in (ALICE_JOINED, *chains) for event in chain}

    async def room_get_event(room_id: str, event_id: str) -> nio.RoomGetEventResponse | nio.RoomGetEventError:
        assert room_id == ROOM
        return nio.RoomGetEventResponse.from_dict(events[event_id])

    joined = {
        str(chain[-1]["state_key"]): None
        for chain in (ALICE_JOINED, *chains)
        if chain[-1]["content"]["membership"] == "join"  # type: ignore[index]
    }
    return SimpleNamespace(
        user_id=ROUTER,
        rooms={ROOM: nio.MatrixRoom(ROOM, ROUTER)},
        joined_members=AsyncMock(return_value=_joined(ROOM, joined)),
        room_get_state=AsyncMock(return_value=_room_state(visibility, *chains)),
        room_get_event=AsyncMock(side_effect=room_get_event),
    )


async def _history(client: SimpleNamespace, user_id: str = BOB) -> RoomHistory:
    reader = await require_room_reader(_request(_bound(client), user_id), ROOM)
    return await reader.history()


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["world_readable", "shared"])
@pytest.mark.parametrize("user_id", [CAROL, BOB, DAVE])
async def test_only_joined_callers_reach_the_history_and_nothing_else_is_read_first(
    visibility: str,
    user_id: str,
) -> None:
    """One who never joined, is only invited, or left gets 403 even where anyone may read the history.

    Nothing of the room but its joined members is read for them.
    """
    client = _client(
        visibility,
        _chain(BOB, ("invite", INVITED_AT, {})),
        _chain(DAVE, ("join", 1, {}), ("leave", 2, {})),
    )
    assert await _answer(_history(client, user_id)) == NOT_A_MEMBER
    client.joined_members.assert_awaited_once_with(ROOM)
    client.room_get_state.assert_not_awaited()
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["world_readable", "shared", "joined"])
@pytest.mark.parametrize("now", ["leave", "ban", "invite"])
async def test_a_caller_no_longer_joined_when_the_history_is_read_gets_403(visibility: str, now: str) -> None:
    """A caller found joined who left, was banned, or left and was invited again before the history read gets 403."""
    client = _client(visibility, _chain(BOB, ("join", JOINED_AT, {})))
    reader = await require_room_reader(_request(_bound(client), BOB), ROOM)
    later = [] if now == "leave" else [(now, 5_000, {})]
    client.room_get_state.return_value = _room_state(
        visibility,
        _chain(BOB, ("join", JOINED_AT, {}), ("leave", 4_000, {}), *later),
    )
    assert await _answer(reader.history()) == NOT_A_MEMBER
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("visibility", "read_as", "since"),
    [
        ("world_readable", "world_readable", None),
        ("shared", "shared", None),
        (None, "shared", None),
        ("invited", "invited", INVITED_AT),
        ("joined", "joined", JOINED_AT),
        ("org.example.custom", "org.example.custom", JOINED_AT),
        (["shared"], "joined", JOINED_AT),
    ],
)
async def test_since_follows_the_visibility(visibility: object, read_as: str, since: int | None) -> None:
    """None under `shared`, `world_readable` or none; the invite under `invited`; the join under `joined` or another.

    Display name and avatar changes after joining leave the start at the join.
    """
    history = await _history(_client(visibility, _chain(BOB, *BOB_INVITED_JOINED_RENAMED)))
    assert (history.visibility, history.since()) == (read_as, since)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("steps", "joined_at", "invited_at"),
    [
        # Joined a public room without an invite.
        ((("join", 3_000, {}), ("join", 4_000, {"displayname": "Bobby"})), 3_000, 3_000),
        # Left and joined again: the earlier membership no longer counts.
        ((("join", 1_000, {}), ("leave", 1_500, {}), ("join", 3_000, {})), 3_000, 3_000),
        ((("join", 1_000, {}), ("leave", 1_500, {}), ("invite", 2_000, {}), ("join", 3_000, {})), 3_000, 2_000),
        # Invited twice before joining: the first invite began the membership.
        ((("invite", 1_000, {}), ("invite", 2_000, {"reason": "again"}), ("join", 3_000, {})), 3_000, 1_000),
    ],
)
async def test_the_start_is_the_current_membership(
    steps: tuple[tuple[str, int, dict[str, str]], ...],
    joined_at: int,
    invited_at: int,
) -> None:
    """The join that began the caller's current membership, and under `invited` the invite it came from, else the join."""
    history = await _history(_client("invited", _chain(BOB, *steps)))
    assert (history.since("joined"), history.since()) == (joined_at, invited_at)


@pytest.mark.asyncio
async def test_a_walk_past_its_bound_starts_later_never_earlier() -> None:
    """Past its fixed number of reads, the walk back to the join takes the earliest event it reached."""
    renames = [("join", 10_000 + step, {"displayname": f"Bob {step}"}) for step in range(25)]
    client = _client("invited", _chain(BOB, ("invite", INVITED_AT, {}), ("join", JOINED_AT, {}), *renames))
    history = await _history(client)
    reached = 10_000 + 24 - history_visibility._WALK_STEPS
    assert (history.since(), history.since("joined")) == (reached, reached)
    assert client.room_get_event.await_count == history_visibility._WALK_STEPS


@pytest.mark.asyncio
@pytest.mark.parametrize("errcode", ["M_NOT_FOUND", "M_FORBIDDEN"])
async def test_an_earlier_event_the_bot_may_not_see_ends_the_walk_there(errcode: str) -> None:
    """Under `joined` history a bot that joined later may not read the caller's earlier events: those count no further."""
    client = _client("invited", _chain(BOB, *BOB_INVITED_JOINED_RENAMED))
    client.room_get_event.side_effect = lambda *_: nio.RoomGetEventError("Event not found.", errcode)
    history = await _history(client)
    assert (history.since(), history.since("joined")) == (5_000, 5_000)
    client.room_get_event.assert_awaited_once_with(ROOM, f"${BOB}-2")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("visibility", "recorded_under", "since"),
    [
        ("shared", "joined", JOINED_AT),
        ("joined", "shared", JOINED_AT),
        ("shared", "invited", INVITED_AT),
        ("invited", "joined", JOINED_AT),
        ("joined", "invited", JOINED_AT),
        ("world_readable", "shared", None),
        ("shared", "world_readable", None),
        ("shared", "org.example.custom", JOINED_AT),
    ],
)
async def test_since_takes_the_stricter_of_now_and_recorded_under(
    visibility: str,
    recorded_under: str,
    since: int | None,
) -> None:
    """A record kept under a stricter visibility than the room's now stays hidden from a later joiner, and vice versa."""
    history = await _history(_client(visibility, _chain(BOB, *BOB_INVITED_JOINED_RENAMED)))
    assert history.since(recorded_under) == since


async def _state_answers_an_error(client: SimpleNamespace) -> None:
    client.room_get_state.return_value = nio.RoomGetStateError("M_FORBIDDEN", "M_FORBIDDEN")


async def _state_raises(client: SimpleNamespace) -> None:
    client.room_get_state.side_effect = nio.exceptions.ProtocolError("connection reset")


async def _state_never_answers(client: SimpleNamespace) -> None:
    client.room_get_state = _never_answers


async def _event_answers_an_error(client: SimpleNamespace) -> None:
    client.room_get_event.side_effect = lambda *_: nio.RoomGetEventError("Internal error", "M_UNKNOWN")


async def _event_raises(client: SimpleNamespace) -> None:
    client.room_get_event.side_effect = nio.exceptions.ProtocolError("connection reset")


async def _event_never_answers(client: SimpleNamespace) -> None:
    client.room_get_event = _never_answers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail",
    [
        _state_answers_an_error,
        _state_raises,
        _state_never_answers,
        _event_answers_an_error,
        _event_raises,
        _event_never_answers,
    ],
)
async def test_a_failed_history_read_answers_503(monkeypatch: pytest.MonkeyPatch, fail: object) -> None:
    """A state or event read that answers an error, raises, or outlasts the lookup timeout is 503, never a start."""
    monkeypatch.setattr(plugin_http_runtime, "_LOOKUP_TIMEOUT_SECONDS", 0.01)
    client = _client("joined", _chain(BOB, *BOB_INVITED_JOINED_RENAMED))
    reader = await require_room_reader(_request(_bound(client), BOB), ROOM)
    await fail(client)  # type: ignore[operator]
    assert await _answer(reader.history()) == READ_FAILED


@pytest.mark.asyncio
async def test_a_history_read_asks_a_bot_still_in_the_room() -> None:
    """Once no running bot is in the room any more the read is 503, and asks no bot about a room it left."""
    client = _client("joined", _chain(BOB, ("join", JOINED_AT, {})))
    reader = await require_room_reader(_request(_bound(client), BOB), ROOM)
    del client.rooms[ROOM]
    assert await _answer(reader.history()) == (503, "No running MindRoom bot is in this room.")
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_changed_visibility_or_membership_shows_on_the_next_read() -> None:
    """No answer outlives its read: a new visibility, or a new join after leaving, shows on the next call."""
    client = _client("joined", _chain(BOB, ("join", JOINED_AT, {})))
    reader = await require_room_reader(_request(_bound(client), BOB), ROOM)
    assert (await reader.history()).since() == JOINED_AT
    client.room_get_state.return_value = _room_state("shared", _chain(BOB, ("join", JOINED_AT, {})))
    assert (await reader.history()).since() is None
    rejoined = _chain(BOB, ("join", JOINED_AT, {}), ("leave", 4_000, {}), ("join", 5_000, {}))
    client.room_get_state.return_value = _room_state("joined", rejoined)
    client.room_get_event.side_effect = lambda _room, event_id: nio.RoomGetEventResponse.from_dict(
        next(event for event in rejoined if event["event_id"] == event_id),
    )
    assert (await reader.history()).since() == 5_000


def test_a_route_shows_each_member_the_notes_their_view_allows() -> None:
    """A notes route keeps each note's visibility, and shows a later joiner only what the stricter one allows."""
    client = _client("joined", _chain(BOB, ("join", JOINED_AT, {})))
    app = _bound(client)
    notes: list[dict[str, object]] = [
        {"text": "before Bob", "at": 2_500, "visibility": "joined"},
        {"text": "after Bob", "at": 3_500, "visibility": "joined"},
        {"text": "shared before Bob", "at": 2_600, "visibility": "shared"},
    ]

    @app.get("/rooms/{room_id}/notes")
    async def read_notes(reader: Annotated[RoomReader, Depends(require_room_reader)]) -> list[str]:
        history = await reader.history()
        return [
            str(note["text"])
            for note in notes
            if (since := history.since(str(note["visibility"]))) is None or int(str(note["at"])) > since
        ]

    http = TestClient(app)
    room = quote(ROOM, safe="")
    bob = {"Authorization": f"Bearer {client_session_store(app).create(BOB).token}"}
    alice = {"Authorization": f"Bearer {client_session_store(app).create(ALICE).token}"}
    assert http.get(f"/rooms/{room}/notes", headers=bob).json() == ["after Bob"]
    client.room_get_state.return_value = _room_state("shared", _chain(BOB, ("join", JOINED_AT, {})))
    assert http.get(f"/rooms/{room}/notes", headers=bob).json() == ["after Bob", "shared before Bob"]
    assert http.get(f"/rooms/{room}/notes", headers=alice).json() == ["before Bob", "after Bob", "shared before Bob"]
    assert http.get(f"/rooms/{room}/notes").status_code == 401


@pytest.mark.asyncio
async def test_room_history_is_frozen() -> None:
    """A route cannot change the answer it was given."""
    history = await _history(_client("joined", _chain(BOB, ("join", JOINED_AT, {}))))
    with pytest.raises(AttributeError):
        history.visibility = "shared"  # type: ignore[misc]
