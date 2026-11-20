"""Tool markers leave a reply's text at one write boundary and come back at one read boundary.

``tool_markers_out_of_text`` converts the content that goes to Matrix, as the last step of every send, and
``tool_markers_in_text`` restores it wherever the backend first reads content from Matrix. Everything between the
two works on the reply with its markers in its text, exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config.matrix import MindRoomUserConfig
from mindroom.constants import STREAM_STATUS_KEY, STREAM_STATUS_STREAMING, STREAM_VISIBLE_BODY_KEY
from mindroom.custom_tools.matrix_conversation_operations import _message_payload
from mindroom.custom_tools.matrix_user_reads import _message_lines
from mindroom.delivery_gateway import _reply_row_wire_content
from mindroom.entity_resolution import current_internal_sender_ids, entity_identity_registry
from mindroom.event_journal import DeliveryStage, MatrixDelivery, matrix_delivery_payload
from mindroom.event_journal.models import ReplyRowFacts
from mindroom.execution_preparation import _context_body_from_visible_message
from mindroom.judgment.conversation import message_judgment_text
from mindroom.matrix import large_messages
from mindroom.matrix.client_delivery import (
    DeliveredMatrixEvent,
    build_edit_event_content,
    edit_message_result,
    prepare_message_content,
    send_message_outcome,
    send_message_result,
)
from mindroom.matrix.client_visible_messages import (
    ResolvedVisibleMessage,
    bundled_replacement_body,
    extract_visible_edit_body,
    resolve_visible_event_source,
    thread_root_body_preview,
)
from mindroom.matrix.journal_ingress import ingestion_timeline_views
from mindroom.matrix.large_messages import (
    EDIT_MESSAGE_SIZE_LIMIT,
    calculate_event_size,
    should_send_oversized_nonterminal_streaming_edit,
)
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.message_builder import markdown_to_html
from mindroom.matrix.message_content import (
    extract_and_resolve_message,
    extract_edit_body,
    resolve_event_source_content,
)
from mindroom.matrix.room_history_reads import (
    find_outbox_delivery_event_id_via_room_messages,
    missing_outbox_delivery_copy_indices_via_room_messages,
)
from mindroom.requester_identity import mindroom_user_id
from mindroom.tool_system import events
from mindroom.tool_system.events import (
    ToolTraceEntry,
    _html_block_spans,
    body_without_tool_markers,
    tool_markers_in_text,
    tool_markers_out_of_text,
)
from tests.conftest import make_matrix_client_mock
from tests.cpu_budget_helpers import cpu_budget
from tests.test_matrix_api_tool import _make_context
from tests.test_mentions import _default_runtime_paths, _make_config, _runtime_paths_for

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


_ROOM = "!room:localhost"
_AGENT = "@mindroom_code:localhost"
_PERSON = "@person:localhost"
_TRACE_KEY = "io.mindroom.tool_trace"
_CASES = json.loads((Path(__file__).parent / "fixtures" / "tool-positions-v1.json").read_text(encoding="utf-8"))[
    "cases"
]
_SENT_CASES = [case for case in _CASES if case["wire"] is not None]
_SHELL = {"type": "tool_call_completed", "tool_name": "shell"}


def _case(name: str) -> dict[str, Any]:
    return next(case for case in _CASES if case["name"] == name)


def _content(body: str, html: str, version: int, events: list[Any]) -> dict[str, Any]:
    return {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": html,
        _TRACE_KEY: {"version": version, "events": events},
    }


def _marked(body: str, events: list[Any]) -> dict[str, Any]:
    """Return a reply as the backend builds it: its markers in its text, its HTML rendered from its body."""
    return _content(body, markdown_to_html(body), 2, events)


def _sent(case: dict[str, Any]) -> dict[str, Any]:
    sent = case["sent"]
    return _content(sent["body"], sent["formatted_body"], 2, sent["events"])


def _wire(case: dict[str, Any]) -> dict[str, Any]:
    wire = case["wire"]
    places = zip(case["sent"]["events"], wire["body_line"], wire["at"], strict=True)
    events = [
        {**event, **({} if line is None else {"body_line": line}), **({} if at is None else {"at": at})}
        for event, line, at in places
    ]
    return _content(wire["body"], wire["formatted_body"], 3, events)


def _event(content: dict[str, Any], event_id: str = "$reply", sender: object = _AGENT) -> dict[str, Any]:
    """Return a room message event as a homeserver serves it."""
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "room_id": _ROOM,
        "sender": sender,
        "origin_server_ts": 1_700_000_000_000,
        "content": content,
    }


def _message(content: dict[str, Any]) -> ResolvedVisibleMessage:
    return ResolvedVisibleMessage.synthetic(sender=_AGENT, body=content["body"], event_id="$m", content=content)


def _client() -> AsyncMock:
    client = make_matrix_client_mock(user_id=_AGENT)
    client.room_send = AsyncMock(return_value=nio.RoomSendResponse("$sent", _ROOM))
    return client


def _on_the_wire(client: AsyncMock) -> dict[str, Any]:
    """Return the content of the one event the client sent to Matrix."""
    client.room_send.assert_awaited_once()
    return client.room_send.await_args.kwargs["content"]


def _readers(message: ResolvedVisibleMessage) -> list[object]:
    """Return what the readers of a sent reply make of it: model context, judges, summaries, the read tool."""
    return [
        message.body,
        message.content,
        _context_body_from_visible_message(message, response_sender_id=None),
        message_judgment_text(message),
        body_without_tool_markers(message.body, message.content),
        json.dumps({key: _message_payload(message)[key] for key in ("body", "content")}, sort_keys=True),
    ]


# --- The write boundary: the last step of every send.


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
async def test_a_send_puts_the_fixture_wire_content_on_matrix(case: dict[str, Any]) -> None:
    """The shared fixture pins what Matrix gets; in doubt it gets the reply as it is, and the sender keeps the reply."""
    sent = _sent(case)
    client = _client()

    delivered = await send_message_result(client, _ROOM, sent)

    wire = _on_the_wire(client)
    assert wire is sent if case["wire"] is None else wire == _wire(case)
    assert isinstance(delivered, DeliveredMatrixEvent)
    assert delivered.content_sent is sent
    assert calculate_event_size(wire) <= calculate_event_size(sent)
    if case.get("rendered", True):
        assert case["sent"]["formatted_body"] == markdown_to_html(case["sent"]["body"])


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "Intro.\n\n"])
async def test_an_edit_goes_out_with_the_fallback_of_its_converted_replacement(prefix: str) -> None:
    """The fallback repeats the replacement after "* ", where an opening marker is no line of its own."""
    replacement = _marked(f"{prefix}🔧 `shell` [1]\n\nAnswer.", [_SHELL])
    client = _client()

    delivered = await edit_message_result(client, _ROOM, "$reply", replacement, replacement["body"])

    edit = build_edit_event_content(event_id="$reply", new_content=replacement, new_text=replacement["body"])
    wire = _on_the_wire(client)
    assert wire["m.new_content"] == tool_markers_out_of_text(replacement)
    assert wire["m.new_content"]["body"] == f"{prefix}Answer."
    assert (wire["body"], wire["formatted_body"]) == (f"* {prefix}Answer.", wire["m.new_content"]["formatted_body"])
    assert wire[_TRACE_KEY] == wire["m.new_content"][_TRACE_KEY]
    assert tool_markers_in_text(wire) == edit
    assert delivered is not None
    assert delivered.content_sent == edit


@pytest.mark.asyncio
async def test_an_edit_that_mentions_an_agent_goes_out_without_markers_in_either_layer() -> None:
    """A stream names its edit's text as it was written, while the replacement names the agent by its Matrix ID."""
    config = _make_config(_default_runtime_paths())
    text = "Looking.\n\n🔧 `shell` [1]\n\nOver to @calculator."
    replacement = format_message_with_mentions(
        config,
        _runtime_paths_for(config),
        text,
        tool_trace=[ToolTraceEntry(type="tool_call_completed", tool_name="shell")],
    )
    client = _client()

    delivered = await edit_message_result(client, _ROOM, "$reply", replacement, text)

    wire = _on_the_wire(client)
    assert replacement["body"] == "Looking.\n\n🔧 `shell` [1]\n\nOver to @actual_calculator:localhost."
    assert wire["m.new_content"]["body"] == "Looking.\n\nOver to @actual_calculator:localhost."
    assert wire["body"] == "* Looking.\n\nOver to @calculator."
    for layer in (wire, wire["m.new_content"]):
        assert "🔧" not in layer["formatted_body"]
    assert wire["m.new_content"][_TRACE_KEY]["version"] == 3
    assert delivered is not None
    assert tool_markers_in_text(wire) == delivered.content_sent
    assert delivered.content_sent["body"] == f"* {text}"


