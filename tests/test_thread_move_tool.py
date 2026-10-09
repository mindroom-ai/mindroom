"""Tests for moving a thread into another room."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SKIP_MENTIONS_KEY,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    TOOL_TRACE_CONTENT_KEY,
)
from mindroom.custom_tools.thread_move import ThreadMoveTools, _plan_thread_copy, _PlannedCopy
from mindroom.matrix.client_delivery import DeliveredMatrixEvent, MatrixDeliveryFailure, MatrixDeliveryFailureKind
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.message_target import MessageTarget
from mindroom.thread_tags import RESOLVED_THREAD_TAG, ThreadTagRecord, ThreadTagsError, ThreadTagsState
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

HUMAN_ID = "@dominic:example.org"
CODE_ID = "@mindroom_code:example.org"
ABSENT_ID = "@mindroom_research:example.org"
ENTITY_IDS = {CODE_ID: "code", ABSENT_ID: "research", "@mindroom_router:example.org": ROUTER_AGENT_NAME}
TEXT_KEYS = {"msgtype", "body", "format", "formatted_body", TOOL_TRACE_CONTENT_KEY}


def _message(sender: str, content: dict[str, Any], event_id: str = "$event:example.org") -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=str(content.get("body", "")),
        event_id=event_id,
        content=content,
    )


def _plan(
    *messages: ResolvedVisibleMessage,
    display_names: dict[str, str] | None = None,
) -> list[_PlannedCopy]:
    return _plan_thread_copy(
        messages,
        entity_name_for_sender=ENTITY_IDS.get,
        target_posters=frozenset({"code", ROUTER_AGENT_NAME}),
        display_names=display_names if display_names is not None else {HUMAN_ID: "Dominic"},
    )


def test_plan_posts_entity_messages_as_their_own_account() -> None:
    """An entity joined to the target room re-posts its own message unchanged."""
    [copy] = _plan(_message(CODE_ID, {"msgtype": "m.text", "body": "done"}, "$reply:example.org"))

    assert copy.poster == "code"
    assert copy.source_event_id == "$reply:example.org"
    assert copy.content["body"] == "done"
    assert ORIGINAL_SENDER_KEY not in copy.content


def test_plan_relays_human_and_absent_entity_messages_through_router() -> None:
    """Humans and entities missing from the target room are relayed by the router."""
    human, absent = _plan(
        _message(HUMAN_ID, {"msgtype": "m.text", "body": "hi"}),
        _message(ABSENT_ID, {"msgtype": "m.text", "body": "found it"}),
    )

    assert human.poster == ROUTER_AGENT_NAME
    assert human.content[ORIGINAL_SENDER_KEY] == HUMAN_ID
    assert absent.poster == ROUTER_AGENT_NAME
    assert absent.content[ORIGINAL_SENDER_KEY] == ABSENT_ID


@pytest.mark.parametrize(
    "content",
    [
        {"msgtype": "m.notice", "body": "Thread summary"},
        {"msgtype": "m.text", "body": "...", STREAM_STATUS_KEY: STREAM_STATUS_PENDING},
        {"msgtype": "m.text", "body": "partial", STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
        {"msgtype": "m.text", "body": "waiting", STREAM_STATUS_KEY: STREAM_STATUS_APPROVAL_PENDING},
    ],
)
def test_plan_skips_notices_and_in_progress_replies(content: dict[str, Any]) -> None:
    """Runtime notices and replies still being written are not copied."""
    assert _plan(_message(CODE_ID, content)) == []


def test_plan_keeps_completed_replies() -> None:
    """A finished streamed reply is copied."""
    [copy] = _plan(
        _message(CODE_ID, {"msgtype": "m.text", "body": "all done", STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED}),
    )

    assert copy.content["body"] == "all done"


def test_plan_copies_only_allowlisted_keys() -> None:
    """Run, stream, relation, and attachment metadata never carry over, and every copy is mention-guarded."""
    content = {
        "msgtype": "m.text",
        "body": "@code please check",
        "format": "org.matrix.custom.html",
        "formatted_body": "<p>please check</p>",
        TOOL_TRACE_CONTENT_KEY: {"version": 2, "events": []},
        "m.relates_to": {"rel_type": "m.thread", "event_id": "$root:example.org"},
        "m.mentions": {"user_ids": [CODE_ID]},
        "io.mindroom.ai_run": {"run_id": "run"},
        STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED,
        "com.mindroom.attachment_ids": ["att_1"],
        "io.mindroom.interactive": {"options": []},
    }
    own, relayed = _plan(_message(CODE_ID, content), _message(HUMAN_ID, content))

    for copy in (own, relayed):
        assert copy.content[SKIP_MENTIONS_KEY] is True
        assert copy.content["m.mentions"] == {}
    assert set(own.content) == {*TEXT_KEYS, SKIP_MENTIONS_KEY, "m.mentions"}
    assert set(relayed.content) == {*TEXT_KEYS, SKIP_MENTIONS_KEY, "m.mentions", ORIGINAL_SENDER_KEY}


def test_plan_prefixes_relayed_text() -> None:
    """Relayed text shows its author because clients do not render the relay sender."""
    [copy] = _plan(
        _message(
            HUMAN_ID,
            {"msgtype": "m.text", "body": "hi", "format": "org.matrix.custom.html", "formatted_body": "<p>hi</p>"},
        ),
    )

    assert copy.content["body"] == "Dominic: hi"
    assert copy.content["formatted_body"] == "<strong>Dominic</strong>: <p>hi</p>"


def test_plan_escapes_display_name_in_formatted_prefix() -> None:
    """A display name cannot inject markup into the relayed copy."""
    [copy] = _plan(
        _message(
            HUMAN_ID,
            {"msgtype": "m.text", "body": "hi", "format": "org.matrix.custom.html", "formatted_body": "hi"},
        ),
        display_names={HUMAN_ID: "<b>Dom</b>"},
    )

    assert copy.content["formatted_body"] == "<strong>&lt;b&gt;Dom&lt;/b&gt;</strong>: hi"


def test_plan_prefixes_relayed_media_caption() -> None:
    """Relayed media keeps its file and shows the author in the caption."""
    plain, captioned = _plan(
        _message(HUMAN_ID, {"msgtype": "m.image", "body": "photo.png", "url": "mxc://example.org/abc"}),
        _message(
            HUMAN_ID,
            {"msgtype": "m.image", "body": "look", "filename": "photo.png", "url": "mxc://example.org/abc"},
        ),
    )

    assert plain.content["filename"] == "photo.png"
    assert plain.content["body"] == "Dominic: photo.png"
    assert plain.content["url"] == "mxc://example.org/abc"
    assert captioned.content["filename"] == "photo.png"
    assert captioned.content["body"] == "Dominic: look"


def test_plan_name_falls_back_to_user_id() -> None:
    """A sender without a display name is named by Matrix user ID."""
    [copy] = _plan(_message(HUMAN_ID, {"msgtype": "m.text", "body": "hi"}), display_names={})

    assert copy.content["body"] == f"{HUMAN_ID}: hi"


SOURCE_ROOM_ID = "!source:localhost"
TARGET_ROOM_ID = "!target:localhost"
ROOT_ID = "$root:localhost"
REQUESTER_ID = "@user:localhost"
MODULE = "mindroom.custom_tools.thread_move"


class _Orchestrator:
    """Lend running entities' clients the way the live orchestrator does."""

    def __init__(self, clients: dict[str, AsyncMock]) -> None:
        self.clients = clients

    def running_entity_client(self, entity_name: str) -> AsyncMock | None:
        return self.clients.get(entity_name)


