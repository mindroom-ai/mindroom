"""What a personal agent writes as its owner through `matrix_user_write`, each event marked as the agent's."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.attachments.storage import register_local_attachment
from mindroom.constants import AGENT_AUTHORED_KEY
from mindroom.custom_tools import matrix_user_writes
from mindroom.entity_resolution import current_entity_id
from mindroom.matrix.guarded_rooms import guarded_room
from tests.matrix_user_helpers import (
    OWNER,
    ROOM,
    TOKEN,
    Answer,
    FakeHomeserver,
    HomeserverRequest,
    matrix_error,
    owner_config,
    owner_runtime_paths,
    owner_toolkit,
    owner_turn,
    text_message,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

BOB = "@bob:example.com"
CAROL = "@carol:example.com"
OTHER_ROOM = "!other:example.com"
NEW_DM = "!new:example.com"
CLIENT = "/_matrix/client/v3"
ROOM_PATH = f"{CLIENT}/rooms/{ROOM}"
ENCRYPTION_PATH = f"{ROOM_PATH}/state/m.room.encryption"
OWN_EVENT_PATH = f"{ROOM_PATH}/event/$own"
UPLOAD_PATH = "/_matrix/media/v3/upload"
DIRECT_PATH = f"{CLIENT}/user/{OWNER}/account_data/m.direct"
CREATE_PATH = f"{CLIENT}/createRoom"
THREAD_PATH = f"/_matrix/client/v1/rooms/{ROOM}/relations/$root/m.thread"
ENCRYPTED = "The room is encrypted, and nothing is sent as the person into an encrypted room."
COMMAND_LIKE = 'A message sent as the person cannot start with "!" or "\\": it would read as a command.'
MEGOLM = {"algorithm": "m.megolm.v1.aes-sha2"}


def _send_path(room_id: str = ROOM, event_type: str = "m.room.message") -> str:
    """Return the path every send of ``event_type`` into ``room_id`` starts, before its transaction ID."""
    return f"{CLIENT}/rooms/{room_id}/send/{event_type}/"


def _members_path(room_id: str) -> str:
    return f"{CLIENT}/rooms/{room_id}/members"


def _members(**memberships: str) -> dict[str, object]:
    """Return a room's member list, ``alice="join"`` naming ``@alice:example.com``'s membership."""
    return {
        "chunk": [
            {
                "type": "m.room.member",
                "state_key": f"@{name}:example.com",
                "sender": f"@{name}:example.com",
                "content": {"membership": membership},
            }
            for name, membership in memberships.items()
        ],
    }


def _sent(homeserver: FakeHomeserver, room_id: str = ROOM, event_type: str = "m.room.message") -> list[dict]:
    """Return the content of each event sent into ``room_id`` as ``event_type``, in order."""
    return [
        request.body
        for request in homeserver.requests
        if request.method == "PUT" and request.path.startswith(_send_path(room_id, event_type))
    ]


def _changes(homeserver: FakeHomeserver) -> list[HomeserverRequest]:
    """Return the requests that change anything: every one but a GET."""
    return [request for request in homeserver.requests if request.method != "GET"]


@pytest.fixture(autouse=True)
def _fresh_write_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with no recent writes and no direct chat waiting to be recorded."""
    monkeypatch.setattr(matrix_user_writes, "_RECENT_WRITES", defaultdict(deque))
    monkeypatch.setattr(matrix_user_writes, "_UNRECORDED_DIRECT_ROOMS", {})


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    """The primary runtime, with the token in its environment."""
    return owner_runtime_paths(tmp_path, MATRIX_USER_APPSERVICE_TOKEN=TOKEN)


@pytest.fixture
def config(paths: RuntimePaths) -> Config:
    """The config with Alice's personal agent."""
    return owner_config(paths)


@pytest.fixture
def agent_id(config: Config, paths: RuntimePaths) -> str:
    """The Matrix ID of Alice's personal agent, which marks every event it sends as hers."""
    del config
    return current_entity_id("mind_alice", paths).full_id