@pytest.mark.asyncio
async def test_an_edit_bound_to_its_target_when_its_row_is_claimed_sends_no_marker_in_either_layer() -> None:
    """A reply row prepared before the reply's create was acknowledged is wrapped as an edit only when it is sent."""
    replacement = _sent(_case("calls that open and end the reply"))
    client = _client()
    # As the delivery gateway freezes a reply row whose target is not known yet, and sends it once claimed.
    payload = await prepare_message_content(
        client,
        _ROOM,
        matrix_delivery_payload("principal", "$turn", DeliveryStage.EDIT, replacement),
    )
    assert isinstance(payload, dict)
    claimed = MatrixDelivery(
        delivery_id="$turn",
        stage=DeliveryStage.EDIT,
        room_id=_ROOM,
        membership_epoch=0,
        thread_id=None,
        transaction_id="tx",
        payload=payload,
        edits_event_id="$reply",
        acknowledged_event_id=None,
        created_at_ns=0,
        reply_id="reply",
        reply_row=ReplyRowFacts(placeholder_only=False, new_text=replacement["body"]),
    )
    row = _reply_row_wire_content(claimed)

    await send_message_outcome(
        client,
        _ROOM,
        row,
        operation="edit_message",
        transaction_id="tx",
        content_is_prepared=True,
    )

    wire = _on_the_wire(client)
    assert row["body"] == f"* {replacement['body']}"
    for layer in (wire, wire["m.new_content"]):
        assert "🔧" not in layer["body"]
        assert "🔧" not in layer["formatted_body"]
        assert layer[_TRACE_KEY]["version"] == 3
    assert (wire["body"], wire["m.new_content"]["body"]) == ("* Reading done.", "Reading done.")
    assert tool_markers_in_text(wire) == row


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [[None], [None, _SHELL], ["shell"], [_SHELL, 7]])
async def test_a_reply_whose_trace_has_an_entry_that_is_no_call_is_sent_as_it_is(events: list[Any]) -> None:
    """Marker [n] names entry n, so a list with a broken entry is sent on untouched, never filtered and renumbered."""
    index = events.index(_SHELL) + 1 if _SHELL in events else 1
    content = _marked(f"Before.\n\n🔧 `shell` [{index}]\n\nAfter.", events)
    client = _client()

    assert await send_message_result(client, _ROOM, content) is not None

    assert _on_the_wire(client) is content
    assert tool_markers_in_text(content) is content