@dataclass
class _Move:
    context: ToolRuntimeContext
    ids: dict[str, str]
    clients: dict[str, AsyncMock]


@dataclass
class _MatrixMocks:
    send: AsyncMock
    set_tag: AsyncMock


def _move(
    tmp_path: Path,
    *,
    thread_id: str | None = ROOT_ID,
    running: tuple[str, ...] = ("general", "code", ROUTER_AGENT_NAME),
) -> _Move:
    config = bind_runtime_paths(
        Config(agents={name: AgentConfig(display_name=name.title()) for name in ("general", "code", "research")}),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(config, runtime_paths).items()}
    clients = {name: make_matrix_client_mock(user_id=ids[name]) for name in running}
    context = make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(room_id=SOURCE_ROOM_ID, thread_id=thread_id, reply_to_event_id=None),
        requester_id=REQUESTER_ID,
        client=clients.get("general", make_matrix_client_mock(user_id=ids["general"])),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        orchestrator=_Orchestrator(clients),
    )
    return _Move(context=context, ids=ids, clients=clients)


def _thread(move: _Move) -> list[ResolvedVisibleMessage]:
    return [
        _message(REQUESTER_ID, {"msgtype": "m.text", "body": "can you two look at this"}, ROOT_ID),
        _message(move.ids["code"], {"msgtype": "m.text", "body": "@research found it?"}, "$code:localhost"),
        _message(move.ids["research"], {"msgtype": "m.text", "body": "yes"}, "$research:localhost"),
        _message(move.ids["general"], {"msgtype": "m.notice", "body": "Thread summary"}, "$summary:localhost"),
        _message(
            move.ids["general"],
            {"msgtype": "m.text", "body": "Moving...", STREAM_STATUS_KEY: STREAM_STATUS_STREAMING},
            "$current:localhost",
        ),
    ]


