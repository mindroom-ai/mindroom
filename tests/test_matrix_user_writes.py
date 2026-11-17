"""What a personal agent writes as its owner through `matrix_user_write`, each event marked as the agent's."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

import aiohttp
import pytest
from structlog.testing import capture_logs

from mindroom.ai import ai_response
from mindroom.attachments.storage import register_local_attachment
from mindroom.constants import AGENT_AUTHORED_KEY
from mindroom.custom_tools import matrix_user_writes
from mindroom.entity_resolution import current_entity_id
from mindroom.matrix.guarded_rooms import guarded_room
from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment
from mindroom.matrix.user_masquerade import create_user_masquerade_client
from mindroom.response_turn import CompletedApprovalRun, PausedAnswer, ResponsePausedForApproval
from mindroom.tool_system.runtime_context import (
    get_tool_runtime_context,
    register_tool_runtime_media_attachment,
    tool_runtime_context,
)
from tests.approval_continuation_helpers import approval_continuation, decided_calls
from tests.conftest import install_model_providers, make_turn_context
from tests.delegation_helpers import approval_calls_for, approval_executor
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.matrix_user_helpers import (
    AGENT,
    OWNER,
    ROOM,
    ROOM_NEEDED,
    TOKEN,
    Answer,
    FakeHomeserver,
    HomeserverRequest,
    edit_of,
    error_result,
    matrix_error,
    ok_result,
    owner_config,
    owner_runtime_paths,
    owner_toolkit,
    owner_turn,
    room_event,
    text_message,
)

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.attachments.storage import AttachmentRecord
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.response_turn import PausedAttempt
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

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
# An alias, which the homeserver's directory resolves.
ALIAS = "#planning:example.com"
DIRECTORY_PATH = f"{CLIENT}/directory/room/{ALIAS}"
ENCRYPTED = "The room is encrypted, and nothing is sent as your owner into an encrypted room."
COMMAND_LIKE = 'A message sent as your owner cannot start with "!" or "\\": it would read as a command.'
REACTION_KEY = "emoji must be one emoji, at most 8 characters and without spaces."
MEGOLM = {"algorithm": "m.megolm.v1.aes-sha2"}
# A DM made with Bob whose record in `m.direct` failed.
DM_NOT_RECORDED = (
    f"Made the DM {NEW_DM} with {BOB}, but could not record it as one "
    "(Matrix answered 500 M_UNKNOWN: Database unavailable). "
    f"Nothing was sent; to send there, use room_id {NEW_DM}."
)
CONTACT_ROOM_REFUSED = "Nothing is written as your owner into a contact room."
UNCONFIRMED = "The homeserver did not confirm the message. It may have been sent: read the room before sending again."


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


def _direct_changes(homeserver: FakeHomeserver) -> list[HomeserverRequest]:
    """Return the requests that change the owner's `m.direct`."""
    return [request for request in _changes(homeserver) if request.path == DIRECT_PATH]


def _notes(paths: RuntimePaths, tmp_path: Path, room_id: str = ROOM, **fields: str) -> AttachmentRecord:
    """Write `notes.txt` and register it as a file of ``room_id`` with ``fields``, as an attachment of a turn is."""
    notes = tmp_path / "notes.txt"
    notes.write_text("Remember the milk")
    record = register_local_attachment(paths.storage_root, notes, kind="file", room_id=room_id, **fields)
    assert record is not None
    return record


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
    return current_entity_id(AGENT, paths).full_id


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


async def _write(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    *,
    attachment_ids: tuple[str, ...] = (),
    **arguments: object,
) -> dict:
    """Call `matrix_user_write` in Alice's turn, holding ``attachment_ids``, with her agent and return its fields."""
    with owner_turn(config, paths, attachment_ids=attachment_ids), homeserver.serving_the_toolkit():
        return json.loads(await owner_toolkit(config, paths).matrix_user_write(**arguments))


@pytest.mark.asyncio
async def test_send_formats_and_stamps_the_message(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    agent_id: str,
) -> None:
    """The text is formatted as the agent's own messages are, marked as the agent's, and sent as the owner."""
    result = await _write(config, paths, homeserver, action="send", room_id=ROOM, message="See **this**")

    assert result == ok_result("send", room_id=ROOM, event_id="$sent")
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
    """History reads a long message or edit from its uploaded full text, so that text is marked as the agent's too."""
    homeserver.answers[UPLOAD_PATH] = {"content_uri": "mxc://example.com/full"}
    homeserver.answers[OWN_EVENT_PATH] = text_message("$own", "Helo", sender=OWNER)
    text = "word " * 12_000

    sent = await _write(config, paths, homeserver, action="send", room_id=ROOM, message=text)
    edited = await _write(config, paths, homeserver, action="edit", room_id=ROOM, event_id="$own", message=text)

    assert sent == ok_result("send", room_id=ROOM, event_id="$sent")
    assert edited == ok_result("edit", room_id=ROOM, event_id="$sent")
    [message_upload, edit_upload] = homeserver.requests_to(UPLOAD_PATH)
    assert message_upload.uploaded is not None
    full = json.loads(message_upload.uploaded)
    assert full["body"] == text.strip()
    assert full[AGENT_AUTHORED_KEY] == agent_id
    assert edit_upload.uploaded is not None
    full_edit = json.loads(edit_upload.uploaded)
    assert full_edit["m.new_content"]["body"] == text.strip()
    assert full_edit[AGENT_AUTHORED_KEY] == full_edit["m.new_content"][AGENT_AUTHORED_KEY] == agent_id
    [preview, _] = _sent(homeserver)
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
    """A file of the turn goes out as one event, uploaded as the owner and marked as the agent's."""
    record = _notes(paths, tmp_path, mime_type="text/plain")
    homeserver.answers[UPLOAD_PATH] = {"content_uri": "mxc://example.com/notes"}

    with capture_logs() as logs:
        result = await _write(
            config,
            paths,
            homeserver,
            attachment_ids=(record.attachment_id,),
            action="send",
            room_id=ROOM,
            attachment=record.attachment_id,
        )

    assert result == ok_result("send", room_id=ROOM, event_id="$sent")
    [upload] = homeserver.requests_to(UPLOAD_PATH)
    assert upload.uploaded == b"Remember the milk"
    assert upload.query["user_id"] == [OWNER]
    # The token travels only in the Authorization header.
    assert TOKEN not in upload.url
    assert "access_token" not in upload.url
    [file] = _sent(homeserver)
    assert (file["msgtype"], file["url"], file["filename"]) == ("m.file", "mxc://example.com/notes", "notes.txt")
    assert file[AGENT_AUTHORED_KEY] == agent_id
    [audit] = [entry for entry in logs if entry["event"] == "matrix_user_write_audit"]
    assert (audit["status"], audit["event_id"], audit["attachments"]) == ("ok", "$sent", 1)