@pytest.mark.asyncio
async def test_a_fenced_example_of_a_marker_keeps_its_line_in_the_plain_body() -> None:
    """A marker line that is no top-level HTML paragraph of the same call is text, which a plain client must get."""
    case = _case("a fenced example of the marker of a call that shows none stays version 2")
    client = _client()

    await send_message_result(client, _ROOM, _sent(case))

    assert _on_the_wire(client)["body"] == "Example:\n\n```\n🔧 `shell` [1]\n\ncode\n```\n\nDone."


def test_content_that_names_no_markers_to_take_out_is_sent_as_it_is() -> None:
    """Plain messages, content already converted, a trace without HTML and a warm-up frame stay the same object."""
    sent = _sent(_case("calls between two paragraphs share one position"))
    plain = {"msgtype": "m.text", "body": "🔧 `shell` [1]"}
    no_html = {"msgtype": "m.text", "body": "🔧 `shell` [1]", _TRACE_KEY: {"version": 2, "events": [_SHELL]}}
    warming = {**sent, STREAM_VISIBLE_BODY_KEY: sent["body"]}
    foreign_fallback = {**build_edit_event_content(event_id="$reply", new_content=sent, new_text="Other text.")}
    for content in (plain, tool_markers_out_of_text(sent), no_html, warming, foreign_fallback):
        assert tool_markers_out_of_text(content) is content
    assert tool_markers_in_text(sent) is sent


