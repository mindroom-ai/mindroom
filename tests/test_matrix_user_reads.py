"""The person's own view of Matrix that the `matrix_user` toolkit reads: their rooms, one room, and people."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.matrix_user_reads import MatrixRequestError, read_people, read_room, read_rooms
from mindroom.matrix.user_masquerade import create_user_masquerade_client
from tests.matrix_user_helpers import (
    SYNC_PATH,
    Answer,
    FakeHomeserver,
    invited_room,
    joined_room,
    matrix_error,
    state_event,
    sync_answer,
)

if TYPE_CHECKING:
    from pathlib import Path

    import nio

PERSON = "@alice:example.com"
AGENT = "@mind:example.com"
TOKEN = "as-user-secret"  # noqa: S105
ROOM = "!room:example.com"
ROOM_PATH = f"/_matrix/client/v3/rooms/{ROOM}"


@pytest.fixture
def homeserver() -> FakeHomeserver:
    """The person's homeserver, with no answers yet."""
    return FakeHomeserver()


@pytest.fixture
def client(tmp_path: Path, homeserver: FakeHomeserver) -> nio.AsyncClient:
    """A client that acts as the person, talking to the fake homeserver."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.com"},
    )
    return homeserver.serve(
        create_user_masquerade_client(paths, token=TOKEN, person_user_id=PERSON, agent_user_id=AGENT),
    )


def _sync_filter(homeserver: FakeHomeserver, index: int = 0) -> dict[str, object]:
    [sync_filter] = [request for request in homeserver.requests if request.path == SYNC_PATH][index].query["filter"]
    return json.loads(sync_filter)


@pytest.mark.asyncio
async def test_rooms_lists_unread_first_then_invitations_then_the_rest(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """Rooms that want attention come first, then invitations, then every other room, each group by name."""
    renamed = state_event("m.room.name", {"name": "Team"})
    homeserver.answers[SYNC_PATH] = sync_answer(
        join={
            "!zeta:example.com": joined_room("Zeta"),
            "!busy:example.com": joined_room("Old team name", unread=3, mentions=1, timeline=(renamed,)),
            "!quiet:example.com": joined_room("archive"),
            "!flag:example.com": joined_room("Billing", marked_unread=True),
        },
        invite={"!party:example.com": invited_room(PERSON, "@bob:example.com", "Party")},
    )

    result = await read_rooms(client, query=None, limit=None)

    assert result == {
        "rooms": [
            "!flag:example.com | Billing | unread 0, mentions 0, marked unread",
            "!busy:example.com | Team | unread 3, mentions 1",
            "!party:example.com | Party | invited by @bob:example.com",
            "!quiet:example.com | archive | unread 0, mentions 0",
            "!zeta:example.com | Zeta | unread 0, mentions 0",
        ],
        "left_out": 0,
    }
    [request] = homeserver.requests
    assert request.method == "GET"
    assert request.query["timeout"] == ["0"]
    assert request.query["set_presence"] == ["offline"]
    assert request.query["user_id"] == [PERSON]
    room_filter = _sync_filter(homeserver)["room"]
    assert room_filter["state"]["types"] == ["m.room.name", "m.room.member"]
    assert room_filter["timeline"]["limit"] == 1
    assert room_filter["ephemeral"] == {"types": []}
    assert room_filter["account_data"] == {"types": ["m.marked_unread"]}


@pytest.mark.asyncio
async def test_rooms_names_a_dm_by_the_other_person(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A direct chat without a name is named by the other person, a direct invitation by who sent it."""
    homeserver.answers[SYNC_PATH] = sync_answer(
        join={"!dm:example.com": joined_room(unread=1), "!group:example.com": joined_room()},
        invite={"!ask:example.com": invited_room(PERSON, "@carol:example.com", is_direct=True)},
        direct={"@bob:example.com": ["!dm:example.com"]},
    )

    result = await read_rooms(client, query=None, limit=None)

    assert result["rooms"] == [
        "!dm:example.com | @bob:example.com | unread 1, mentions 0",
        "!ask:example.com | @carol:example.com | invited by @carol:example.com",
        "!group:example.com | (no name) | unread 0, mentions 0",
    ]