@pytest.mark.asyncio
async def test_a_file_sent_into_a_thread_follows_its_newest_message(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
) -> None:
    """A file goes into a thread as a text does: after the thread's newest message."""
    record = _notes(paths, tmp_path, mime_type="text/plain")
    homeserver.answers[UPLOAD_PATH] = {"content_uri": "mxc://example.com/notes"}
    homeserver.answers[THREAD_PATH] = {"chunk": [text_message("$latest", "Newest reply")]}

    await _write(
        config,
        paths,
        homeserver,
        attachment_ids=(record.attachment_id,),
        action="send",
        room_id=ROOM,
        thread_id="$root",
        attachment=record.attachment_id,
    )

    [file] = _sent(homeserver)
    assert file["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$root",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$latest"},
    }


@pytest.mark.asyncio
async def test_a_message_and_a_file_in_one_send_are_refused_before_any_request(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
) -> None:
    """A send is one event, so it cannot end with one of two sent: a text and a file are two calls."""
    record = _notes(paths, tmp_path, mime_type="text/plain")

    result = await _write(
        config,
        paths,
        homeserver,
        attachment_ids=(record.attachment_id,),
        action="send",
        room_id=ROOM,
        message="Here it is",
        attachment=record.attachment_id,
    )

    assert result == error_result(
        "send",
        "send takes message or attachment, not both: send the text and the file as two calls.",
    )
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_an_attachment_the_turn_does_not_hold_is_refused_before_any_request(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """Only the turn's own files can be sent."""
    result = await _write(config, paths, homeserver, action="send", room_id=ROOM, attachment="att_elsewhere")

    assert result == error_result("send", "Attachment ID is not available in this context: att_elsewhere")
    assert homeserver.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_for",
    [{"room_id": OTHER_ROOM}, {"thread_id": "$elsewhere"}],
    ids=["another-room", "another-thread"],
)
async def test_a_file_stored_for_another_conversation_is_refused_before_any_request(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
    stored_for: dict[str, str],
) -> None:
    """A stored file the turn does not hold is sent only when it was stored for the turn's own room and thread."""
    record = _notes(paths, tmp_path, **stored_for)

    result = await _write(config, paths, homeserver, action="send", room_id=ROOM, attachment=record.attachment_id)

    assert result == error_result("send", f"Attachment ID is not available in this context: {record.attachment_id}")
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_an_ephemeral_attachment_is_named_as_such(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """An encrypted file the turn holds only in memory has no stored copy to upload, and the refusal says which it is."""
    media = RuntimeEncryptedMediaAttachment(
        attachment_id="att_media",
        filename="photo.png",
        url="mxc://example.com/media",
        key="key",
        iv="iv",
        sha256="hash",
        mime_type="image/png",
        size=1,
    )

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        turn = get_tool_runtime_context()
        assert turn is not None
        register_tool_runtime_media_attachment(turn, media)
        result = await owner_toolkit(config, paths).matrix_user_write(
            action="send",
            room_id=ROOM,
            attachment="att_media",
        )

    assert json.loads(result) == error_result(
        "send",
        "Ephemeral attachment att_media can be sent but has no local file.",
    )
    assert homeserver.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["!admin users list", "  \\!admin x", "\u200b!x"])
async def test_a_command_like_file_name_is_refused(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
    name: str,
) -> None:
    """A file goes out with its name as its text, which a bridge or the homeserver would run as the owner's command.

    Whoever sent a file named it, so a downloaded file can carry such a name.
    """
    record = _notes(paths, tmp_path, filename=name)

    result = await _write(
        config,
        paths,
        homeserver,
        attachment_ids=(record.attachment_id,),
        action="send",
        room_id=ROOM,
        attachment=record.attachment_id,
    )

    assert result == error_result(
        "send",
        f"Attachment {record.attachment_id}'s file name is sent as its text. {COMMAND_LIKE}",
    )
    assert homeserver.requests == []


def _new_turn(config: Config, paths: RuntimePaths) -> ToolRuntimeContext:
    """Return the context of a turn of Alice's with her agent in ``ROOM``, built anew: it holds no attachments."""
    with owner_turn(config, paths):
        turn = get_tool_runtime_context()
    assert turn is not None
    return turn


async def _run_until_a_write_waits(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    monkeypatch: pytest.MonkeyPatch,
    model: ScriptedModel,
) -> PausedAttempt:
    """Run a reply of Alice's agent to her with ``model`` until a write it calls waits for her to confirm it."""
    install_model_providers(monkeypatch, {"default": model})
    # Without background tool jobs, the call that waits pauses the reply.
    config.background_tool_jobs.enabled = False
    turn = _new_turn(config, paths)
    identity = turn.execution_identity
    reply = replace(
        make_turn_context(AGENT, session_id=identity.session_id, room_id=ROOM, requester_id=OWNER),
        prompt="Send the notes to the planning room.",
        execution_identity=identity,
        supports_native_tool_approval=True,
    )
    with (
        tool_runtime_context(turn),
        homeserver.serving_the_toolkit(),
        pytest.raises(ResponsePausedForApproval) as waiting,
    ):
        await ai_response(reply, paths, config)
    return waiting.value.paused


async def _confirm(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    paused: PausedAttempt,
) -> CompletedApprovalRun | PausedAttempt:
    """Confirm the write ``paused`` waits for and continue its run, as the approval runner does once Alice confirms.

    The run continues in a context built anew for the same room, thread, and requester, which holds only the
    attachments of Alice's own message, here none, and nothing an earlier call of the run added to its context.
    """
    turn = _new_turn(config, paths)
    identity = turn.execution_identity
    waiting = approval_calls_for(paused, default_agent=AGENT, toolkit_names={"matrix_user_write": "matrix_user"})
    continuation = approval_continuation(
        run_id=paused.run_id,
        session_id=paused.session_id,
        entity_name=AGENT,
        room_id=identity.room_id,
        thread_id=identity.thread_id,
        requester_id=OWNER,
        state="claimed",
        calls=decided_calls(waiting, {call.tool_call_id: True for call in waiting}),
        runtime_model_name=paused.runtime_model_name,
    )
    with homeserver.serving_the_toolkit():
        return await approval_executor(config, paths).continue_run(
            continuation,
            paused_answer=PausedAnswer(text=paused.response_text, tool_trace=paused.tool_trace),
            tool_context=turn,
            tool_trace_collector=[],
            quiet=False,
            progress=None,
            system_enrichment_items=(),
        )