def _large(calls: int, prose: int) -> dict[str, Any]:
    body = "\n\n".join([*(f"🔧 `shell` [{index}]" for index in range(1, calls + 1)), "x" * prose])
    return _marked(body, [_SHELL] * calls)


@pytest.mark.asyncio
async def test_a_message_too_large_for_one_event_keeps_its_markers_in_its_preview_and_its_sidecar() -> None:
    """Sizes are those of the marked reply, so one moved to a sidecar is uploaded and previewed as it was built."""
    content = _large(3, 80_000)
    client = _client()
    upload = AsyncMock(return_value=("mxc://localhost/full", {}))

    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload):
        await send_message_result(client, _ROOM, content)

    preview = _on_the_wire(client)
    assert preview["body"].startswith("🔧 `shell` [1]\n\n🔧 `shell` [2]")
    assert _TRACE_KEY not in preview
    assert upload.await_args.args[2] == content


def _progress_edit(calls: int, prose: int) -> dict[str, Any]:
    replacement = {**_large(calls, prose), STREAM_STATUS_KEY: STREAM_STATUS_STREAMING}
    return build_edit_event_content(event_id="$reply", new_content=replacement, new_text=replacement["body"])


def test_an_oversized_progress_edit_is_paced_by_the_size_it_uploads() -> None:
    """An edit too large for one event uploads its marked content, so that size sets the wait for the next one."""
    under = _progress_edit(80, 100)
    over = _progress_edit(80, 16_000)
    assert calculate_event_size(under) <= EDIT_MESSAGE_SIZE_LIMIT < calculate_event_size(tool_markers_out_of_text(over))
    large_messages._oversized_nonterminal_streaming_edit_next_allowed_at.clear()

    with patch("mindroom.matrix.large_messages.monotonic", return_value=100.0):
        assert should_send_oversized_nonterminal_streaming_edit(
            room_id=_ROOM,
            original_event_id="$a",
            edit_content=under,
        )
        assert not large_messages._oversized_nonterminal_streaming_edit_next_allowed_at
        assert should_send_oversized_nonterminal_streaming_edit(
            room_id=_ROOM,
            original_event_id="$b",
            edit_content=over,
        )

    waits = large_messages._oversized_nonterminal_streaming_edit_next_allowed_at
    assert waits.pop((_ROOM, "$b")) == pytest.approx(100.0 + calculate_event_size(over) / 4096)


@pytest.mark.asyncio
async def test_an_edit_that_only_fits_without_its_markers_is_sized_with_them() -> None:
    """The marker bytes count toward the limits as they always did: what is sent is never larger than what was sized."""
    replacement = _large(120, 100)
    edit = build_edit_event_content(event_id="$reply", new_content=replacement, new_text=replacement["body"])
    marked, sent = calculate_event_size(edit), calculate_event_size(tool_markers_out_of_text(edit))
    assert sent < marked
    client = _client()
    upload = AsyncMock(return_value=("mxc://localhost/full", {}))

    with (
        patch("mindroom.matrix.large_messages.EDIT_MESSAGE_SIZE_LIMIT", (marked + sent) // 2),
        patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload),
    ):
        await edit_message_result(client, _ROOM, "$reply", replacement, replacement["body"])

    assert upload.await_args.args[2] == edit
    assert _TRACE_KEY not in _on_the_wire(client)["m.new_content"]


# --- The read boundary: where the backend first reads content from Matrix.