@pytest.fixture
def homeserver() -> FakeHomeserver:
    """A homeserver whose rooms are unencrypted, as it answers no encryption state, and that takes every send."""
    return FakeHomeserver(
        {
            f"{_send_path()}*": Answer({"event_id": "$sent"}),
            f"{_send_path(event_type='m.reaction')}*": Answer({"event_id": "$reaction"}),
            f"{_send_path(NEW_DM)}*": Answer({"event_id": "$dm"}),
        },
    )


async def _write(config: Config, paths: RuntimePaths, homeserver: FakeHomeserver, **arguments: object) -> dict:
    """Call `matrix_user_write` in Alice's turn with her agent and return its fields."""
    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        return json.loads(await owner_toolkit(config, paths).matrix_user_write(**arguments))


def _error(action: str, message: str) -> dict[str, str]:
    return {"status": "error", "tool": "matrix_user", "action": action, "message": message}


def _ok(action: str, **fields: object) -> dict[str, object]:
    return {"status": "ok", "tool": "matrix_user", "action": action, **fields}


@pytest.mark.asyncio
async def test_send_formats_and_stamps_the_message(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    agent_id: str,
) -> None:
    """The text is formatted as the agent's own messages are, marked as the agent's, and sent as the owner."""
    result = await _write(config, paths, homeserver, action="send", room_id=ROOM, message="See **this**")

    assert result == _ok("send", room_id=ROOM, event_id="$sent")
    [content] = _sent(homeserver)
    assert content["msgtype"] == "m.text"
    assert content["body"] == "See **this**"
    assert "<strong>this</strong>" in content["formatted_body"]
    assert content[AGENT_AUTHORED_KEY] == agent_id
    [send] = _changes(homeserver)
    assert send.query["user_id"] == [OWNER]
    # The room's encryption is read first, by this call's own client.
    assert homeserver.paths()[0] == ENCRYPTION_PATH


@pytest.mark.asyncio
async def test_a_long_message_uploads_its_full_text_with_the_marker(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    agent_id: str,
) -> None:
    """History reads a long message from its uploaded full text, so that text is marked as the agent's too."""
    homeserver.answers[UPLOAD_PATH] = {"content_uri": "mxc://example.com/full"}
    text = "word " * 12_000

    result = await _write(config, paths, homeserver, action="send", room_id=ROOM, message=text)

    assert result == _ok("send", room_id=ROOM, event_id="$sent")
    [upload] = homeserver.requests_to(UPLOAD_PATH)
    assert upload.uploaded is not None
    full = json.loads(upload.uploaded)
    assert full["body"] == text.strip()
    assert full[AGENT_AUTHORED_KEY] == agent_id
    [preview] = _sent(homeserver)
    assert len(preview["body"]) < len(text)
    assert preview[AGENT_AUTHORED_KEY] == agent_id