@pytest.mark.asyncio
async def test_a_file_downloaded_in_a_run_is_sent_once_the_owner_confirms(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    agent_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A send waits for the owner and then runs in a context built anew, where a file the run downloaded still goes out.

    Nothing is written before the owner confirms. The file is uploaded as the owner and marked as the agent's.
    """
    files = "!files:example.com"
    content = {
        "msgtype": "m.file",
        "body": "notes.txt",
        "url": "mxc://example.com/abc",
        "info": {"mimetype": "text/plain"},
    }
    homeserver.answers.update(
        {
            f"{CLIENT}/rooms/{files}/event/$file": room_event("$file", "m.room.message", content),
            "/_matrix/client/v1/media/download/example.com/abc": Answer(b"Remember the milk"),
            UPLOAD_PATH: {"content_uri": "mxc://example.com/copy"},
        },
    )
    send = tool_call("matrix_user_write", "send")

    async def name_the_download() -> None:
        # The model sends the attachment that its download's result named.
        fetched = json.loads(str(model.requests[-1].messages[-1].content))
        arguments = {"action": "send", "room_id": ROOM, "attachment": fetched["attachment_id"]}
        send["function"]["arguments"] = json.dumps(arguments)

    model = ScriptedModel(
        [
            calls(tool_call("matrix_user_read", "fetch", action="download", room_id=files, event_id="$file")),
            [Await(name_the_download), *calls(send)],
            answer("Sent."),
        ],
    )

    paused = await _run_until_a_write_waits(config, paths, homeserver, monkeypatch, model)

    assert [waiting.tool_name for waiting in paused.tools] == ["matrix_user_write"]
    assert _changes(homeserver) == []

    result = await _confirm(config, paths, homeserver, paused)

    assert isinstance(result, CompletedApprovalRun)
    sent = json.loads(str(model.requests[-1].messages[-1].content))
    assert sent == ok_result("send", room_id=ROOM, event_id="$sent")
    [upload] = homeserver.requests_to(UPLOAD_PATH)
    assert upload.uploaded == b"Remember the milk"
    assert upload.query["user_id"] == [OWNER]
    [file] = _sent(homeserver)
    assert (file["msgtype"], file["url"], file["body"]) == ("m.file", "mxc://example.com/copy", "notes.txt")
    assert file[AGENT_AUTHORED_KEY] == agent_id
    [send_request] = [request for request in _changes(homeserver) if request.path.startswith(_send_path())]
    assert send_request.query["user_id"] == [OWNER]


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

    assert result == ok_result("send", room_id=newer, event_id="$dm")
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

    assert result == ok_result("send", room_id=NEW_DM, event_id="$dm")
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
    """A DM that could not be recorded sends nothing and says which room to send to; the next send to them uses it."""
    homeserver.answers.update(
        {
            # The person has no DMs yet. Each send reads `m.direct`, then reads it again before recording: the first
            # record fails, the second works.
            DIRECT_PATH: [
                matrix_error(404, "M_NOT_FOUND", "No m.direct"),
                matrix_error(404, "M_NOT_FOUND", "No m.direct"),
                matrix_error(500, "M_UNKNOWN", "Database unavailable"),
                matrix_error(404, "M_NOT_FOUND", "No m.direct"),
                matrix_error(404, "M_NOT_FOUND", "No m.direct"),
                Answer({}),
            ],
            CREATE_PATH: {"room_id": NEW_DM},
            _members_path(NEW_DM): _members(alice="join", bob="invite"),
        },
    )

    failed = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")
    retried = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")

    assert failed == error_result("send", DM_NOT_RECORDED)
    assert retried == ok_result("send", room_id=NEW_DM, event_id="$dm")
    assert len(homeserver.requests_to(CREATE_PATH)) == 1
    [_, recorded] = _direct_changes(homeserver)
    assert recorded.body == {BOB: [NEW_DM]}
    assert len(_sent(homeserver, NEW_DM)) == 1


@pytest.mark.asyncio
async def test_recording_a_new_dm_keeps_what_the_owner_changed_meanwhile(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """`m.direct` is written whole, so the copy written is the one read just before, not the one read at the start."""
    dave = {"@dave:example.com": ["!d:example.com"]}
    homeserver.answers.update(
        {
            DIRECT_PATH: [
                {CAROL: ["!c:example.com"]},
                {CAROL: ["!c:example.com"], **dave},
            ],
            CREATE_PATH: {"room_id": NEW_DM},
        },
    )

    result = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")

    assert result == ok_result("send", room_id=NEW_DM, event_id="$dm")
    [recorded] = _direct_changes(homeserver)
    assert recorded.body == {CAROL: ["!c:example.com"], **dave, BOB: [NEW_DM]}


@pytest.mark.asyncio
async def test_a_failed_second_read_records_nothing_and_names_the_room(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """When `m.direct` cannot be read again before the write, the early copy is not written in its place."""
    homeserver.answers.update(
        {
            DIRECT_PATH: [
                {CAROL: ["!c:example.com"]},
                matrix_error(500, "M_UNKNOWN", "Database unavailable"),
            ],
            CREATE_PATH: {"room_id": NEW_DM},
        },
    )

    result = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")

    assert result == error_result("send", DM_NOT_RECORDED)
    assert _direct_changes(homeserver) == []


@dataclass
class _SlowHomeserver(FakeHomeserver):
    """A homeserver that lets another call's request in before it answers one, and keeps the `m.direct` it is given."""

    direct: dict[str, object] = field(default_factory=dict)
    # The ID each new room gets, in turn.
    new_rooms: list[str] = field(default_factory=list)

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
        await asyncio.sleep(0)
        return await super().request(method, url, **kwargs)

    def _answer(self, path: str) -> Answer:
        request = self.requests[-1]
        if path == DIRECT_PATH:
            if request.method == "PUT":
                assert isinstance(request.body, dict)
                self.direct = request.body
            return Answer(self.direct)
        if path == CREATE_PATH:
            return Answer({"room_id": self.new_rooms.pop(0)})
        return super()._answer(path)


async def _write_both(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    first: dict[str, object],
    second: dict[str, object],
) -> list[dict]:
    """Make two `matrix_user_write` calls of Alice's agent at once and return the fields of each."""
    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        toolkit = owner_toolkit(config, paths)
        results = await asyncio.gather(toolkit.matrix_user_write(**first), toolkit.matrix_user_write(**second))
    return [json.loads(result) for result in results]


@pytest.mark.asyncio
async def test_two_first_sends_to_one_person_at_once_make_one_dm(config: Config, paths: RuntimePaths) -> None:
    """The second call waits for the first to make and record the DM, then sends there too."""
    homeserver = _SlowHomeserver(
        {
            _members_path(NEW_DM): _members(alice="join", bob="invite"),
            f"{_send_path(NEW_DM)}*": Answer({"event_id": "$dm"}),
        },
        new_rooms=[NEW_DM, "!second:example.com"],
    )

    results = await _write_both(
        config,
        paths,
        homeserver,
        {"action": "send", "user_id": BOB, "message": "Lunch?"},
        {"action": "send", "user_id": BOB, "message": "At one?"},
    )

    assert results == [ok_result("send", room_id=NEW_DM, event_id="$dm")] * 2
    assert len(homeserver.requests_to(CREATE_PATH)) == 1
    assert homeserver.direct == {BOB: [NEW_DM]}
    assert sorted(content["body"] for content in _sent(homeserver, NEW_DM)) == ["At one?", "Lunch?"]


@pytest.mark.asyncio
async def test_first_sends_to_two_people_at_once_record_both_dms(config: Config, paths: RuntimePaths) -> None:
    """`m.direct` is written whole, so the second call reads it only after the first has written its DM there."""
    carol_dm = "!carol-dm:example.com"
    homeserver = _SlowHomeserver(
        {
            f"{_send_path(NEW_DM)}*": Answer({"event_id": "$dm"}),
            f"{_send_path(carol_dm)}*": Answer({"event_id": "$carol"}),
        },
        new_rooms=[NEW_DM, carol_dm],
    )

    results = await _write_both(
        config,
        paths,
        homeserver,
        {"action": "send", "user_id": BOB, "message": "Lunch?"},
        {"action": "send", "user_id": CAROL, "message": "Lunch?"},
    )

    assert results == [
        ok_result("send", room_id=NEW_DM, event_id="$dm"),
        ok_result("send", room_id=carol_dm, event_id="$carol"),
    ]
    assert homeserver.direct == {BOB: [NEW_DM], CAROL: [carol_dm]}


@pytest.mark.asyncio
async def test_a_new_dm_that_is_encrypted_names_the_room_it_made(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A homeserver that encrypts private rooms makes the new DM encrypted: nothing is sent, and the refusal names it."""
    homeserver.answers.update(
        {
            DIRECT_PATH: {},
            CREATE_PATH: {"room_id": NEW_DM},
            f"{CLIENT}/rooms/{NEW_DM}/state/m.room.encryption": MEGOLM,
        },
    )

    result = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")

    assert result == error_result("send", f"Made the DM {NEW_DM} with {BOB}. {ENCRYPTED}")
    assert [request.path for request in _changes(homeserver)] == [CREATE_PATH, DIRECT_PATH]
    assert _sent(homeserver, NEW_DM) == []


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

    assert result == error_result(arguments["action"], ENCRYPTED)
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

    assert joined == ok_result("join", room_id=ROOM)
    assert sent == error_result("send", ENCRYPTED)
    assert homeserver.paths() == [f"{CLIENT}/join/{ROOM}", ENCRYPTION_PATH]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["send", "edit"])
@pytest.mark.parametrize(
    "text",
    ["!help", "  !admin users list", "\\!admin x", "\\\\!x", "\ufeff!x", "\u200b!x", "\u2060\\\\!x"],
)
async def test_a_command_like_message_is_refused(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    action: str,
    text: str,
) -> None:
    """A bridge or the homeserver would run such a message as the person's command, so it is never sent.

    Invisible characters before the command do not hide it: a recipient may drop them first.
    """
    result = await _write(config, paths, homeserver, action=action, room_id=ROOM, event_id="$own", message=text)

    assert result == error_result(action, COMMAND_LIKE)
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_a_message_with_an_exclamation_mark_elsewhere_is_sent(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """Only the start of a message makes a command."""
    result = await _write(config, paths, homeserver, action="send", room_id=ROOM, message="Ship it! !now")

    assert result == ok_result("send", room_id=ROOM, event_id="$sent")
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

    assert result == error_result(
        arguments["action"],
        f"Only your owner's own events can be changed, and $theirs was sent by {BOB}.",
    )
    assert _changes(homeserver) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["redact", "edit"])
@pytest.mark.parametrize(
    "state",
    [
        room_event("$own", "m.room.create", {"room_version": "11"}, sender=OWNER, state_key=""),
        room_event("$own", "m.room.name", {"name": "Planning"}, sender=OWNER, state_key=""),
        room_event("$own", "m.room.member", {"membership": "join"}, sender=OWNER, state_key=OWNER),
    ],
    ids=["create", "name", "member"],
)
async def test_a_room_setting_cannot_be_redacted_or_edited(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    action: str,
    state: dict[str, object],
) -> None:
    """The owner sent the events that made and set up their rooms, but only their messages and reactions are offered."""
    homeserver.answers[OWN_EVENT_PATH] = state

    result = await _write(config, paths, homeserver, action=action, room_id=ROOM, event_id="$own", message="Hello")

    assert result == error_result(
        action,
        f"Only messages and reactions can be changed this way, and $own is room state of type {state['type']}.",
    )
    assert _changes(homeserver) == []
    assert len(homeserver.requests_to(OWN_EVENT_PATH)) == 1


@pytest.mark.asyncio
async def test_only_a_message_can_be_edited(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """An edit replaces a message's text, so the owner's reaction is not edited, though it can be removed."""
    reaction = {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$message", "key": "👍"}}
    homeserver.answers[OWN_EVENT_PATH] = room_event("$own", "m.reaction", reaction, sender=OWNER)
    homeserver.answers[f"{ROOM_PATH}/redact/$own/*"] = {"event_id": "$redaction"}

    edited = await _write(config, paths, homeserver, action="edit", room_id=ROOM, event_id="$own", message="Hello")
    assert edited == error_result("edit", "Only messages can be edited, and $own is of type m.reaction.")
    assert _changes(homeserver) == []

    redacted = await _write(config, paths, homeserver, action="redact", room_id=ROOM, event_id="$own")
    assert redacted == ok_result("redact", room_id=ROOM, event_id="$redaction")


@pytest.mark.asyncio
async def test_an_edit_of_an_edit_replaces_the_original_message(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """An edit's result names its replacement event, and editing that again edits the message it replaced.

    Clients ignore a replacement of a replacement, so sent as named it would change nothing anyone sees.
    """
    homeserver.answers[OWN_EVENT_PATH] = text_message("$own", "Helo", sender=OWNER)
    homeserver.answers[f"{ROOM_PATH}/event/$edit"] = edit_of("$edit", "$own", "Hello", sender=OWNER)

    result = await _write(config, paths, homeserver, action="edit", room_id=ROOM, event_id="$edit", message="Hi all")

    assert result == ok_result("edit", room_id=ROOM, event_id="$sent")
    [edit] = _sent(homeserver)
    assert edit["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$own"}
    assert edit["m.new_content"]["body"] == "Hi all"


@pytest.mark.asyncio
async def test_an_edit_of_an_edit_checks_the_message_it_replaced(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """The message an edit names, through the owner's own replacement event, must be the owner's too."""
    homeserver.answers[f"{ROOM_PATH}/event/$edit"] = edit_of("$edit", "$theirs", "Hello", sender=OWNER)
    homeserver.answers[f"{ROOM_PATH}/event/$theirs"] = text_message("$theirs", "Hi", sender=BOB)

    result = await _write(config, paths, homeserver, action="edit", room_id=ROOM, event_id="$edit", message="Hi all")

    assert result == error_result(
        "edit",
        f"Only your owner's own events can be changed, and $theirs was sent by {BOB}.",
    )
    assert _changes(homeserver) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("emoji", "refused"),
    [("👍" * 9, True), ("👍 👍", True), ("👍" * 8, False)],
    ids=["nine-characters", "a-space", "eight-characters"],
)
async def test_a_reaction_must_be_one_short_emoji(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    emoji: str,
    *,
    refused: bool,
) -> None:
    """A reaction acknowledges a message, as one the `react` tool sends does, so a sentence is never one."""
    homeserver.answers[OWN_EVENT_PATH] = text_message("$own", "Helo", sender=OWNER)

    result = await _write(config, paths, homeserver, action="react", room_id=ROOM, event_id="$own", emoji=emoji)

    reacted = [content["m.relates_to"]["key"] for content in _sent(homeserver, event_type="m.reaction")]
    if refused:
        assert result == error_result("react", REACTION_KEY)
        assert homeserver.requests == []
    else:
        assert result == ok_result("react", room_id=ROOM, event_id="$reaction")
        assert reacted == [emoji]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held", "refusal"),
    [
        (None, "Matrix answered 404 M_NOT_FOUND: Not found"),
        (text_message("$elsewhere", "Hi", room_id=OTHER_ROOM), "That event is in another room."),
    ],
    ids=["not-found", "another-room"],
)
async def test_react_refuses_an_event_the_room_does_not_hold(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    held: dict[str, object] | None,
    refusal: str,
) -> None:
    """A reaction to an event the room does not hold would be sent and seen by nobody, so its target is read first."""
    if held is not None:
        homeserver.answers[f"{ROOM_PATH}/event/$elsewhere"] = held

    result = await _write(config, paths, homeserver, action="react", room_id=ROOM, event_id="$elsewhere", emoji="👍")

    assert result == error_result("react", refusal)
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

    assert reacted == ok_result("react", room_id=ROOM, event_id="$reaction")
    assert edited == ok_result("edit", room_id=ROOM, event_id="$sent")
    assert redacted == ok_result("redact", room_id=ROOM, event_id="$redaction")
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
async def test_every_built_content_carries_the_marker_before_the_client_stamps_it(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
    agent_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each event the toolkit builds is marked as the agent's before the client marks it again on the wire."""
    record = _notes(paths, tmp_path, mime_type="text/plain")
    homeserver.answers.update(
        {
            UPLOAD_PATH: {"content_uri": "mxc://example.com/notes"},
            OWN_EVENT_PATH: text_message("$own", "Helo", sender=OWNER),
            f"{ROOM_PATH}/redact/$own/*": {"event_id": "$redaction"},
        },
    )
    # What each event-creating request hands the client, by the event's type or as a redaction.
    handed: list[tuple[str, dict[str, object]]] = []

    def create(*args: Any, **kwargs: Any) -> nio.AsyncClient:  # noqa: ANN401
        client = homeserver.serve(create_user_masquerade_client(*args, **kwargs))
        send = client.send

        async def handed_send(method: str, path: str, data: object = None, *rest: Any) -> Any:  # noqa: ANN401
            event = re.search(r"/rooms/[^/]+/(?:send/([^/]+)|(redact))/", unquote(path))
            if event is not None:
                handed.append((event.group(1) or event.group(2), json.loads(str(data))))
            return await send(method, path, data, *rest)

        monkeypatch.setattr(client, "send", handed_send)
        return client

    monkeypatch.setattr("mindroom.custom_tools.matrix_user.create_user_masquerade_client", create)
    toolkit = owner_toolkit(config, paths)
    with owner_turn(config, paths, attachment_ids=(record.attachment_id,)):
        results = [
            json.loads(await toolkit.matrix_user_write(room_id=ROOM, **arguments))
            for arguments in [
                {"action": "send", "message": "Here it is"},
                {"action": "send", "attachment": record.attachment_id},
                {"action": "react", "event_id": "$own", "emoji": "👍"},
                {"action": "edit", "event_id": "$own", "message": "Hello"},
                {"action": "redact", "event_id": "$own"},
            ]
        ]

    assert [result["status"] for result in results] == ["ok"] * 5
    assert [kind for kind, _ in handed] == [
        "m.room.message",
        "m.room.message",
        "m.reaction",
        "m.room.message",
        "redact",
    ]
    text, file, reaction, edit, redaction = [content for _, content in handed]
    assert file["msgtype"] == "m.file"
    new_content = edit["m.new_content"]
    assert isinstance(new_content, dict)
    for content in (text, file, reaction, edit, new_content, redaction):
        assert content[AGENT_AUTHORED_KEY] == agent_id


@pytest.mark.asyncio
async def test_join_leave_invite_create_room(config: Config, paths: RuntimePaths, homeserver: FakeHomeserver) -> None:
    """Membership changes in the owner's name: a room by ID or alias, an invitation, a new room."""
    homeserver.answers.update(
        {
            f"{CLIENT}/join/{ALIAS}": {"room_id": ROOM},
            f"{ROOM_PATH}/invite": {},
            f"{CLIENT}/rooms/{OTHER_ROOM}/leave": {},
            CREATE_PATH: {"room_id": "!trip:example.com"},
        },
    )

    results = [
        await _write(config, paths, homeserver, action="join", room_id=ALIAS),
        await _write(config, paths, homeserver, action="invite", room_id=ROOM, user_id=BOB),
        await _write(config, paths, homeserver, action="leave", room_id=OTHER_ROOM),
        await _write(config, paths, homeserver, action="create_room", name="Trip", user_id=BOB),
    ]

    assert results == [
        ok_result("join", room_id=ROOM),
        ok_result("invite", room_id=ROOM),
        ok_result("leave", room_id=OTHER_ROOM),
        ok_result("create_room", room_id="!trip:example.com"),
    ]
    assert [(request.method, request.path, request.body) for request in homeserver.requests] == [
        ("POST", f"{CLIENT}/join/{ALIAS}", {}),
        ("POST", f"{ROOM_PATH}/invite", {"user_id": BOB}),
        ("POST", f"{CLIENT}/rooms/{OTHER_ROOM}/leave", {}),
        ("POST", CREATE_PATH, {"name": "Trip", "preset": "private_chat", "invite": [BOB]}),
    ]
    assert all(request.query["user_id"] == [OWNER] for request in homeserver.requests)


@pytest.mark.asyncio
async def test_join_passes_an_alias_to_the_homeserver_as_it_is(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A join by room ID has no servers to try for a room elsewhere, so the alias itself goes to the homeserver."""
    homeserver.answers.update(
        {
            DIRECTORY_PATH: {"room_id": ROOM, "servers": ["example.com"]},
            f"{CLIENT}/join/{ALIAS}": {"room_id": OTHER_ROOM},
        },
    )

    result = await _write(config, paths, homeserver, action="join", room_id=ALIAS)

    assert result == ok_result("join", room_id=OTHER_ROOM)
    [join] = homeserver.requests
    assert (join.method, urlsplit(join.url).path) == ("POST", f"{CLIENT}/join/{quote(ALIAS, safe='')}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{"action": "send", "message": "Hi"}, {"action": "react", "event_id": "$own", "emoji": "👍"}],
    ids=["send", "react"],
)
async def test_a_room_named_by_alias_is_resolved_for_writes(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    arguments: dict[str, str],
) -> None:
    """The room the alias names is the one checked, counted, guarded, written to, and reported."""
    homeserver.answers.update(
        {
            DIRECTORY_PATH: {"room_id": OTHER_ROOM, "servers": ["example.com"]},
            f"{CLIENT}/rooms/{OTHER_ROOM}/event/$own": text_message("$own", "Helo", sender=OWNER),
            f"{_send_path(OTHER_ROOM)}*": {"event_id": "$sent"},
            f"{_send_path(OTHER_ROOM, 'm.reaction')}*": {"event_id": "$sent"},
        },
    )
    action = arguments["action"]

    result = await _write(config, paths, homeserver, room_id=ALIAS, **arguments)
    with owner_turn(config, paths), homeserver.serving_the_toolkit(), guarded_room(OTHER_ROOM):
        guarded = json.loads(await owner_toolkit(config, paths).matrix_user_write(room_id=ALIAS, **arguments))

    assert result == ok_result(action, room_id=OTHER_ROOM, event_id="$sent")
    assert guarded == error_result(action, CONTACT_ROOM_REFUSED)
    lookup, encryption, *_, sent, guarded_lookup = homeserver.requests
    assert (lookup.method, lookup.path, lookup.query["user_id"]) == ("GET", DIRECTORY_PATH, [OWNER])
    assert (encryption.method, encryption.path) == ("GET", f"{CLIENT}/rooms/{OTHER_ROOM}/state/m.room.encryption")
    assert sent.path.startswith(f"{CLIENT}/rooms/{OTHER_ROOM}/send/")
    assert all(OTHER_ROOM in request.path for request in homeserver.requests[1:-1])
    assert guarded_lookup.path == DIRECTORY_PATH
    assert set(matrix_user_writes._RECENT_WRITES) == {(AGENT, OWNER, OTHER_ROOM)}


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

    assert json.loads(result) == error_result("send", CONTACT_ROOM_REFUSED)
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_the_rate_limit_counts_per_agent_person_and_room(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
) -> None:
    """Twelve writes per 30 seconds into one room, a file being one write like any other; another room has its own count."""
    record = _notes(paths, tmp_path, mime_type="text/plain")
    homeserver.answers.update(
        {
            UPLOAD_PATH: {"content_uri": "mxc://example.com/notes"},
            OWN_EVENT_PATH: text_message("$own", "Helo", sender=OWNER),
            f"{CLIENT}/rooms/{OTHER_ROOM}/event/$own": text_message("$own", "Helo", sender=OWNER),
            f"{_send_path(OTHER_ROOM, 'm.reaction')}*": {"event_id": "$elsewhere"},
        },
    )
    toolkit = owner_toolkit(config, paths)

    with owner_turn(config, paths, attachment_ids=(record.attachment_id,)), homeserver.serving_the_toolkit():
        allowed = [
            json.loads(await toolkit.matrix_user_write(action="react", room_id=ROOM, event_id="$own", emoji="👍"))
            for _ in range(11)
        ]
        twelfth = json.loads(
            await toolkit.matrix_user_write(action="send", room_id=ROOM, attachment=record.attachment_id),
        )
        thirteenth = json.loads(
            await toolkit.matrix_user_write(action="react", room_id=ROOM, event_id="$own", emoji="🎉"),
        )
        elsewhere = json.loads(
            await toolkit.matrix_user_write(action="react", room_id=OTHER_ROOM, event_id="$own", emoji="🎉"),
        )

    assert {result["status"] for result in allowed} == {"ok"}
    assert twelfth == ok_result("send", room_id=ROOM, event_id="$sent")
    assert thirteenth == error_result("react", "Rate limit exceeded for matrix_user writes (12 per 30s).")
    assert elsewhere == ok_result("react", room_id=OTHER_ROOM, event_id="$elsewhere")
    # Each count belongs to the agent, the person it acts for, and the room.
    assert set(matrix_user_writes._RECENT_WRITES) == {(AGENT, OWNER, ROOM), (AGENT, OWNER, OTHER_ROOM)}


@pytest.mark.asyncio
async def test_a_stray_room_id_does_not_give_create_room_its_own_rate_limit(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """create_room takes no room_id, so varying one given anyway leaves every create_room one count."""
    homeserver.answers[CREATE_PATH] = {"room_id": NEW_DM}
    toolkit = owner_toolkit(config, paths)

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        results = [
            json.loads(
                await toolkit.matrix_user_write(action="create_room", name="Trip", room_id=f"!stray{n}:example.com"),
            )
            for n in range(13)
        ]

    assert {result["status"] for result in results[:12]} == {"ok"}
    assert results[12] == error_result("create_room", "Rate limit exceeded for matrix_user writes (12 per 30s).")


@pytest.mark.asyncio
async def test_a_failed_create_room_does_not_log_a_stray_room_id(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """The audit line of a create_room that failed names no room, whatever room_id it was given."""
    homeserver.answers[CREATE_PATH] = matrix_error(500, "M_UNKNOWN", "Database unavailable")

    with capture_logs() as logs:
        failed = await _write(config, paths, homeserver, action="create_room", name="Trip", room_id=OTHER_ROOM)

    assert failed["status"] == "error"
    [audit] = [entry for entry in logs if entry["event"] == "matrix_user_write_audit"]
    assert audit["room_id"] is None


@pytest.mark.asyncio
async def test_a_stray_attachment_on_react_counts_for_nothing(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """Only send takes an attachment: one given to a reaction, even one the turn does not hold, is not looked at."""
    homeserver.answers[OWN_EVENT_PATH] = text_message("$own", "Helo", sender=OWNER)

    with capture_logs() as logs:
        result = await _write(
            config,
            paths,
            homeserver,
            action="react",
            room_id=ROOM,
            event_id="$own",
            emoji="👍",
            attachment="att_missing",
        )

    assert result == ok_result("react", room_id=ROOM, event_id="$reaction")
    [audit] = [entry for entry in logs if entry["event"] == "matrix_user_write_audit"]
    assert audit["attachments"] == 0


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
        "agent": AGENT,
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


@dataclass
class _HangingHomeserver(FakeHomeserver):
    """A homeserver that never answers a send into ``NEW_DM``, and says when one has arrived."""

    send_arrived: asyncio.Event = field(default_factory=asyncio.Event)

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:  # noqa: ANN401
        if unquote(urlsplit(url).path).startswith(_send_path(NEW_DM)):
            self.send_arrived.set()
            await asyncio.Event().wait()
        return await super().request(method, url, **kwargs)


@pytest.mark.asyncio
async def test_a_write_cancelled_midway_leaves_its_audit_line(config: Config, paths: RuntimePaths) -> None:
    """A write stopped after it made a DM, while its message was going out, is logged as interrupted, with the room."""
    homeserver = _HangingHomeserver({DIRECT_PATH: {}, CREATE_PATH: {"room_id": NEW_DM}})

    with capture_logs() as logs:
        writing = asyncio.create_task(
            _write(config, paths, homeserver, action="send", user_id=BOB, message="The secret plan"),
        )
        await homeserver.send_arrived.wait()
        writing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writing

    [audit] = [entry for entry in logs if entry["event"] == "matrix_user_write_audit"]
    assert (audit["status"], audit["error"]) == ("interrupted", "CancelledError")
    assert (audit["action"], audit["room_id"], audit["invited"], audit["event_id"]) == ("send", NEW_DM, BOB, None)
    assert all("secret plan" not in str(entry) for entry in logs)


@pytest.mark.asyncio
async def test_a_write_that_fails_unexpectedly_leaves_its_audit_line(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A failure that is no failed request is raised as it is, after the write is logged as interrupted."""
    homeserver.answers[f"{ROOM_PATH}/invite"] = RuntimeError("The connection pool is closed")

    with capture_logs() as logs, pytest.raises(RuntimeError, match="connection pool"):
        await _write(config, paths, homeserver, action="invite", room_id=ROOM, user_id=BOB)

    [audit] = [entry for entry in logs if entry["event"] == "matrix_user_write_audit"]
    assert (audit["status"], audit["error"], audit["invited"]) == ("interrupted", "RuntimeError", BOB)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "invite", "room_id": ROOM, "user_id": BOB},
        {"action": "create_room", "name": "Trip", "user_id": BOB},
        {"action": "send", "user_id": BOB, "message": "Lunch?"},
    ],
    ids=["invite", "create_room", "new-dm"],
)
async def test_the_audit_line_names_whom_a_write_invited(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    arguments: dict[str, str],
) -> None:
    """Whom the owner's account invited is logged, into a room, a new room, or a DM made for a send."""
    homeserver.answers.update({f"{ROOM_PATH}/invite": {}, CREATE_PATH: {"room_id": NEW_DM}, DIRECT_PATH: {}})

    with capture_logs() as logs:
        result = await _write(config, paths, homeserver, **arguments)

    assert result["status"] == "ok"
    [audit] = [entry for entry in logs if entry["event"] == "matrix_user_write_audit"]
    assert audit["invited"] == BOB