@pytest.mark.parametrize("case", _SENT_CASES, ids=[case["name"] for case in _SENT_CASES])
def test_reading_what_was_sent_gives_the_reply_back_exactly(case: dict[str, Any]) -> None:
    """Restored content is the built reply itself, so every reader reads what it always read."""
    sent, wire = _sent(case), _wire(case)

    assert tool_markers_in_text(wire) == sent
    assert tool_markers_in_text(sent) is sent
    assert _readers(_message(tool_markers_in_text(wire))) == _readers(_message(sent))


def _is_agent(sender: str) -> bool:
    return sender == _AGENT


_INTERNAL_USER = "@mindroom_user:localhost"
# Whom a read trusts for the text of a warm-up frame, and whose replies had marker lines: the first has the internal
# user, whose account a person may sign in to.
_TRUSTED, _AGENTS = [_AGENT, _INTERNAL_USER], frozenset({_AGENT})


@pytest.fixture
def runtime() -> ToolRuntimeContext:
    """Return a runtime with one agent and the internal user, each with its Matrix account."""
    context = _make_context(mindroom_user=MindRoomUserConfig())
    assert mindroom_user_id(context.config, context.runtime_paths) == _INTERNAL_USER
    assert _INTERNAL_USER in current_internal_sender_ids(context.config, context.runtime_paths)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _SENT_CASES, ids=[case["name"] for case in _SENT_CASES])
async def test_every_way_into_the_backend_reads_an_agents_reply_as_it_was_built(case: dict[str, Any]) -> None:
    """A synced event, a fetched one and one a person's account reads all carry the reply with its markers."""
    sent, wire = _sent(case), _wire(case)
    edit = build_edit_event_content(event_id="$reply", new_content=sent, new_text=sent["body"])
    wire_edit = tool_markers_out_of_text(edit)
    assert wire_edit is not edit

    views = ingestion_timeline_views(
        room_id=_ROOM,
        source=_event(wire),
        self_sender="@mindroom_other:localhost",
        provenance=nio.TimelineEventProvenance.LIVE,
        sender_is_managed=_is_agent,
    )
    assert views is not None
    inbound, projected = views
    assert inbound.source["content"] == sent
    assert projected is not None
    assert projected.content == sent

    fetched = await resolve_event_source_content(_event(wire), None, _AGENTS)
    assert fetched["content"] == sent
    message = await extract_and_resolve_message(nio.RoomMessage.parse_event(_event(wire)), agent_sender_ids=_AGENTS)
    assert (message["body"], message["content"]) == (sent["body"], sent)
    edited = await extract_edit_body(_event(wire_edit, "$edit"), None, agent_sender_ids=_AGENTS)
    assert edited == (sent["body"], sent)

    def read_by_a_person(reply: dict[str, Any], its_edit: dict[str, Any]) -> list[list[str]]:
        events = [_event(reply), _event(its_edit, "$edit")]
        return [_message_lines(events[:1], _AGENTS), _message_lines(events, _AGENTS)]

    assert read_by_a_person(wire, wire_edit) == read_by_a_person(sent, edit)


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [_PERSON, _INTERNAL_USER, [_AGENT], {"id": _AGENT}, None])
async def test_no_way_into_the_backend_puts_markers_in_a_message_that_is_no_agents(sender: object) -> None:
    """Anyone can write positions into a trace, and only an agent's reply had marker lines to put back.

    A person may sign in to the internal user's account, and a homeserver may serve a sender that is no ID.
    """
    wire = _wire(_case(_BETWEEN))
    wire_edit = {**wire, "body": f"* {wire['body']}", "m.new_content": wire}
    event, edit = _event(wire, sender=sender), _event(wire_edit, "$edit", sender)
    assert tool_markers_in_text(wire)["body"] != wire["body"]

    assert await resolve_event_source_content(event, None, _AGENTS) is event
    assert await extract_edit_body(edit, None, trusted_sender_ids=_TRUSTED, agent_sender_ids=_AGENTS) == (
        wire["body"],
        wire,
    )
    assert all("🔧" not in line for line in _message_lines([event, edit], _AGENTS))
    if not isinstance(sender, str):
        return

    views = ingestion_timeline_views(
        room_id=_ROOM,
        source=event,
        self_sender=_AGENT,
        provenance=nio.TimelineEventProvenance.LIVE,
        sender_is_managed=_is_agent,
    )
    assert views is not None
    inbound, projected = views
    assert inbound.source["content"] == wire
    assert projected is not None
    assert projected.content == wire

    message = await extract_and_resolve_message(
        nio.RoomMessage.parse_event(event),
        trusted_sender_ids=_TRUSTED,
        agent_sender_ids=_AGENTS,
    )
    assert (message["body"], message["content"]) == (wire["body"], wire)


