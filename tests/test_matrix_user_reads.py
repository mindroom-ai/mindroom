"""The person's own view of Matrix that the `matrix_user` toolkit reads: rooms, messages, files, and people."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.attachments.storage import load_attachment
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.matrix_user_reads import (
    MatrixRequestError,
    download_file,
    mark_read,
    read_messages,
    read_people,
    read_room,
    read_rooms,
    search_messages,
)
from mindroom.matrix.user_masquerade import create_user_masquerade_client
from tests.matrix_user_helpers import (
    SYNC_PATH,
    Answer,
    FakeHomeserver,
    edit_of,
    invited_room,
    joined_room,
    matrix_error,
    message_line,
    reaction,
    room_event,
    state_event,
    sync_answer,
    text_message,
)

if TYPE_CHECKING:
    from pathlib import Path

    import nio

PERSON = "@alice:example.com"
AGENT = "@mind:example.com"
TOKEN = "as-user-secret"  # noqa: S105
ROOM = "!room:example.com"
ROOM_PATH = f"/_matrix/client/v3/rooms/{ROOM}"
MESSAGES_PATH = f"{ROOM_PATH}/messages"
SEARCH_PATH = "/_matrix/client/v3/search"
MEDIA_PATH = "/_matrix/client/v1/media/download/example.com/abc"
MARKED_UNREAD_PATH = f"/_matrix/client/v3/user/{PERSON}/rooms/{ROOM}/account_data/m.marked_unread"
# What a page of messages asks for: the messages, and the reactions to count on them.
MESSAGES_FILTER = {"types": ["m.room.message", "m.room.encrypted", "m.reaction"]}
CAROL = "@carol:example.com"


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
            "!flag:example.com | Billing | marked unread",
            "!busy:example.com | Team | unread 3, mentions 1",
            "!party:example.com | Party | invited by @bob:example.com",
            "!quiet:example.com | archive",
            "!zeta:example.com | Zeta",
        ],
        "left_out": 0,
    }
    [request] = homeserver.requests
    assert request.method == "GET"
    assert request.query["timeout"] == ["0"]
    assert request.query["set_presence"] == ["offline"]
    assert request.query["user_id"] == [PERSON]


@pytest.mark.asyncio
async def test_the_sync_filter_asks_only_for_the_fields_it_reads(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """Every event comes trimmed to what the room list reads, and the joined rooms' state is their names alone.

    The newest event, whatever its type, can rename the room; an invitation's stripped state is not filtered.
    """
    homeserver.answers[SYNC_PATH] = sync_answer()

    await read_rooms(client, query=None, limit=None)

    sync_filter = _sync_filter(homeserver)
    assert sync_filter["event_fields"] == [
        "type",
        "state_key",
        "sender",
        "content.name",
        "content.is_direct",
        "content.unread",
    ]
    room_filter = sync_filter["room"]
    assert room_filter["state"] == {"types": ["m.room.name"], "lazy_load_members": True}
    assert room_filter["timeline"] == {"limit": 1}
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
        "!dm:example.com | @bob:example.com | unread 1",
        "!ask:example.com | @carol:example.com | invited by @carol:example.com",
        "!group:example.com | (no name)",
    ]


@pytest.mark.asyncio
async def test_an_unnamed_room_is_named_by_its_heroes(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A room with no name that is no direct chat is named by the members the homeserver picks, and found by them."""
    homeserver.answers[SYNC_PATH] = sync_answer(
        join={
            "!trio:example.com": joined_room(heroes=("@bob:example.com", "@carol:example.com")),
            "!quiet:example.com": joined_room(),
        },
    )

    every = await read_rooms(client, query=None, limit=None)
    found = await read_rooms(client, query="carol", limit=None)

    assert every["rooms"] == [
        "!trio:example.com | @bob:example.com, @carol:example.com",
        "!quiet:example.com | (no name)",
    ]
    assert found["rooms"] == ["!trio:example.com | @bob:example.com, @carol:example.com"]