@pytest.mark.asyncio
async def test_the_audit_line_of_an_unrecorded_dm_names_its_room(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A DM made but not recorded is logged with its room and whom it invited; reused later, with its room alone."""
    unavailable = matrix_error(500, "M_UNKNOWN", "Database unavailable")
    no_direct = matrix_error(404, "M_NOT_FOUND", "No m.direct")
    homeserver.answers.update(
        {
            # Each send reads `m.direct`, then fails to read it again before recording.
            DIRECT_PATH: [no_direct, unavailable, no_direct, unavailable],
            CREATE_PATH: {"room_id": NEW_DM},
            _members_path(NEW_DM): _members(alice="join", bob="invite"),
        },
    )

    with capture_logs() as logs:
        made = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")
        reused = await _write(config, paths, homeserver, action="send", user_id=BOB, message="Lunch?")

    assert made["status"] == reused["status"] == "error"
    first, second = [entry for entry in logs if entry["event"] == "matrix_user_write_audit"]
    assert (first["room_id"], first["invited"]) == (NEW_DM, BOB)
    assert second["room_id"] == NEW_DM
    assert "invited" not in second
    assert len(homeserver.requests_to(CREATE_PATH)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "blank"),
    [
        ({"room_id": ROOM}, {"event_id": ""}),
        ({"room_id": ROOM}, {"thread_id": " "}),
        ({"room_id": ROOM}, {"user_id": ""}),
        ({"user_id": BOB}, {"room_id": "  "}),
        ({"room_id": ROOM}, {"attachment": " "}),
    ],
    ids=["event_id", "thread_id", "user_id", "room_id", "attachment"],
)
async def test_a_blank_write_argument_is_left_out(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    arguments: dict[str, str],
    blank: dict[str, str],
) -> None:
    """A blank argument, as a model may send for one it means to leave out, is no reply target, thread, recipient, or file."""
    homeserver.answers.update(
        {
            DIRECT_PATH: {BOB: [NEW_DM]},
            _members_path(NEW_DM): _members(alice="join", bob="join"),
        },
    )

    without = await _write(config, paths, homeserver, action="send", message="Yes", **arguments)
    with_blank = await _write(config, paths, homeserver, action="send", message="Yes", **arguments, **blank)

    assert without["status"] == "ok"
    assert with_blank == without
    room_id = without["room_id"]
    first, second = _sent(homeserver, room_id)
    assert second == first


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"action": "send", "message": "Hi"}, "send needs exactly one of room_id and user_id."),
        (
            {"action": "send", "room_id": ROOM, "user_id": BOB, "message": "Hi"},
            "send needs exactly one of room_id and user_id.",
        ),
        ({"action": "send", "room_id": ROOM}, "send needs message or attachment."),
        (
            {"action": "send", "room_id": ROOM, "event_id": "$x", "attachment": "att_x"},
            "A reply (event_id) needs message.",
        ),
        (
            {"action": "send", "user_id": "bob", "message": "Hi"},
            "user_id is a full Matrix ID such as @bob:example.com.",
        ),
        ({"action": "react", "room_id": ROOM, "event_id": "$x"}, "react needs emoji."),
        ({"action": "edit", "room_id": ROOM, "message": "Hi"}, "edit needs event_id."),
        ({"action": "redact", "event_id": "$x"}, f"redact {ROOM_NEEDED}"),
        ({"action": "join"}, f"join {ROOM_NEEDED}"),
        ({"action": "leave", "room_id": " "}, f"leave {ROOM_NEEDED}"),
        ({"action": "invite", "room_id": ROOM}, "invite needs user_id."),
        ({"action": "create_room"}, "create_room needs name."),
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

    assert result == error_result(str(arguments["action"]), message)
    assert homeserver.requests == []


def _event_write_arguments(kind: str, record: AttachmentRecord) -> dict[str, str]:
    """Return the arguments of a write of ``kind`` into ``ROOM`` that sends one event; a file's is ``record``."""
    return {
        "text": {"action": "send", "message": "Lunch?"},
        "file": {"action": "send", "attachment": record.attachment_id},
        "react": {"action": "react", "event_id": "$own", "emoji": "👍"},
        "edit": {"action": "edit", "event_id": "$own", "message": "Hello"},
    }[kind]


async def _write_with_send_answer(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
    kind: str,
    answer: Answer | Exception,
) -> dict:
    """Make a write of ``kind`` into ``ROOM`` whose event's send the homeserver answers with ``answer``."""
    record = _notes(paths, tmp_path, mime_type="text/plain")
    homeserver.answers.update(
        {
            OWN_EVENT_PATH: text_message("$own", "Helo", sender=OWNER),
            UPLOAD_PATH: {"content_uri": "mxc://example.com/notes"},
            f"{_send_path()}*": answer,
            f"{_send_path(event_type='m.reaction')}*": answer,
        },
    )
    return await _write(
        config,
        paths,
        homeserver,
        attachment_ids=(record.attachment_id,),
        room_id=ROOM,
        **_event_write_arguments(kind, record),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "file", "react", "edit"])