def _delivered_sends() -> AsyncMock:
    event_ids = iter(f"$copy{index}:localhost" for index in range(100))

    async def send(_client: object, _room_id: str, content: dict[str, Any], **_kwargs: object) -> DeliveredMatrixEvent:
        return DeliveredMatrixEvent(event_id=next(event_ids), content_sent=content)

    return AsyncMock(side_effect=send)


@contextmanager
def _matrix(
    move: _Move,
    history: list[ResolvedVisibleMessage],
    *,
    members: set[str] | None = None,
    encryption: tuple[bool | MatrixDeliveryFailure, bool | MatrixDeliveryFailure] = (False, False),
    send: AsyncMock | None = None,
    tags: ThreadTagsState | None = None,
    set_tag: AsyncMock | None = None,
    full_history: bool = True,
) -> Iterator[_MatrixMocks]:
    if members is None:
        members = {REQUESTER_ID, move.ids["general"], move.ids["code"], move.ids[ROUTER_AGENT_NAME]}
    mocks = _MatrixMocks(send=send or _delivered_sends(), set_tag=set_tag or AsyncMock())

    async def room_encryption(_client: object, room_id: str, **_kwargs: object) -> bool | MatrixDeliveryFailure:
        return encryption[0] if room_id == SOURCE_ROOM_ID else encryption[1]

    with (
        patch(
            f"{MODULE}.complete_thread_history",
            new=AsyncMock(return_value=thread_history_result(history, is_full_history=full_history)),
        ),
        patch(f"{MODULE}.get_room_members", new=AsyncMock(return_value=members)),
        patch(f"{MODULE}.resolve_room_encryption_outcome", new=AsyncMock(side_effect=room_encryption)),
        patch(f"{MODULE}.send_message_result", new=mocks.send),
        patch(f"{MODULE}.get_thread_tags", new=AsyncMock(return_value=tags)),
        patch(f"{MODULE}.set_thread_tag", new=mocks.set_tag),
        tool_runtime_context(move.context),
    ):
        yield mocks


async def _run(room_id: str = TARGET_ROOM_ID, thread_id: str | None = None) -> dict[str, Any]:
    return json.loads(await ThreadMoveTools().move_thread(room_id, thread_id))


def test_thread_move_tool_registered(tmp_path: Path) -> None:
    """The tool is one room-context capability with a single function."""
    move = _move(tmp_path)
    metadata = TOOL_METADATA["thread_move"]

    assert metadata.function_names == ("move_thread",)
    assert metadata.requires_room_context is True
    assert isinstance(
        get_tool_by_name("thread_move", move.context.runtime_paths, worker_target=None),
        ThreadMoveTools,
    )


@pytest.mark.asyncio
async def test_move_thread_without_context_returns_error() -> None:
    """The tool refuses to run outside a tool runtime context."""
    payload = await _run()

    assert payload["status"] == "error"
    assert payload["message"] == "Thread move tool context is unavailable in this runtime path."


@pytest.mark.asyncio
async def test_move_thread_requires_a_thread(tmp_path: Path) -> None:
    """A room-level conversation has no thread to move."""
    move = _move(tmp_path, thread_id=None)
    with _matrix(move, _thread(move)) as mocks:
        payload = await _run()

    assert payload["status"] == "error"
    assert "thread_id is required" in payload["message"]
    mocks.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_thread_rejects_current_room(tmp_path: Path) -> None:
    """Moving a thread into its own room would only duplicate it."""
    move = _move(tmp_path)
    with _matrix(move, _thread(move)) as mocks:
        payload = await _run(SOURCE_ROOM_ID)

    assert payload == {
        "message": "The thread is already in this room.",
        "status": "error",
        "tool": "thread_move",
    }
    mocks.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_thread_resolves_an_alias_of_an_unconfigured_room(tmp_path: Path) -> None:
    """A room MindRoom did not create is found through its Matrix alias."""
    move = _move(tmp_path)
    move.context.client.room_resolve_alias = AsyncMock(
        return_value=nio.RoomResolveAliasResponse("#ideas:localhost", TARGET_ROOM_ID, ["localhost"]),
    )
    with _matrix(move, _thread(move)) as mocks:
        payload = await _run("#ideas:localhost")

    move.context.client.room_resolve_alias.assert_awaited_once_with("#ideas:localhost")
    assert payload["status"] == "ok"
    assert {call.args[1] for call in mocks.send.await_args_list[:3]} == {TARGET_ROOM_ID}