@pytest.mark.asyncio
async def test_the_reads_that_know_the_runtime_take_the_internal_user_for_a_person(runtime: ToolRuntimeContext) -> None:
    """A read that names no senders takes them from the runtime: its router, agents and teams, and nobody else."""
    config, paths = runtime.config, runtime.runtime_paths
    agent = entity_identity_registry(config, paths).current_ids["general"].full_id
    case = _case("a separator after a call and extra blank lines stay")
    sent, wire = _sent(case), _wire(case)
    edit = build_edit_event_content(event_id="$reply", new_content=sent, new_text=sent["body"])
    wire_edit = tool_markers_out_of_text(edit)
    assert wire_edit["m.new_content"] == wire

    async def read(sender: str) -> list[object]:
        event, its_edit = _event(wire, sender=sender), _event(wire_edit, "$edit", sender)
        edited = {**_event({"msgtype": "m.text", "body": "Before."}, sender=sender)}
        edited["unsigned"] = {"m.relations": {"m.replace": {"latest_event": its_edit}}}
        source, _body = await resolve_visible_event_source(event, None, fallback_body="", **known)
        _edit_body, edit_content = await extract_visible_edit_body(its_edit, None, **known)
        return [
            source["content"],
            edit_content,
            await bundled_replacement_body(edited, client=runtime.client, **known),
            await thread_root_body_preview(nio.RoomMessage.parse_event(event), client=runtime.client, **known),
        ]

    known = {"config": config, "runtime_paths": paths}
    assert await read(agent) == [sent, sent, "After.", "After."]
    for sender in (_INTERNAL_USER, _PERSON):
        assert await read(sender) == [wire, wire, wire["body"], "--- After."]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "markers_leave"),
    [
        ("@mindroom_code:localhost", True),
        ("@Mindroom_code:localhost", True),
        ("@helper:localhost", False),
        ("", False),
        (None, False),
    ],
)
async def test_only_an_account_mindroom_chat_takes_for_an_agent_sends_its_reply_without_markers(
    user_id: str | None,
    markers_leave: bool,
) -> None:
    """MindRoom Chat puts markers back for a ``mindroom_`` username alone, so any other account's reply keeps them.

    An agent may have any username, and its reply of calls alone would be empty to everyone who reads it there.
    A client that names no account is in doubt, and in doubt the reply goes out as it was built.
    """
    sent = _sent(_case("a reply of calls alone"))
    client = _client()
    client.user_id = user_id

    await send_message_result(client, _ROOM, sent)

    wire = _on_the_wire(client)
    assert (wire["body"] == "") is markers_leave
    assert wire is sent or markers_leave


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_recovery_finds_in_the_room_the_reply_an_earlier_device_sent(edit: bool) -> None:
    """Recovery looks in the room for the exact content of an outbox row, which Matrix got without its markers."""
    sent = _sent(_case(_BETWEEN))
    frozen = build_edit_event_content(event_id="$reply", new_content=sent, new_text=sent["body"]) if edit else sent
    client = _client()
    await send_message_outcome(client, _ROOM, frozen, transaction_id="tx", content_is_prepared=True)
    wire = _on_the_wire(client)
    assert wire != frozen
    in_room = nio.RoomMessage.parse_event(_event(wire, "$sent"))
    client.room_messages = AsyncMock(return_value=nio.RoomMessagesResponse(_ROOM, [in_room], start="", end=None))

    found = await find_outbox_delivery_event_id_via_room_messages(
        client,
        _ROOM,
        delivery_sender=_AGENT,
        source_event_ids=(),
        delivery_content=frozen,
        delivery_event_type="m.room.message",
    )
    missing = await missing_outbox_delivery_copy_indices_via_room_messages(
        client,
        _ROOM,
        delivery_sender=_AGENT,
        delivery_contents=[frozen, frozen],
        delivery_event_type="m.room.message",
    )

    assert found == "$sent"
    assert missing == [1]