@pytest.mark.parametrize(
    "answer",
    [
        # The connection dropped while the answer was read, after the request had gone out.
        aiohttp.ClientPayloadError("Response payload is not completed"),
        matrix_error(500, "M_UNKNOWN", "Internal server error"),
        matrix_error(502, "M_UNKNOWN", "Bad gateway"),
        Answer("The upstream server timed out", status=504),
    ],
    ids=["lost-answer", "500", "502", "504-not-json"],
)
async def test_a_send_the_homeserver_did_not_confirm_says_it_may_have_been_sent(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
    kind: str,
    answer: Answer | Exception,
) -> None:
    """A send without the homeserver's own refusal may be in the room, and sending again would post it twice."""
    result = await _write_with_send_answer(config, paths, homeserver, tmp_path, kind, answer)

    assert result == error_result(_event_write_arguments(kind, _notes(paths, tmp_path))["action"], UNCONFIRMED)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "file", "react", "edit"])
@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (matrix_error(403, "M_FORBIDDEN", "You cannot post here"), "RoomSendError: M_FORBIDDEN You cannot post here"),
        (matrix_error(413, "M_TOO_LARGE", "Event too large"), "Event too large"),
    ],
    ids=["403", "413"],
)
async def test_a_send_the_homeserver_refused_says_nothing_was_sent(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
    kind: str,
    answer: Answer,
    why: str,
) -> None:
    """A refusal with a 4xx status is the homeserver saying it took nothing, so the send can be made again."""
    result = await _write_with_send_answer(config, paths, homeserver, tmp_path, kind, answer)

    assert result["message"] == f"Nothing was sent: {why}."


@pytest.mark.asyncio
async def test_a_file_that_could_not_be_uploaded_says_nothing_was_sent(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    tmp_path: Path,
) -> None:
    """A file whose upload failed never became an event, so nothing was sent."""
    record = _notes(paths, tmp_path, mime_type="text/plain")
    homeserver.answers[UPLOAD_PATH] = matrix_error(413, "M_TOO_LARGE", "Too large")

    result = await _write(
        config,
        paths,
        homeserver,
        attachment_ids=(record.attachment_id,),
        action="send",
        room_id=ROOM,
        attachment=record.attachment_id,
    )

    assert result == error_result(
        "send",
        f"Nothing was sent: attachment {record.attachment_id} could not be uploaded.",
    )
    assert _sent(homeserver) == []