@pytest.mark.parametrize("room_id", ["#missing:localhost", "ideas"])
@pytest.mark.asyncio
async def test_move_thread_rejects_an_unknown_room(tmp_path: Path, room_id: str) -> None:
    """A room name that resolves to no room is reported as unknown, not as an access problem."""
    move = _move(tmp_path)
    move.context.client.room_resolve_alias = AsyncMock(
        return_value=nio.RoomResolveAliasError("Room alias not found", "M_NOT_FOUND"),
    )
    with _matrix(move, _thread(move)) as mocks:
        payload = await _run(room_id)

    assert payload["message"] == f"Unknown room {room_id!r}; pass a room ID, alias, or configured room name."
    mocks.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_thread_rejects_unauthorized_target(tmp_path: Path) -> None:
    """A requester who may not act in the target room cannot move a thread there."""
    move = _move(tmp_path)
    with (
        _matrix(move, _thread(move)) as mocks,
        patch(f"{MODULE}.room_access_allowed", new=AsyncMock(return_value=False)),
    ):
        payload = await _run()

    assert payload["status"] == "error"
    assert payload["message"] == "Not authorized to access the target room."
    mocks.send.assert_not_awaited()


@pytest.mark.parametrize(
    ("running", "members_without"),
    [
        (("general", "code"), None),
        (("general", "code", ROUTER_AGENT_NAME), ROUTER_AGENT_NAME),
        (("general", "code", ROUTER_AGENT_NAME), "general"),
    ],
)
@pytest.mark.asyncio
async def test_move_thread_requires_router_and_agent_in_target(
    tmp_path: Path,
    running: tuple[str, ...],
    members_without: str | None,
) -> None:
    """The router relays copies and the acting agent writes tags, so both must be in the target room."""
    move = _move(tmp_path, running=running)
    members = {REQUESTER_ID, *move.ids.values()} - {move.ids[members_without]} if members_without else None
    with _matrix(move, _thread(move), members=members) as mocks:
        payload = await _run()

    assert payload["status"] == "error"
    expected = (
        "Invite general to the target room before moving a thread there."
        if members_without == "general"
        else "The router must be in the target room to move a thread."
    )
    assert payload["message"] == expected
    mocks.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_thread_rejects_incomplete_history(tmp_path: Path) -> None:
    """A thread whose full history cannot be read is refused instead of moved in part."""
    move = _move(tmp_path)
    with _matrix(move, _thread(move), full_history=False) as mocks:
        payload = await _run()

    assert payload["message"] == "This thread is too long to move."
    mocks.send.assert_not_awaited()


@pytest.mark.parametrize(
    ("encryption", "message"),
    [
        ((True, False), "Cannot move a thread from an encrypted room into an unencrypted room."),
        (
            (MatrixDeliveryFailure(MatrixDeliveryFailureKind.UNKNOWN_ENCRYPTION_STATE, "unknown"), False),
            "Could not determine whether the source and target rooms are encrypted.",
        ),
    ],
)
@pytest.mark.asyncio
async def test_move_thread_refuses_encrypted_to_unencrypted(
    tmp_path: Path,
    encryption: tuple[bool | MatrixDeliveryFailure, bool],
    message: str,
) -> None:
    """End-to-end encrypted content is never re-posted in plaintext."""
    move = _move(tmp_path)
    with _matrix(move, _thread(move), encryption=encryption) as mocks:
        payload = await _run()

    assert payload["message"] == message
    mocks.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_thread_rejects_thread_with_only_skipped_messages(tmp_path: Path) -> None:
    """A thread with nothing to copy is refused before anything is posted."""
    move = _move(tmp_path)
    with _matrix(move, _thread(move)[3:]) as mocks:
        payload = await _run()

    assert payload["message"] == "This thread has no messages to move."
    mocks.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_thread_copies_messages_in_order(tmp_path: Path) -> None:
    """Each copy is posted in order, by its own entity when possible, as one new thread."""
    move = _move(tmp_path)
    with _matrix(move, _thread(move)) as mocks:
        payload = await _run()

    copies = mocks.send.await_args_list[:3]
    assert [call.args[0] for call in copies] == [
        move.clients[ROUTER_AGENT_NAME],
        move.clients["code"],
        move.clients[ROUTER_AGENT_NAME],
    ]
    assert {call.args[1] for call in copies} == {TARGET_ROOM_ID}
    root, code_reply, research_reply = (call.args[2] for call in copies)
    assert "m.relates_to" not in root
    assert root[ORIGINAL_SENDER_KEY] == REQUESTER_ID
    assert code_reply["body"] == "@research found it?"
    assert code_reply[SKIP_MENTIONS_KEY] is True
    assert code_reply["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$copy0:localhost",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$copy0:localhost"},
    }
    assert research_reply[ORIGINAL_SENDER_KEY] == move.ids["research"]
    assert research_reply["m.relates_to"]["m.in_reply_to"] == {"event_id": "$copy1:localhost"}
    assert payload["status"] == "ok"
    assert payload["room_id"] == TARGET_ROOM_ID
    assert payload["thread_id"] == "$copy0:localhost"
    assert payload["link"] == "https://matrix.to/#/%21target%3Alocalhost/%24copy0%3Alocalhost?via=localhost"
    assert payload["copied"] == 3
    assert payload["skipped"] == 2