@pytest.mark.asyncio
async def test_send_into_a_thread_and_as_a_reply(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A thread reply follows the thread's newest message; a reply names its message, in a thread or not."""
    homeserver.answers[THREAD_PATH] = {"chunk": [text_message("$latest", "Newest reply")]}

    await _write(config, paths, homeserver, action="send", room_id=ROOM, thread_id="$root", message="On it")
    await _write(config, paths, homeserver, action="send", room_id=ROOM, event_id="$question", message="Yes")
    await _write(
        config,
        paths,
        homeserver,
        action="send",
        room_id=ROOM,
        thread_id="$root",
        event_id="$question",
        message="Yes, there",
    )

    assert [content["m.relates_to"] for content in _sent(homeserver)] == [
        {
            "rel_type": "m.thread",
            "event_id": "$root",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$latest"},
        },
        {"m.in_reply_to": {"event_id": "$question"}},
        {
            "rel_type": "m.thread",
            "event_id": "$root",
            "is_falling_back": False,
            "m.in_reply_to": {"event_id": "$question"},
        },
    ]
    [newest, _] = homeserver.requests_to(THREAD_PATH)
    assert (newest.query["dir"], newest.query["limit"]) == (["b"], ["1"])


@pytest.mark.asyncio
async def test_a_thread_without_replies_continues_from_its_root(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """The first reply in a thread follows the thread's root."""
    homeserver.answers[THREAD_PATH] = {"chunk": []}

    await _write(config, paths, homeserver, action="send", room_id=ROOM, thread_id="$root", message="First")

    [content] = _sent(homeserver)
    assert content["m.relates_to"]["m.in_reply_to"] == {"event_id": "$root"}


@pytest.mark.asyncio
async def test_send_with_an_attachment(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
    agent_id: str,
) -> None:
    """A file of the turn goes out after the text, uploaded as the owner and marked as the agent's."""
    notes = tmp_path / "notes.txt"
    notes.write_text("Remember the milk")
    record = register_local_attachment(paths.storage_root, notes, kind="file", mime_type="text/plain", room_id=ROOM)
    assert record is not None
    homeserver.answers[UPLOAD_PATH] = {"content_uri": "mxc://example.com/notes"}
    homeserver.answers[f"{_send_path()}*"] = [Answer({"event_id": "$text"}), Answer({"event_id": "$file"})]

    with owner_turn(config, paths, attachment_ids=(record.attachment_id,)), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_write(
            action="send",
            room_id=ROOM,
            message="Here it is",
            attachments=[record.attachment_id],
        )

    assert json.loads(result) == _ok("send", room_id=ROOM, event_id="$text", event_ids=["$text", "$file"])
    [upload] = homeserver.requests_to(UPLOAD_PATH)
    assert upload.uploaded == b"Remember the milk"
    assert upload.query["user_id"] == [OWNER]
    [_, file] = _sent(homeserver)
    assert (file["msgtype"], file["url"], file["filename"]) == ("m.file", "mxc://example.com/notes", "notes.txt")
    assert file[AGENT_AUTHORED_KEY] == agent_id


@pytest.mark.asyncio
async def test_an_attachment_the_turn_does_not_hold_is_refused_before_any_request(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """Only the turn's own files can be sent."""
    result = await _write(config, paths, homeserver, action="send", room_id=ROOM, attachments=["att_elsewhere"])

    assert result == _error("send", "Attachment att_elsewhere is not available in this conversation.")
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_send_to_a_person_uses_their_newest_dm(config: Config, paths: RuntimePaths) -> None:
    """The newest room listed as a DM with them whose members are only the two of them is their DM."""
    old, newer, group = "!old:example.com", "!newer:example.com", "!group:example.com"
    homeserver = FakeHomeserver(
        {
            DIRECT_PATH: {BOB: [old, newer, group], CAROL: ["!carol:example.com"]},
            _members_path(group): _members(alice="join", bob="join", carol="join"),
            _members_path(newer): _members(alice="join", bob="join"),
            f"{_send_path(newer)}*": Answer({"event_id": "$dm"}),
        },
    )

    result = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")

    assert result == _ok("send", room_id=newer, event_id="$dm")
    assert [content["body"] for content in _sent(homeserver, newer)] == ["Lunch?"]
    # The oldest is not looked at, and nothing is made or recorded.
    assert homeserver.requests_to(_members_path(old)) == []
    assert len(_changes(homeserver)) == 1


@pytest.mark.asyncio
async def test_send_to_a_person_without_a_dm_creates_one_and_records_it(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """Without a DM of only the two of them, a new one is made, added to the owner's DMs, and the message sent there."""
    left = "!left:example.com"
    homeserver.answers.update(
        {
            DIRECT_PATH: {BOB: [left], CAROL: ["!carol:example.com"]},
            _members_path(left): _members(alice="join", bob="leave"),
            CREATE_PATH: {"room_id": NEW_DM},
        },
    )

    result = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")

    assert result == _ok("send", room_id=NEW_DM, event_id="$dm")
    [created, recorded, sent] = _changes(homeserver)
    assert created.body == {"preset": "trusted_private_chat", "is_direct": True, "invite": [BOB]}
    assert (recorded.method, recorded.path) == ("PUT", DIRECT_PATH)
    assert recorded.body == {BOB: [left, NEW_DM], CAROL: ["!carol:example.com"]}
    assert sent.path.startswith(_send_path(NEW_DM))


@pytest.mark.asyncio
async def test_a_failed_dm_record_names_the_created_room_and_a_retry_reuses_it(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A DM that could not be recorded sends nothing, says which room it is, and the next send to them uses it."""
    homeserver.answers.update(
        {
            # The person has no DMs yet; the first record fails, the second works.
            DIRECT_PATH: [
                matrix_error(404, "M_NOT_FOUND", "No m.direct"),
                matrix_error(500, "M_UNKNOWN", "Database unavailable"),
                matrix_error(404, "M_NOT_FOUND", "No m.direct"),
                Answer({}),
            ],
            CREATE_PATH: {"room_id": NEW_DM},
            _members_path(NEW_DM): _members(alice="join", bob="invite"),
        },
    )

    failed = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")
    retried = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")

    assert failed == _error(
        "send",
        f"Made the DM {NEW_DM} with {BOB}, but could not record it as one "
        "(Matrix answered 500 M_UNKNOWN: Database unavailable). Nothing was sent; sending again uses this room.",
    )
    assert retried == _ok("send", room_id=NEW_DM, event_id="$dm")
    assert len(homeserver.requests_to(CREATE_PATH)) == 1
    [_, recorded] = [request for request in _changes(homeserver) if request.path == DIRECT_PATH]
    assert recorded.body == {BOB: [NEW_DM]}
    assert len(_sent(homeserver, NEW_DM)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "send", "message": "Hi"},
        {"action": "react", "event_id": "$own", "emoji": "👍"},
        {"action": "edit", "event_id": "$own", "message": "Hello"},
    ],
    ids=["send", "react", "edit"],
)
async def test_nothing_is_sent_into_an_encrypted_room(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    arguments: dict[str, str],
) -> None:
    """With no device, a send into an encrypted room would go out in plain text, so nothing is sent."""
    homeserver.answers[ENCRYPTION_PATH] = MEGOLM
    homeserver.answers[OWN_EVENT_PATH] = text_message("$own", "Helo", sender=OWNER)

    result = await _write(config, paths, homeserver, room_id=ROOM, **arguments)

    assert result == _error(arguments["action"], ENCRYPTED)
    assert _changes(homeserver) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [matrix_error(403, "M_FORBIDDEN", "Not in the room"), matrix_error(500, "M_UNKNOWN", "Boom"), TimeoutError()],
    ids=["403", "500", "timeout"],
)
async def test_an_unreadable_encryption_state_counts_as_encrypted(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    answer: Answer | Exception,
) -> None:
    """Only the homeserver saying the room has no encryption state lets a message through."""
    homeserver.answers[ENCRYPTION_PATH] = answer

    result = await _write(config, paths, homeserver, action="send", room_id=ROOM, message="Hi")

    assert result["status"] == "error"
    assert result["message"].startswith("Nothing was sent: whether the room is encrypted could not be read (")
    assert _changes(homeserver) == []


@pytest.mark.asyncio
async def test_a_joined_room_is_still_checked_for_encryption_before_a_send(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """Joining a room tells the next send nothing about its encryption, which is read again."""
    homeserver.answers[f"{CLIENT}/join/{ROOM}"] = {"room_id": ROOM}
    homeserver.answers[ENCRYPTION_PATH] = MEGOLM

    joined = await _write(config, paths, homeserver, action="join", room_id=ROOM)
    sent = await _write(config, paths, homeserver, action="send", room_id=ROOM, message="Hi")

    assert joined == _ok("join", room_id=ROOM)
    assert sent == _error("send", ENCRYPTED)
    assert homeserver.paths() == [f"{CLIENT}/join/{ROOM}", ENCRYPTION_PATH]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send", "edit"])
@pytest.mark.parametrize("text", ["!help", "  !admin users list", "\\!admin x", "\\\\!x"])
async def test_a_command_like_message_is_refused(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    action: str,
    text: str,
) -> None:
    """A bridge or the homeserver would run such a message as the person's command, so it is never sent."""
    result = await _write(config, paths, homeserver, action=action, room_id=ROOM, event_id="$own", message=text)

    assert result == _error(action, COMMAND_LIKE)
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_a_message_with_an_exclamation_mark_elsewhere_is_sent(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """Only the start of a message makes a command."""
    result = await _write(config, paths, homeserver, action="send", room_id=ROOM, message="Ship it! !now")

    assert result == _ok("send", room_id=ROOM, event_id="$sent")
    assert [content["body"] for content in _sent(homeserver)] == ["Ship it! !now"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{"action": "edit", "message": "Hello"}, {"action": "redact"}],
    ids=["edit", "redact"],
)
async def test_edit_and_redact_refuse_another_persons_event(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    arguments: dict[str, str],
) -> None:
    """The owner's account may change only the owner's own messages."""
    homeserver.answers[f"{ROOM_PATH}/event/$theirs"] = text_message("$theirs", "Hi", sender=BOB)

    result = await _write(config, paths, homeserver, room_id=ROOM, event_id="$theirs", **arguments)

    assert result == _error(
        arguments["action"],
        f"Only the person's own events can be changed, and $theirs was sent by {BOB}.",
    )
    assert _changes(homeserver) == []


@pytest.mark.asyncio
async def test_react_edit_redact_send_their_events(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    agent_id: str,
) -> None:
    """A reaction, an edit on both of its layers, and a redaction each go out marked as the agent's."""
    homeserver.answers[OWN_EVENT_PATH] = text_message("$own", "Helo", sender=OWNER)
    homeserver.answers[f"{ROOM_PATH}/redact/$own/*"] = {"event_id": "$redaction"}

    reacted = await _write(config, paths, homeserver, action="react", room_id=ROOM, event_id="$own", emoji="👍")
    edited = await _write(config, paths, homeserver, action="edit", room_id=ROOM, event_id="$own", message="Hello")
    redacted = await _write(
        config,
        paths,
        homeserver,
        action="redact",
        room_id=ROOM,
        event_id="$own",
        message="Sent by mistake",
    )

    assert reacted == _ok("react", room_id=ROOM, event_id="$reaction")
    assert edited == _ok("edit", room_id=ROOM, event_id="$sent")
    assert redacted == _ok("redact", room_id=ROOM, event_id="$redaction")
    assert _sent(homeserver, event_type="m.reaction") == [
        {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$own", "key": "👍"}, AGENT_AUTHORED_KEY: agent_id},
    ]
    [edit] = _sent(homeserver)
    assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$own"}
    assert (edit["body"], edit["m.new_content"]["body"]) == ("* Hello", "Hello")
    assert edit[AGENT_AUTHORED_KEY] == edit["m.new_content"][AGENT_AUTHORED_KEY] == agent_id
    [redaction] = [request for request in _changes(homeserver) if "/redact/" in request.path]
    assert redaction.body == {"reason": "Sent by mistake", AGENT_AUTHORED_KEY: agent_id}


@pytest.mark.asyncio
async def test_join_leave_invite_create_room(config: Config, paths: RuntimePaths, homeserver: FakeHomeserver) -> None:
    """Membership changes in the owner's name: a room by ID or alias, an invitation, a new room."""
    alias = "#planning:example.com"
    homeserver.answers.update(
        {
            f"{CLIENT}/join/{alias}": {"room_id": ROOM},
            f"{ROOM_PATH}/invite": {},
            f"{CLIENT}/rooms/{OTHER_ROOM}/leave": {},
            CREATE_PATH: {"room_id": "!trip:example.com"},
        },
    )

    results = [
        await _write(config, paths, homeserver, action="join", room_id=alias),
        await _write(config, paths, homeserver, action="invite", room_id=ROOM, user_id=BOB),
        await _write(config, paths, homeserver, action="leave", room_id=OTHER_ROOM),
        await _write(config, paths, homeserver, action="create_room", name="Trip", user_id=BOB),
    ]

    assert results == [
        _ok("join", room_id=ROOM),
        _ok("invite", room_id=ROOM),
        _ok("leave", room_id=OTHER_ROOM),
        _ok("create_room", room_id="!trip:example.com"),
    ]
    assert [(request.method, request.path, request.body) for request in homeserver.requests] == [
        ("POST", f"{CLIENT}/join/{alias}", {}),
        ("POST", f"{ROOM_PATH}/invite", {"user_id": BOB}),
        ("POST", f"{CLIENT}/rooms/{OTHER_ROOM}/leave", {}),
        ("POST", CREATE_PATH, {"name": "Trip", "preset": "private_chat", "invite": [BOB]}),
    ]
    assert all(request.query["user_id"] == [OWNER] for request in homeserver.requests)


@pytest.mark.asyncio
async def test_a_created_room_has_no_managed_power_levels(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A room made as the owner gets the homeserver's defaults, as a room they make in their own client does."""
    homeserver.answers[CREATE_PATH] = {"room_id": "!trip:example.com"}

    await _write(config, paths, homeserver, action="create_room", name="Trip")

    [created] = homeserver.requests
    assert created.body == {"name": "Trip", "preset": "private_chat"}


@pytest.mark.asyncio
async def test_a_contact_room_is_refused_before_any_request(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A room the turn guards, as a contact turn guards its contact room, takes nothing written as the owner."""
    with owner_turn(config, paths), homeserver.serving_the_toolkit(), guarded_room(ROOM):
        result = await owner_toolkit(config, paths).matrix_user_write(action="send", room_id=ROOM, message="Hi")

    assert json.loads(result) == _error("send", "Nothing is written as the person into a contact room.")
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_the_rate_limit_counts_per_agent_person_and_room(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
) -> None:
    """Twelve writes per 30 seconds into one room, a file counting as one more; another room has its own count."""
    notes = tmp_path / "notes.txt"
    notes.write_text("Remember the milk")
    record = register_local_attachment(paths.storage_root, notes, kind="file", mime_type="text/plain", room_id=ROOM)
    assert record is not None
    homeserver.answers[UPLOAD_PATH] = {"content_uri": "mxc://example.com/notes"}
    homeserver.answers[f"{_send_path(OTHER_ROOM, 'm.reaction')}*"] = {"event_id": "$elsewhere"}
    toolkit = owner_toolkit(config, paths)

    with owner_turn(config, paths, attachment_ids=(record.attachment_id,)), homeserver.serving_the_toolkit():
        allowed = [
            json.loads(await toolkit.matrix_user_write(action="react", room_id=ROOM, event_id="$own", emoji="👍"))
            for _ in range(11)
        ]
        with_file = json.loads(
            await toolkit.matrix_user_write(action="send", room_id=ROOM, attachments=[record.attachment_id]),
        )
        twelfth = json.loads(await toolkit.matrix_user_write(action="react", room_id=ROOM, event_id="$own", emoji="✅"))
        thirteenth = json.loads(
            await toolkit.matrix_user_write(action="react", room_id=ROOM, event_id="$own", emoji="🎉"),
        )
        elsewhere = json.loads(
            await toolkit.matrix_user_write(action="react", room_id=OTHER_ROOM, event_id="$own", emoji="🎉"),
        )

    assert {result["status"] for result in allowed} == {"ok"}
    rate_limited = _error("send", "Rate limit exceeded for matrix_user writes (12 per 30s).")
    assert with_file == rate_limited
    assert twelfth["status"] == "ok"
    assert thirteenth == {**rate_limited, "action": "react"}
    assert elsewhere == _ok("react", room_id=OTHER_ROOM, event_id="$elsewhere")
    assert homeserver.requests_to(UPLOAD_PATH) == []
    # Each count belongs to the agent, the person it acts for, and the room.
    assert set(matrix_user_writes._RECENT_WRITES) == {("mind_alice", OWNER, ROOM), ("mind_alice", OWNER, OTHER_ROOM)}


@pytest.mark.asyncio
async def test_each_write_leaves_one_audit_line_without_the_text(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """Who wrote what kind of event where, and what came of it, is logged; what the message says is not."""
    with capture_logs() as logs:
        await _write(config, paths, homeserver, action="send", room_id=ROOM, message="The secret plan")
        homeserver.answers[ENCRYPTION_PATH] = MEGOLM
        await _write(config, paths, homeserver, action="send", room_id=ROOM, message="The secret plan")

    sent, refused = [entry for entry in logs if entry["event"] == "matrix_user_write_audit"]
    assert sent == {
        "event": "matrix_user_write_audit",
        "log_level": "warning",
        "agent": "mind_alice",
        "person": OWNER,
        "room_id": ROOM,
        "action": "send",
        "status": "ok",
        "event_id": "$sent",
        "content_keys": sent["content_keys"],
        "content_bytes": sent["content_bytes"],
        "attachments": 0,
    }
    assert {"body", "formatted_body", AGENT_AUTHORED_KEY} <= set(sent["content_keys"])
    assert sent["content_bytes"] > len("The secret plan")
    assert (refused["status"], refused["error"]) == ("error", ENCRYPTED)
    assert all("secret plan" not in str(entry) for entry in logs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"action": "send", "message": "Hi"}, "send needs exactly one of room_id and user_id."),
        (
            {"action": "send", "room_id": ROOM, "user_id": BOB, "message": "Hi"},
            "send needs exactly one of room_id and user_id.",
        ),
        ({"action": "send", "room_id": ROOM}, "send needs message, attachments, or both."),
        (
            {"action": "send", "room_id": ROOM, "event_id": "$x", "attachments": ["att_x"]},
            "A reply (event_id) needs message.",
        ),
        (
            {"action": "send", "user_id": "bob", "message": "Hi"},
            "user_id is a full Matrix ID such as @bob:example.com.",
        ),
        ({"action": "react", "room_id": ROOM, "event_id": "$x"}, "react needs emoji."),
        ({"action": "edit", "room_id": ROOM, "message": "Hi"}, "edit needs event_id."),
        ({"action": "redact", "event_id": "$x"}, "redact needs room_id, a room ID such as !abc:example.com."),
        (
            {"action": "join"},
            "join needs room_id, a room ID such as !abc:example.com or an alias such as #abc:example.com.",
        ),
        ({"action": "invite", "room_id": ROOM}, "invite needs user_id."),
        ({"action": "create_room"}, "create_room needs name."),
        ({"action": "kick"}, "action must be send, react, edit, redact, join, leave, invite, or create_room."),
    ],
)
async def test_each_action_names_what_it_is_missing(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    arguments: dict[str, object],
    message: str,
) -> None:
    """A write without what it needs says what is missing and asks nothing of the homeserver."""
    result = await _write(config, paths, homeserver, **arguments)

    assert result == _error(str(arguments["action"]), message)
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_a_failed_attachment_names_what_was_already_sent(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
) -> None:
    """A message whose file failed to go out says so and which events did, so the model does not send them again."""
    notes = tmp_path / "notes.txt"
    notes.write_text("Remember the milk")
    record = register_local_attachment(paths.storage_root, notes, kind="file", mime_type="text/plain", room_id=ROOM)
    assert record is not None
    homeserver.answers[UPLOAD_PATH] = matrix_error(413, "M_TOO_LARGE", "Too large")

    with owner_turn(config, paths, attachment_ids=(record.attachment_id,)), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_write(
            action="send",
            room_id=ROOM,
            message="Here it is",
            attachments=[record.attachment_id],
        )

    assert json.loads(result) == _error(
        "send",
        f"Attachment {record.attachment_id} could not be sent; already sent into {ROOM}: $sent.",
    )