def test_the_model_reads_a_separator_after_a_call_as_it_always_did() -> None:
    """History drops a separator that follows a marker, which it can only do for a reply read with its markers."""
    case = _case("a separator after a call and extra blank lines stay")
    message = _message(tool_markers_in_text(_wire(case)))

    assert _context_body_from_visible_message(message, response_sender_id=None) == "After."
    assert body_without_tool_markers(message.body, message.content) == "After."


def test_a_person_reading_a_reply_of_calls_alone_sees_its_calls() -> None:
    """The reads of a person's own account keep marker lines as text, so a reply of calls alone is not empty."""
    case = _case("a reply of calls alone")

    (line,) = _message_lines([_event(_wire(case))], _AGENTS)

    assert "🔧 `shell` [1]" in line
    assert "🔧 `shell` [2] ⏳" in line
    assert "(no text)" not in line


@pytest.mark.parametrize("place", [True, 1.0, -1, 9, None, "1"])
def test_a_position_that_is_no_place_in_the_text_leaves_the_content_as_it_is(place: object) -> None:
    """The backend restores only what its write boundary wrote; MindRoom Chat alone guesses a place for the rest."""
    content = _wire(_case("calls between two paragraphs share one position"))
    content[_TRACE_KEY]["events"][0].update(body_line=place, at=place)
    assert tool_markers_in_text(content) is content


def test_positions_out_of_the_calls_order_or_on_one_layer_only_leave_the_content_as_it_is() -> None:
    """A marker has a place in the body and in the HTML, after the place of the call before it."""
    content = _wire(_case("calls that open and end the reply"))
    content[_TRACE_KEY]["events"][0].update(body_line=1, at=1)
    content[_TRACE_KEY]["events"][1].update(body_line=0, at=0)
    assert tool_markers_in_text(content) is content
    content = _wire(_case("calls that open and end the reply"))
    del content[_TRACE_KEY]["events"][0]["at"]
    assert tool_markers_in_text(content) is content


@pytest.mark.parametrize(
    "trace",
    [
        {"version": 3, "events": 5},
        {"version": 3, "events": "events"},
        {"version": 3, "events": {"at": 0}},
        {"version": 3, "events": [1, None, {"tool_name": 3, "at": 0}, {"tool_name": "", "body_line": 0}]},
        {"version": 3, "events": [None, {"tool_name": "shell", "at": 0, "body_line": 0}]},
        {"version": 3},
        {"version": 4, "events": [{"tool_name": "shell", "at": 0, "body_line": 0}]},
        [{"tool_name": "shell", "at": 0}],
    ],
)
def test_a_malformed_trace_reads_as_its_text(trace: object) -> None:
    """Any room member can write the trace key, so any value under it leaves the content as it is."""
    content = {**_content("Found it.", "<p>Found it.</p>\n", 3, []), _TRACE_KEY: trace}
    assert tool_markers_in_text(content) is content
    assert tool_markers_out_of_text(content) is content
    assert message_judgment_text(_message(content)) == "Found it."


_BETWEEN = "calls between two paragraphs share one position"
_OTHER_FALLBACKS = [
    {"body": "Something else."},
    {"body": "* Something else."},
    {"formatted_body": "<p>Something else.</p>\n"},
    {_TRACE_KEY: {"version": 2, "events": [_SHELL]}},
]