@pytest.mark.asyncio
async def test_a_name_cannot_fake_a_column(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A `|` that whoever named a room, a person, or an account wrote becomes `¦`, so a line keeps its own columns."""
    homeserver.answers.update(
        {
            SYNC_PATH: sync_answer(
                join={ROOM: joined_room("Team | unread 99")},
                invite={
                    "!ask:example.com": invited_room(PERSON, "@x|y:example.com", "Ask | invited by @bob:example.com"),
                },
            ),
            "/_matrix/client/v3/user_directory/search": {
                "results": [{"user_id": "@bob:example.com", "display_name": "Bob | @admin:example.com"}],
            },
        },
    )

    rooms = await read_rooms(client, query=None, limit=None)
    people = await read_people(client, "bob", limit=None)

    assert rooms["rooms"] == [
        "!ask:example.com | Ask ¦ invited by @bob:example.com | invited by @x¦y:example.com",
        f"{ROOM} | Team ¦ unread 99",
    ]
    assert people["people"] == ["@bob:example.com | Bob ¦ @admin:example.com"]


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
            "!work:example.com | Teamwork | unread 2",
            "!steam:example.com | Steam room | invited by @bob:example.com",
            "!dm:example.com | @teammate:example.com",
            "!team:example.com | Team",
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
    assert result["rooms"][0] == "!room000:example.com | Room 000"
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

    assert (await read_rooms(client, query=None, limit=None))["rooms"] == [f"{ROOM} | Room"]

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
    homeserver.answers["/_matrix/client/v3/user_directory/search"] = Answer(b" " * (2 * 1024 * 1024) + b"{}")

    with pytest.raises(MatrixRequestError, match="larger than 2 MiB"):
        await read_people(client, "bob", limit=None)


@pytest.mark.asyncio
async def test_an_oversized_room_list_says_what_to_do(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A room list too large to read says how to reach one room instead."""
    homeserver.answers[SYNC_PATH] = Answer(b" " * (2 * 1024 * 1024) + b"{}")

    with pytest.raises(MatrixRequestError) as error:
        await read_rooms(client, query=None, limit=None)

    assert str(error.value) == (
        "This account has too many rooms to list at once. "
        'Ask the person for the room\'s name or ID and use action "room".'
    )


async def _messages(
    client: nio.AsyncClient,
    *,
    event_id: str | None = None,
    thread_id: str | None = None,
    page_token: str | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    return await read_messages(
        client,
        ROOM,
        event_id=event_id,
        thread_id=thread_id,
        page_token=page_token,
        limit=limit,
    )


def _thread_reply(event_id: str, root: str, body: str, *, minute: int) -> dict[str, object]:
    relation = {"rel_type": "m.thread", "event_id": root, "is_falling_back": True, "m.in_reply_to": {"event_id": root}}
    content = {"msgtype": "m.text", "body": body, "m.relates_to": relation}
    return room_event(event_id, "m.room.message", content, minute=minute)


@pytest.mark.asyncio
async def test_messages_are_one_line_each_oldest_first(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A page of the newest messages, each one line with what applies of its file and thread, oldest first."""
    file_content = {
        "msgtype": "m.file",
        "body": "notes.txt",
        "url": "mxc://example.com/abc",
        "info": {"mimetype": "text/plain", "size": 2048},
    }
    image_content = {
        "msgtype": "m.image",
        "body": "Look at this",
        "filename": "cat.png",
        "url": "mxc://example.com/cat",
        "info": {"mimetype": "image/png"},
    }
    homeserver.answers[MESSAGES_PATH] = {
        # Newest first, as the homeserver pages backwards; shorter than asked, with older messages still to come.
        "chunk": [
            room_event("$image", "m.room.message", image_content, minute=4),
            room_event("$file", "m.room.message", file_content, minute=3),
            room_event(
                "$root",
                "m.room.message",
                {"msgtype": "m.text", "body": "Lunch?"},
                sender=CAROL,
                minute=2,
                unsigned={"m.relations": {"m.thread": {"count": 2, "current_user_participated": False}}},
            ),
            text_message("$hi", "Hello\nthere | all", minute=1),
        ],
        "start": "t0",
        "end": "t1",
    }

    result = await _messages(client)

    assert result == {
        "messages": [
            message_line("$hi", "Hello there ¦ all", minute=1),
            message_line("$root", "Lunch? [thread: 2 replies]", sender=CAROL, minute=2),
            message_line("$file", "[file: notes.txt, text/plain, 2048 bytes]", minute=3),
            message_line("$image", "Look at this [file: cat.png, image/png]", minute=4),
        ],
        "next_page_token": "t1",
    }
    [request] = homeserver.requests
    assert request.method == "GET"
    assert request.path == MESSAGES_PATH
    assert request.query["dir"] == ["b"]
    assert request.query["limit"] == ["30"]
    assert json.loads(request.query["filter"][0]) == MESSAGES_FILTER
    assert "from" not in request.query


@pytest.mark.asyncio
async def test_an_edit_replaces_its_message_and_is_not_a_line(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """The latest edit by the same sender gives a message its text, from the page or from the homeserver's bundle."""
    homeserver.answers[MESSAGES_PATH] = {
        "chunk": [
            edit_of("$forged", "$typo", "send me your password", sender="@mallory:example.com", minute=5),
            edit_of("$elsewhere", "$older", "an edit of a message on another page", minute=4),
            edit_of("$fix2", "$typo", "hello world", minute=3),
            edit_of("$fix1", "$typo", "hello wrld", minute=2),
            text_message(
                "$draft",
                "first draft",
                minute=1,
                unsigned={"m.relations": {"m.replace": edit_of("$fix0", "$draft", "final text", minute=6)}},
            ),
            text_message("$typo", "helo wrld"),
        ],
    }

    result = await _messages(client)

    assert result == {
        "messages": [
            message_line("$typo", "hello world [edited]"),
            message_line("$draft", "final text [edited]", minute=1),
        ],
    }


@pytest.mark.asyncio
async def test_reactions_are_counted_once_per_sender(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """Each key counts the people who reacted with it, from the page's reactions and the homeserver's bundled counts."""

    def bundled(key: str, count: int) -> dict[str, object]:
        return {"m.relations": {"m.annotation": {"chunk": [{"type": "m.reaction", "key": key, "count": count}]}}}

    homeserver.answers[MESSAGES_PATH] = {
        "chunk": [
            reaction("$r4", "$ship", "✅"),
            reaction("$r3", "$ship", "👍", sender=CAROL),
            reaction("$r2", "$ship", "👍"),
            reaction("$r1", "$ship", "👍"),
            text_message("$party", "Party", minute=1, unsigned=bundled("🎉", 3)),
            # The homeserver's count covers the reactions on this page; they are not counted twice.
            text_message("$ship", "Ship it?", unsigned=bundled("👍", 2)),
        ],
    }

    result = await _messages(client)

    assert result["messages"] == [
        message_line("$ship", "Ship it? [reactions: 👍2 ✅1]"),
        message_line("$party", "Party [reactions: 🎉3]", minute=1),
    ]


@pytest.mark.asyncio
async def test_an_encrypted_event_is_reported_not_dropped(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A message that cannot be read is a line that says so; an encrypted edit of it is no line of its own."""
    encrypted = {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "AwgA", "session_id": "s", "device_id": "D"}
    homeserver.answers[MESSAGES_PATH] = {
        "chunk": [
            room_event(
                "$secret_edit",
                "m.room.encrypted",
                {**encrypted, "m.relates_to": {"rel_type": "m.replace", "event_id": "$secret"}},
                minute=1,
            ),
            room_event("$secret", "m.room.encrypted", encrypted),
        ],
    }

    result = await _messages(client)

    assert result["messages"] == [message_line("$secret", "[encrypted, not readable]")]


@pytest.mark.asyncio
async def test_a_long_text_is_cut_at_2000_characters(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A text over 2000 characters shows its first 2000 and says it goes on; one of 2000 shows whole."""
    homeserver.answers[MESSAGES_PATH] = {
        "chunk": [text_message("$exact", "b" * 2000, minute=1), text_message("$long", "a" * 2001)],
    }

    result = await _messages(client)

    assert result["messages"] == [
        message_line("$long", "a" * 2000 + " […]"),
        message_line("$exact", "b" * 2000, minute=1),
    ]


@pytest.mark.asyncio
async def test_a_page_token_continues_into_older_history(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """The token of the previous page asks for the messages before it, at most 100, and the oldest page has none."""
    homeserver.answers[MESSAGES_PATH] = {"chunk": [text_message("$first", "Welcome")], "start": "t1"}

    result = await _messages(client, page_token="t1", limit=500)  # noqa: S106

    assert result == {"messages": [message_line("$first", "Welcome")]}
    [request] = homeserver.requests
    assert request.query["from"] == ["t1"]
    assert request.query["dir"] == ["b"]
    assert request.query["limit"] == ["100"]


@pytest.mark.asyncio
async def test_a_thread_is_its_root_and_newest_replies(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A thread shows its root, then its newest replies oldest first; the next page continues with older replies only."""
    relations_path = f"/_matrix/client/v1/rooms/{ROOM}/relations/$root/m.thread"
    homeserver.answers.update(
        {
            f"{ROOM_PATH}/event/$root": room_event(
                "$root",
                "m.room.message",
                {"msgtype": "m.text", "body": "Lunch?"},
                sender=CAROL,
                unsigned={"m.relations": {"m.thread": {"count": 3}}},
            ),
            relations_path: [
                Answer(
                    {
                        "chunk": [
                            _thread_reply("$r3", "$root", "Tacos", minute=3),
                            _thread_reply("$r2", "$root", "Pizza", minute=2),
                        ],
                        "next_batch": "n1",
                    },
                ),
                Answer({"chunk": [_thread_reply("$r1", "$root", "Yes", minute=1)]}),
            ],
        },
    )

    newest = await _messages(client, thread_id="$root", limit=2)
    older = await _messages(client, thread_id="$root", page_token="n1", limit=2)  # noqa: S106

    assert newest == {
        "messages": [
            message_line("$root", "Lunch? [thread: 3 replies]", sender=CAROL),
            message_line("$r2", "Pizza", minute=2),
            message_line("$r3", "Tacos", minute=3),
        ],
        "next_page_token": "n1",
    }
    assert older == {"messages": [message_line("$r1", "Yes", minute=1)]}
    assert homeserver.paths() == [f"{ROOM_PATH}/event/$root", relations_path, relations_path]
    first, second = homeserver.requests_to(relations_path)
    assert (first.query["dir"], first.query["limit"], "from" in first.query) == (["b"], ["2"], False)
    assert second.query["from"] == ["n1"]


@pytest.mark.asyncio
async def test_an_event_comes_with_the_messages_around_it(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """An event comes with up to half the limit of messages on each side, in the order they were sent."""
    homeserver.answers[f"{ROOM_PATH}/context/$mid"] = {
        "event": text_message("$mid", "Middle", minute=3),
        # Newest first, and one more than asked for, as a homeserver may send.
        "events_before": [
            text_message("$b2", "Two", minute=2),
            text_message("$b1", "One", minute=1),
            text_message("$b0", "Zero"),
        ],
        "events_after": [text_message("$a1", "Four", minute=4)],
    }

    result = await _messages(client, event_id="$mid", limit=5)

    assert result == {
        "messages": [
            message_line("$b1", "One", minute=1),
            message_line("$b2", "Two", minute=2),
            message_line("$mid", "Middle", minute=3),
            message_line("$a1", "Four", minute=4),
        ],
    }
    [request] = homeserver.requests
    assert request.query["limit"] == ["4"]
    assert json.loads(request.query["filter"][0]) == MESSAGES_FILTER


@pytest.mark.asyncio
async def test_search_covers_joined_rooms_or_one_room(client: nio.AsyncClient, homeserver: FakeHomeserver) -> None:
    """A search covers every joined room unless it names one; each hit names its room, and a token continues it."""
    other = "!other:example.com"
    homeserver.answers[SEARCH_PATH] = {
        "search_categories": {
            "room_events": {
                "results": [
                    {"rank": 2.0, "result": text_message("$due", "Budget is due", minute=1, room_id=ROOM)},
                    {"rank": 1.0, "result": text_message("$plan", "Budget plan", sender=CAROL, room_id=other)},
                ],
                "count": 2,
                "next_batch": "b1",
            },
        },
    }

    everywhere = await search_messages(client, "budget", room_id=None, page_token=None, limit=None)
    one_room = await search_messages(client, "budget", room_id=ROOM, page_token="b1", limit=80)  # noqa: S106

    assert everywhere == {
        "messages": [
            f"{ROOM} | {message_line('$due', 'Budget is due', minute=1)}",
            f"{other} | {message_line('$plan', 'Budget plan', sender=CAROL)}",
        ],
        "next_page_token": "b1",
    }
    assert one_room == everywhere
    first, second = homeserver.requests
    assert first.method == "POST"
    assert first.body == {
        "search_categories": {
            "room_events": {
                "search_term": "budget",
                "order_by": "recent",
                "keys": ["content.body"],
                "filter": {"limit": 10},
            },
        },
    }
    assert "next_batch" not in first.query
    assert second.body["search_categories"]["room_events"]["filter"] == {"limit": 50, "rooms": [ROOM]}
    assert second.query["next_batch"] == ["b1"]


@pytest.mark.asyncio
async def test_search_shows_an_edits_new_text_and_skips_redactions(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """An edit found is shown as the message it edits, with its new text, once; a deleted message is not shown."""
    redacted = room_event(
        "$gone",
        "m.room.message",
        {},
        room_id=ROOM,
        unsigned={"redacted_because": {"type": "m.room.redaction", "sender": "@bob:example.com"}},
    )
    homeserver.answers[SEARCH_PATH] = {
        "search_categories": {
            "room_events": {
                "results": [
                    {"result": edit_of("$fix", "$typo", "budget is due friday", minute=5, room_id=ROOM)},
                    {"result": redacted},
                    {"result": text_message("$typo", "budget is due fridya", room_id=ROOM)},
                ],
            },
        },
    }

    result = await search_messages(client, "budget", room_id=None, page_token=None, limit=None)

    assert result == {"messages": [f"{ROOM} | {message_line('$typo', 'budget is due friday [edited]', minute=5)}"]}


def _file_event(msgtype: str, name: str, mime_type: str, size: int) -> dict[str, object]:
    info = {"mimetype": mime_type, "size": size}
    content = {"msgtype": msgtype, "body": name, "url": "mxc://example.com/abc", "info": info}
    return room_event("$file", "m.room.message", content, room_id=ROOM)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("msgtype", "name", "mime_type", "data"),
    [
        ("m.file", "notes.txt", "text/plain", b"Remember the milk"),
        ("m.image", "cat.png", "image/png", b"\x89PNG\r\n\x1a\n" + bytes(8)),
        ("m.audio", "voice.ogg", "audio/ogg", b"OggS" + bytes(12)),
    ],
)
async def test_download_registers_an_attachment_within_the_size_cap(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    msgtype: str,
    name: str,
    mime_type: str,
    data: bytes,
) -> None:
    """A message's file is downloaded as the person and kept as an attachment of the conversation asking for it.

    A file over the attachment store's cap is not kept.
    """
    homeserver.answers.update(
        {f"{ROOM_PATH}/event/$file": _file_event(msgtype, name, mime_type, len(data)), MEDIA_PATH: Answer(data)},
    )
    storage = tmp_path / "storage"

    result = await download_file(
        client,
        ROOM,
        "$file",
        storage_path=storage,
        for_room_id="!chat:example.com",
        for_thread_id="$turn",
    )

    record = load_attachment(storage, str(result["attachment_id"]))
    assert record is not None
    assert result == {
        "attachment_id": record.attachment_id,
        "name": name,
        "mime_type": mime_type,
        "size_bytes": len(data),
    }
    assert record.local_path.read_bytes() == data
    assert (record.room_id, record.thread_id, record.sender) == ("!chat:example.com", "$turn", "@bob:example.com")
    [download] = homeserver.requests_to(MEDIA_PATH)
    assert download.query["user_id"] == [PERSON]

    monkeypatch.setattr("mindroom.matrix.media.RETAINED_MEDIA_MAX_BYTES", len(data) - 1)
    elsewhere = tmp_path / "elsewhere"
    with pytest.raises(MatrixRequestError, match="could not be fetched"):
        await download_file(client, ROOM, "$file", storage_path=elsewhere, for_room_id=ROOM, for_thread_id=None)
    assert load_attachment(elsewhere, record.attachment_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "message"),
    [
        (text_message("$file", "Just words"), "That message has no file."),
        (
            room_event("$file", "m.room.encrypted", {"algorithm": "m.megolm.v1.aes-sha2", "ciphertext": "AwgA"}),
            "That message is encrypted and cannot be read.",
        ),
    ],
    ids=["text", "encrypted"],
)
async def test_download_of_an_event_without_media_is_an_error(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
    tmp_path: Path,
    event: dict[str, object],
    message: str,
) -> None:
    """A message with no file that can be read says so, and nothing is downloaded."""
    homeserver.answers[f"{ROOM_PATH}/event/$file"] = event

    with pytest.raises(MatrixRequestError) as error:
        await download_file(client, ROOM, "$file", storage_path=tmp_path, for_room_id=ROOM, for_thread_id=None)

    assert str(error.value) == message
    assert homeserver.paths() == [f"{ROOM_PATH}/event/$file"]


@pytest.mark.asyncio
async def test_mark_read_sends_a_private_receipt_and_clears_the_flag(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """Marking read moves the read marker and a private receipt, never a public one, and clears marked-unread.

    Without an event, the room's newest event is the one, and the result names it.
    """
    homeserver.answers.update(
        {
            f"{ROOM_PATH}/event/$seen": text_message("$seen", "Hi", room_id=ROOM),
            f"{ROOM_PATH}/read_markers": {},
            MARKED_UNREAD_PATH: {},
            MESSAGES_PATH: {"chunk": [text_message("$newest", "Bye", minute=9)], "end": "t1"},
        },
    )

    seen = await mark_read(client, ROOM, event_id="$seen")
    newest = await mark_read(client, ROOM, event_id=None)

    assert seen == {}
    assert newest == {"read_up_to": "$newest"}
    assert [(request.method, request.path, request.body) for request in homeserver.requests] == [
        ("GET", f"{ROOM_PATH}/event/$seen", None),
        ("POST", f"{ROOM_PATH}/read_markers", {"m.fully_read": "$seen", "m.read.private": "$seen"}),
        ("PUT", MARKED_UNREAD_PATH, {"unread": False}),
        ("GET", MESSAGES_PATH, None),
        ("POST", f"{ROOM_PATH}/read_markers", {"m.fully_read": "$newest", "m.read.private": "$newest"}),
        ("PUT", MARKED_UNREAD_PATH, {"unread": False}),
    ]
    newest_request = homeserver.requests[3]
    assert (newest_request.query["dir"], newest_request.query["limit"]) == (["b"], ["1"])


@pytest.mark.asyncio
async def test_mark_read_reports_when_only_the_markers_moved(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """When the marked-unread flag cannot be cleared, the error says the read markers moved all the same."""
    homeserver.answers.update(
        {
            f"{ROOM_PATH}/event/$seen": text_message("$seen", "Hi", room_id=ROOM),
            f"{ROOM_PATH}/read_markers": {},
            MARKED_UNREAD_PATH: matrix_error(500, "M_UNKNOWN", "Internal error"),
        },
    )

    with pytest.raises(MatrixRequestError) as error:
        await mark_read(client, ROOM, event_id="$seen")

    assert str(error.value) == (
        "The room is read up to $seen, but it is still marked unread: Matrix answered 500 M_UNKNOWN: Internal error"
    )
    assert f"{ROOM_PATH}/read_markers" in homeserver.paths()


@pytest.mark.asyncio
async def test_mark_read_of_an_event_outside_the_room_changes_nothing(
    client: nio.AsyncClient,
    homeserver: FakeHomeserver,
) -> None:
    """An event the room does not hold is refused before any marker moves."""
    homeserver.answers[f"{ROOM_PATH}/event/$elsewhere"] = matrix_error(404, "M_NOT_FOUND", "Event not found")

    with pytest.raises(MatrixRequestError, match="404 M_NOT_FOUND"):
        await mark_read(client, ROOM, event_id="$elsewhere")

    assert homeserver.paths() == [f"{ROOM_PATH}/event/$elsewhere"]