@pytest.mark.asyncio
async def test_move_thread_copies_tags_posts_notice_and_resolves_source(tmp_path: Path) -> None:
    """The copy keeps the thread's tags and the original points to it and is resolved."""
    move = _move(tmp_path)
    record = ThreadTagRecord(
        set_by="@mindroom_general:localhost",
        set_at="2026-10-09T12:00:00Z",
        data={"level": "high"},
    )
    tags = ThreadTagsState(room_id=SOURCE_ROOM_ID, thread_root_id=ROOT_ID, tags={"priority": record, "ideas": record})
    with _matrix(move, _thread(move), tags=tags) as mocks:
        payload = await _run()

    general = move.clients["general"]
    assert [call.args for call in mocks.set_tag.await_args_list] == [
        (general, TARGET_ROOM_ID, "$copy0:localhost", "ideas"),
        (general, TARGET_ROOM_ID, "$copy0:localhost", "priority"),
        (general, SOURCE_ROOM_ID, ROOT_ID, RESOLVED_THREAD_TAG),
    ]
    assert {call.kwargs["set_by"] for call in mocks.set_tag.await_args_list} == {REQUESTER_ID}
    assert mocks.set_tag.await_args_list[0].kwargs["data"] == {"level": "high"}
    notice_call = mocks.send.await_args_list[-1]
    assert notice_call.args[:2] == (general, SOURCE_ROOM_ID)
    notice = notice_call.args[2]
    assert notice["msgtype"] == "m.notice"
    assert notice["body"] == f"Moved to {payload['link']}"
    assert notice[SKIP_MENTIONS_KEY] is True
    assert notice["m.relates_to"]["event_id"] == ROOT_ID
    assert notice["m.relates_to"]["m.in_reply_to"] == {"event_id": "$current:localhost"}
    assert payload["tags_copied"] == ["ideas", "priority"]
    assert payload["warnings"] == []


@pytest.mark.asyncio
async def test_move_thread_reports_partial_copy_on_send_failure(tmp_path: Path) -> None:
    """A failed send stops the move and leaves the original thread untouched."""
    move = _move(tmp_path)
    delivered = _delivered_sends()
    send = AsyncMock(side_effect=[await delivered(None, TARGET_ROOM_ID, {}), None])
    with _matrix(move, _thread(move), send=send) as mocks:
        payload = await _run()

    assert payload["status"] == "error"
    assert payload["message"] == "Copied 1 of 3 messages before a send failed."
    assert payload["link"] == "https://matrix.to/#/%21target%3Alocalhost/%24copy0%3Alocalhost?via=localhost"
    assert mocks.send.await_count == 2
    mocks.set_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_thread_reports_tag_failure_as_warning(tmp_path: Path) -> None:
    """Once the copy exists, a tag failure is reported instead of failing the move."""
    move = _move(tmp_path)
    with _matrix(move, _thread(move), set_tag=AsyncMock(side_effect=ThreadTagsError("forbidden"))) as mocks:
        payload = await _run()

    assert payload["status"] == "ok"
    assert payload["warnings"] == ["Could not mark the original thread resolved: forbidden"]
    assert mocks.send.await_args_list[-1].args[2]["msgtype"] == "m.notice"