@pytest.mark.asyncio
async def test_rooms_filters_by_name(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A query keeps the rooms whose name contains it, whatever its case, a direct chat's other person included."""
    homeserver.answers[SYNC_PATH] = sync_answer(
        join={
            "!team:example.com": joined_room("Team"),
            "!work:example.com": joined_room("Teamwork", unread=2),
            "!other:example.com": joined_room("Other"),
            "!dm:example.com": joined_room(),
        },
        invite={"!steam:example.com": invited_room(PERSON, "@bob:example.com", "Steam room")},
        direct={"@teammate:example.com": ["!dm:example.com"]},
    )

    result = await read_rooms(client, query="TEAM", limit=None)

    assert result == {
        "rooms": [
            "!work:example.com | Teamwork | unread 2, mentions 0",
            "!steam:example.com | Steam room | invited by @bob:example.com",
            "!dm:example.com | @teammate:example.com | unread 0, mentions 0",
            "!team:example.com | Team | unread 0, mentions 0",
        ],
        "left_out": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("limit", "shown"), [(2, 2), (None, 50), (500, 200), (0, 1)])
async def test_rooms_reports_how_many_it_left_out(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
    limit: int | None,
    shown: int,
) -> None:
    """At most ``limit`` rooms are listed, 50 unless asked, never more than 200, and the result says how many are not."""
    homeserver.answers[SYNC_PATH] = sync_answer(
        join={f"!room{number:03}:example.com": joined_room(f"Room {number:03}") for number in range(250)},
    )

    result = await read_rooms(client, query=None, limit=limit)

    assert len(result["rooms"]) == shown
    assert result["rooms"][0] == "!room000:example.com | Room 000 | unread 0, mentions 0"
    assert result["left_out"] == 250 - shown


@pytest.mark.asyncio
async def test_two_rooms_calls_never_send_the_same_sync_filter(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """Each call's filter differs, so a homeserver cannot answer it from a cache of an earlier sync."""
    homeserver.answers[SYNC_PATH] = sync_answer()

    await read_rooms(client, query=None, limit=None)
    await read_rooms(client, query=None, limit=None)

    first, second = _sync_filter(homeserver, 0), _sync_filter(homeserver, 1)
    assert first != second
    assert first["presence"]["types"] == second["presence"]["types"] == []
    assert {key: value for key, value in first.items() if key != "presence"} == {
        key: value for key, value in second.items() if key != "presence"
    }


@pytest.mark.asyncio
async def test_room_reports_encryption_and_members(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A room's name, topic, encryption, whether it is a direct chat, and its joined members, as many as asked."""
    plain = "!plain:example.com"
    homeserver.answers.update(
        {
            f"{ROOM_PATH}/joined_members": {
                "joined": {
                    "@carol:example.com": {"display_name": "Carol"},
                    PERSON: {"display_name": "Alice"},
                    "@bob:example.com": {"display_name": None},
                },
            },
            f"{ROOM_PATH}/state/m.room.name": {"name": "Planning"},
            f"{ROOM_PATH}/state/m.room.topic": {"topic": "Next quarter"},
            f"{ROOM_PATH}/state/m.room.encryption": {"algorithm": "m.megolm.v1.aes-sha2"},
            f"/_matrix/client/v3/user/{PERSON}/account_data/m.direct": {"@bob:example.com": [ROOM]},
            f"/_matrix/client/v3/rooms/{plain}/joined_members": {"joined": {PERSON: {"display_name": "Alice"}}},
        },
    )

    encrypted = await read_room(client, ROOM, limit=2)
    unencrypted = await read_room(client, plain, limit=None)

    assert encrypted == {
        "room_id": ROOM,
        "name": "Planning",
        "topic": "Next quarter",
        "encrypted": True,
        "dm": True,
        "joined_members": 3,
        "members": ["@alice:example.com | Alice", "@bob:example.com"],
    }
    assert unencrypted == {
        "room_id": plain,
        "name": None,
        "topic": None,
        "encrypted": False,
        "dm": False,
        "joined_members": 1,
        "members": ["@alice:example.com | Alice"],
    }


@pytest.mark.asyncio
async def test_a_room_the_person_is_not_in_is_an_error(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """The homeserver's refusal is the error, with its code, and nothing else is asked."""
    homeserver.answers[f"{ROOM_PATH}/joined_members"] = matrix_error(403, "M_FORBIDDEN", "You are not in this room")

    with pytest.raises(MatrixRequestError) as error:
        await read_room(client, ROOM, limit=None)

    assert str(error.value) == "Matrix answered 403 M_FORBIDDEN: You are not in this room"
    assert homeserver.paths() == [f"{ROOM_PATH}/joined_members"]


@pytest.mark.asyncio
async def test_people_returns_a_profile_for_a_full_id(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A full Matrix ID reads that person's profile and searches nothing."""
    homeserver.answers["/_matrix/client/v3/profile/@bob:example.com"] = {"displayname": "Bob Builder"}

    result = await read_people(client, " @bob:example.com ", limit=None)

    assert result == {"people": ["@bob:example.com | Bob Builder"]}
    assert homeserver.paths() == ["/_matrix/client/v3/profile/@bob:example.com"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("limit", "sent"), [(None, 10), (2, 2), (80, 50)])
async def test_people_searches_the_directory(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
    limit: int | None,
    sent: int,
) -> None:
    """Anything but a full Matrix ID searches the user directory, 10 people unless asked, never more than 50."""
    homeserver.answers["/_matrix/client/v3/user_directory/search"] = {
        "limited": True,
        "results": [
            {"user_id": "@bob:example.com", "display_name": "Bob\nBuilder"},
            {"user_id": "@bobby:example.com"},
        ],
    }

    result = await read_people(client, "bob", limit=limit)

    assert result == {"people": ["@bob:example.com | Bob Builder", "@bobby:example.com"][:sent], "limited": True}
    [request] = homeserver.requests
    assert request.method == "POST"
    assert request.body == {"search_term": "bob", "limit": sent}


@pytest.mark.asyncio
async def test_a_rate_limited_request_is_retried_at_most_twice(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """A 429 with a short wait is retried twice; the third is the error."""
    limited = matrix_error(429, "M_LIMIT_EXCEEDED", "Too many requests", retry_after_ms=1)
    homeserver.answers[SYNC_PATH] = [limited, Answer(sync_answer(join={ROOM: joined_room("Room")}))]

    assert (await read_rooms(client, query=None, limit=None))["rooms"] == [f"{ROOM} | Room | unread 0, mentions 0"]

    homeserver.answers[SYNC_PATH] = [limited]
    with pytest.raises(MatrixRequestError, match="429 M_LIMIT_EXCEEDED"):
        await read_rooms(client, query=None, limit=None)
    assert homeserver.paths() == [SYNC_PATH] * 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limited",
    [
        matrix_error(429, "M_LIMIT_EXCEEDED", "Too many requests", retry_after_ms=6000),
        Answer({}, status=429, headers={"Retry-After": "6"}),
    ],
)
async def test_a_rate_limit_longer_than_5_seconds_is_not_waited_out(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
    limited: Answer,
) -> None:
    """A homeserver that asks for a wait over 5 seconds gets no retry."""
    homeserver.answers[SYNC_PATH] = limited

    with pytest.raises(MatrixRequestError, match="429"):
        await read_rooms(client, query=None, limit=None)

    assert homeserver.paths() == [SYNC_PATH]


@pytest.mark.asyncio
async def test_an_answer_over_2_mib_is_refused(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """An answer is read up to 2 MiB and no further."""
    homeserver.answers[SYNC_PATH] = Answer(b" " * (2 * 1024 * 1024) + b"{}")

    with pytest.raises(MatrixRequestError, match="larger than 2 MiB"):
        await read_rooms(client, query=None, limit=None)