@pytest.mark.parametrize("fallback", [*_OTHER_FALLBACKS, {"body": "* Other lines.\n\n\n\n\n\nWhere the calls stand."}])
def test_an_edit_whose_fallback_is_not_its_replacement_again_is_sent_as_it_is(fallback: dict[str, Any]) -> None:
    """A fallback with other HTML, other calls, or other lines where the calls stand is someone's own."""
    sent = _sent(_case(_BETWEEN))
    edit = {**sent, "body": f"* {sent['body']}", **fallback, "m.new_content": sent}
    assert tool_markers_out_of_text(edit) is edit


@pytest.mark.asyncio
async def test_an_edit_whose_fallback_has_no_html_is_sent_as_it_is() -> None:
    """MindRoom Chat puts a fallback's markers back only beside its replacement's HTML, so without it they stay."""
    replacement = _sent(_case(_BETWEEN))
    edit = {
        "msgtype": "m.text",
        "body": f"* {replacement['body']}",
        "m.new_content": replacement,
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
    }
    client = _client()

    await send_message_result(client, _ROOM, edit)

    assert _on_the_wire(client) == edit


@pytest.mark.parametrize("fallback", _OTHER_FALLBACKS)
def test_an_edit_whose_fallback_is_not_its_replacement_again_reads_as_it_is(fallback: dict[str, Any]) -> None:
    """Only what the write boundary sends is restored, so someone's own fallback is never overwritten."""
    wire = _wire(_case(_BETWEEN))
    edit = {**wire, "body": f"* {wire['body']}", **fallback, "m.new_content": wire}
    assert tool_markers_in_text(edit) is edit


@pytest.mark.parametrize("call", [{**_SHELL, "body_line": 0, "at": 0}, _SHELL])
def test_reading_html_that_never_closes_a_tag_takes_time_in_proportion_to_its_size(call: dict[str, Any]) -> None:
    """An event's HTML is anyone's to write, so a tag is looked for once, not again from every "<" before it."""
    content = _content("Found it.", "<a " * 21_000, 3, [call])
    with cpu_budget(0.5):
        read = tool_markers_in_text(content)
    assert (read is content) == ("at" not in call)


def test_an_edit_has_its_html_read_once_on_its_way_out_and_once_on_its_way_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit's fallback shows its replacement's HTML, so that HTML is read once for both of them.

    A send reads it a second time, without its markers, to check that the content reads back exactly.
    """
    sent = _sent(_case(_BETWEEN))
    edit = build_edit_event_content(event_id="$reply", new_content=sent, new_text=sent["body"])
    read: list[str] = []
    block_spans = events._html_block_spans
    monkeypatch.setattr(events, "_html_block_spans", lambda html: read.append(html) or block_spans(html))

    wire = tool_markers_out_of_text(edit)

    assert wire is not edit
    assert read == [sent["formatted_body"], wire["formatted_body"]]
    read.clear()
    assert tool_markers_in_text(wire) == edit
    assert read == [wire["formatted_body"]]


def test_top_level_blocks() -> None:
    """Elements, void elements and runs of text that is not blank count; nesting and whitespace do not."""
    html = " <p>a<b>b</b></p>\n<hr>text <em>x</em><br/>\n<ul><li><p>c</p></li></ul>"
    assert [html[start : start + 4] for start, _end in _html_block_spans(html)] == [
        "<p>a",
        "<hr>",
        "text",
        "<em>",
        "<br/",
        "<ul>",
    ]
    # A tag name ends where MindRoom Chat's scanner ends it: at the first character outside ASCII.
    html = "prefix <xé>stuff</xé>\n<p>Next</p>\n"
    assert [html[start:end] for start, end in _html_block_spans(html)] == ["prefix ", "<xé>stuff</xé>", "<p>Next</p>"]


def test_a_trace_with_an_entry_that_is_no_call_names_no_marker() -> None:
    """Marker [n] names entry n; skipping a broken entry would give marker [1] the next entry's call."""
    content = _content("A\n\n🔧 `read_file` [1]", "", 2, [1, {**_SHELL, "args_preview": "cmd=rm"}])
    assert "shell" not in (message_judgment_text(_message(content)) or "")
