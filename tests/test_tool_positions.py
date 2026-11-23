"""Tool markers leave a reply's text at one write boundary and come back at one read boundary.

``tool_markers_out_of_text`` converts the content that goes to Matrix, as the last step of every send, and
``tool_markers_in_text`` restores it wherever the backend first reads content from Matrix. Everything between the
two works on the reply with its markers in its text, exactly as before.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom import reply_lifecycle as rl
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.constants import (
    DURABLE_DELIVERY_ID_KEY,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    STREAM_VISIBLE_BODY_KEY,
)
from mindroom.custom_tools.matrix_conversation_operations import _message_payload
from mindroom.custom_tools.matrix_user_reads import _message_lines
from mindroom.delivery_gateway import SendTextRequest, _reply_row_wire_content
from mindroom.entity_resolution import current_internal_sender_ids, entity_identity_registry
from mindroom.event_journal import DeliveryStage, MatrixDelivery, matrix_delivery_payload
from mindroom.event_journal.models import ConversationPage, ReplyRowFacts, VisibleMessage
from mindroom.execution_preparation import _context_body_from_visible_message
from mindroom.judgment.conversation import conversation_context_before_sources, message_judgment_text
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
from mindroom.matrix.conversation_reads import projected_thread_history
from mindroom.matrix.journal_ingress import ingestion_timeline_views
from mindroom.matrix.large_messages import (
    EDIT_MESSAGE_SIZE_LIMIT,
    calculate_event_size,
    should_send_oversized_nonterminal_streaming_edit,
)
from mindroom.matrix.media import decrypt_media_bytes
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
from mindroom.message_target import MessageTarget
from mindroom.mid_turn import MidTurnGate, MidTurnOutcome, QueuedMessage
from mindroom.reply_presentation import Presentation
from mindroom.reply_scope import initial_write, terminal_write
from mindroom.tool_system import events
from mindroom.tool_system.events import (
    ToolTraceEntry,
    _html_block_spans,
    body_without_tool_markers,
    tool_markers_in_text,
    tool_markers_out_of_text,
)
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, test_runtime_paths
from tests.cpu_budget_helpers import cpu_budget
from tests.identity_helpers import persist_entity_accounts
from tests.reply_span_helpers import reply_span
from tests.test_matrix_api_tool import _make_context
from tests.test_mentions import _default_runtime_paths, _make_config, _runtime_paths_for
from tests.test_response_delivery_gateway import _delivered_event_response, _gateway
from tests.tool_position_helpers import (
    _CASES,
    _KEPT_FALLBACKS,
    _KEPT_PLACEHOLDERS,
    _SENT_CASES,
    _SHELL,
    _TRACE_KEY,
    _case,
    _content,
    _marked,
    _sent,
    _wire,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import EventJournalStore, PrincipalStore, ProjectedEvent
    from mindroom.judgment.state import JudgmentMessage
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


_ROOM = "!room:localhost"
_AGENT = "@mindroom_code:localhost"
_PERSON = "@person:localhost"


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
    if case["wire"] is None or not case.get("written", True):
        assert wire is sent
        assert wire["body"] == case["sent"]["body"]
    else:
        assert wire == _wire(case)
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


def _reply_gateway(tmp_path: Path, outbox: PrincipalStore, room_send: AsyncMock) -> DeliveryGateway:
    """Return a gateway that writes an agent's reply rows to ``outbox`` and sends them with ``room_send``."""
    gateway = _gateway(tmp_path, outbox)
    client = gateway.deps.runtime.client
    client.user_id = _AGENT
    client.room_send = room_send

    def observed(room_id: str, event_id: str) -> nio.RoomGetEventResponse:
        response = _delivered_event_response(room_id, event_id)
        response.event.sender = _AGENT
        return response

    client.room_get_event = AsyncMock(side_effect=observed)
    return gateway


def _sent_events(room_send: AsyncMock) -> list[dict[str, Any]]:
    """Return the content of each event the client sent to Matrix, in order."""
    return [call.kwargs["content"] for call in room_send.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("uploads", [True, False], ids=["sidecar", "upload-failure-fallback"])
async def test_a_late_bound_sidecar_preview_freezes_clean_preview_body_as_row_new_text(
    tmp_path: Path,
    journal_store: EventJournalStore,
    uploads: bool,
) -> None:
    """A late-bound row preview freezes its clean marker-free preview body as ``new_text`` for claim-time wrapping."""
    outbox = journal_store.principal("agent@alice")
    room_send = AsyncMock(
        side_effect=[None, nio.RoomSendResponse("$reply", _ROOM), nio.RoomSendResponse("$edit", _ROOM)],
    )
    gateway = _reply_gateway(tmp_path, outbox, room_send)
    target = MessageTarget.resolve(_ROOM, None, "$cause")
    replacement = _large(3, 80_000)
    sidecars: list[bytes] = []

    async def upload(_client: object, data: bytes, **_kwargs: object) -> tuple[object, None]:
        sidecars.append(data)
        return (nio.UploadResponse("mxc://localhost/full") if uploads else nio.UploadError("refused")), None

    with patch("mindroom.matrix.large_messages.upload_media_bytes", new=upload):
        async with reply_span(outbox, source_event_id="$cause", room_id=_ROOM) as handle:
            # Matrix refuses the placeholder, so the answer is written before the reply has an event.
            placeholder = initial_write(handle, Presentation(), placeholder_only=True)
            assert await gateway.send_text(SendTextRequest(target, "Thinking...", reply_write=placeholder)) is None
            await gateway._deliver_reply_write(
                terminal_write(handle, Presentation(), state=rl.ReplyState.COMPLETED),
                target=target,
                content=replacement,
                new_text=replacement["body"],
                result=None,
            )

    row = await outbox.load_matrix_delivery(delivery_id="$cause", stage=DeliveryStage.FINAL)
    assert row is not None
    assert row.reply_row is not None
    assert (row.edits_event_id, row.acknowledged_event_id) == ("$reply", "$edit")
    assert "m.new_content" not in row.payload
    assert row.reply_row.new_text == row.payload["body"]
    placeholder_sent, placeholder_resent, wire = _sent_events(room_send)
    assert placeholder_sent == placeholder_resent
    assert wire["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$reply"}
    assert wire["m.new_content"] == row.payload
    assert wire["body"] == f"* {row.payload['body']}"
    assert wire["body"].startswith("* xxxx")
    assert ("io.mindroom.long_text" in wire["m.new_content"]) is uploads
    _assert_no_raw_markers_on_wire(wire)
    if uploads:
        frozen = {**replacement, DURABLE_DELIVERY_ID_KEY: row.payload[DURABLE_DELIVERY_ID_KEY]}
        file = row.payload["file"]
        uploaded = decrypt_media_bytes(
            sidecars[0],
            key=file["key"]["k"],
            sha256=file["hashes"]["sha256"],
            iv=file["iv"],
        )
        assert json.loads(uploaded) == frozen


def test_pre_upgrade_late_bound_sidecar_row_keeps_verbatim_new_text_for_exact_recovery() -> None:
    """Claim-time wrapping must preserve a pre-upgrade row's frozen new_text verbatim so exact recovery never resends."""
    preview_payload = {
        "msgtype": "m.file",
        "body": "clean preview",
        "format": "org.matrix.custom.html",
        "formatted_body": "<p>clean preview</p>\n",
        "url": "mxc://localhost/full",
        "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
    }
    pre_upgrade_new_text = "🔧 `shell` [1]\n\npre-upgrade original text"
    claimed = MatrixDelivery(
        delivery_id="$turn",
        stage=DeliveryStage.FINAL,
        room_id=_ROOM,
        membership_epoch=0,
        thread_id=None,
        transaction_id="tx",
        payload=preview_payload,
        edits_event_id="$reply",
        acknowledged_event_id=None,
        created_at_ns=0,
        reply_id="reply",
        reply_row=ReplyRowFacts(placeholder_only=False, new_text=pre_upgrade_new_text),
    )

    wrapped = _reply_row_wire_content(claimed)

    assert wrapped["body"] == f"* {pre_upgrade_new_text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("calls", [1, 2])
@pytest.mark.parametrize("stream_status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
async def test_a_canonical_streaming_reply_of_calls_alone_sends_thinking_placeholder_and_restores_exactly(
    calls: int,
    stream_status: str,
    edit: bool,
) -> None:
    """Non-terminal tool-calls-only replies go out as v3 with Thinking... and placeholder_body=True."""
    body = "\n\n".join(f"🔧 `shell` [{index}]" for index in range(1, calls + 1))
    built = {**_marked(body, [_SHELL] * calls), STREAM_STATUS_KEY: stream_status}
    content = build_edit_event_content(event_id="$reply", new_content=built, new_text=body) if edit else built
    client = _client()

    await send_message_result(client, _ROOM, content)

    wire = _on_the_wire(client)
    inner = wire["m.new_content"] if edit else wire
    assert inner["body"] == "Thinking..."
    assert inner["formatted_body"] == "<p>Thinking...</p>\n"
    assert inner[_TRACE_KEY]["version"] == 3
    assert inner[_TRACE_KEY]["placeholder_body"] is True
    assert "🔧" not in inner["body"]
    assert "🔧" not in inner["formatted_body"]
    if edit:
        assert wire["body"] == "* Thinking..."
        assert wire["formatted_body"] == "<p>Thinking...</p>\n"
        assert "🔧" not in wire["body"]
    # Even a reader that does not put markers back sees non-empty placeholder text.
    assert message_judgment_text(_message(inner)) == "Thinking..."
    # Restoring markers reproduces the built content byte-for-byte without placeholder_body.
    restored = tool_markers_in_text(wire)
    assert restored == content
    restored_inner = restored["m.new_content"] if edit else restored
    assert "placeholder_body" not in restored_inner[_TRACE_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", ["create", "edit", "edit-bound-when-claimed"])
@pytest.mark.parametrize("stream_status", [STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING])
async def test_an_outbox_row_of_calls_alone_is_sent_with_its_markers_while_its_reply_streams(
    stream_status: str,
    row: str,
) -> None:
    """A stream that opens with a call creates its reply through the outbox, while the reply is still pending.

    Recovery finds a row's event again by its exact content, and the release before this one does not restore a
    reply from ``placeholder_body``: after a rollback it would send the row a second time. So a row, which carries
    its delivery identity, goes out as it was built, and only a write outside the outbox says it is thinking.
    """
    built = {
        **_marked("🔧 `shell` [1] ⏳", [{**_SHELL, "type": "tool_call_started"}]),
        STREAM_STATUS_KEY: stream_status,
    }
    edit = build_edit_event_content(event_id="$reply", new_content=built, new_text=built["body"])
    direct = built if row == "create" else edit
    stage = DeliveryStage.INITIAL if row == "create" else DeliveryStage.EDIT
    # As the delivery gateway freezes the row, and as it wraps an edit whose target its create bound later.
    frozen = matrix_delivery_payload("principal", "$turn", stage, built if row == "edit-bound-when-claimed" else direct)
    claimed = MatrixDelivery(
        delivery_id="$turn",
        stage=stage,
        room_id=_ROOM,
        membership_epoch=0,
        thread_id=None,
        transaction_id="tx",
        payload=frozen,
        edits_event_id=None if row == "create" else "$reply",
        acknowledged_event_id=None,
        created_at_ns=0,
        reply_id="reply",
        reply_row=ReplyRowFacts(placeholder_only=False, new_text=built["body"]),
    )
    sent = _reply_row_wire_content(claimed)
    client = _client()

    await send_message_outcome(
        client,
        _ROOM,
        sent,
        operation="send_message" if row == "create" else "edit_message",
        transaction_id="tx",
        content_is_prepared=True,
    )

    wire = _on_the_wire(client)
    assert wire == sent
    for layer in (wire, wire.get("m.new_content", wire)):
        assert layer[DURABLE_DELIVERY_ID_KEY]["delivery_id"] == "$turn"
        assert layer["body"].endswith("🔧 `shell` [1] ⏳")
        assert layer[_TRACE_KEY] == {"version": 2, "events": built[_TRACE_KEY]["events"]}
    # The same reply without the row's identity is a progress write, which says it is thinking.
    held = tool_markers_out_of_text(direct)
    progress = held.get("m.new_content", held)
    assert (progress["body"], progress[_TRACE_KEY]["placeholder_body"]) == ("Thinking...", True)
    assert tool_markers_in_text(held) == direct


@pytest.mark.asyncio
async def test_a_stream_that_opens_with_a_call_creates_its_reply_as_it_was_built(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """The create of a stream's reply is an outbox row even while the reply is pending, so it says nothing else."""
    outbox = journal_store.principal("agent@alice")
    room_send = AsyncMock(return_value=nio.RoomSendResponse("$reply", _ROOM))
    gateway = _reply_gateway(tmp_path, outbox, room_send)
    built = {
        **_marked("🔧 `shell` [1] ⏳", [{**_SHELL, "type": "tool_call_started"}]),
        STREAM_STATUS_KEY: STREAM_STATUS_PENDING,
    }

    async with reply_span(outbox, source_event_id="$cause", room_id=_ROOM) as handle:
        await gateway._deliver_reply_write(
            initial_write(handle, Presentation(), placeholder_only=False),
            target=MessageTarget.resolve(_ROOM, None, "$cause"),
            content=built,
            new_text=None,
            result=None,
        )

    row = await outbox.load_matrix_delivery(delivery_id="$cause", stage=DeliveryStage.INITIAL)
    assert row is not None
    assert row.acknowledged_event_id == "$reply"
    (wire,) = _sent_events(room_send)
    assert wire == row.payload == {**built, DURABLE_DELIVERY_ID_KEY: row.payload[DURABLE_DELIVERY_ID_KEY]}


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("stream_status", [None, STREAM_STATUS_COMPLETED])
@pytest.mark.parametrize("body", ["🔧 `shell` [1]\n\n🔧 `shell` [2]", "🔧 `shell` [1]\n\n \n\n🔧 `shell` [2]\n\n"])
async def test_a_terminal_reply_of_calls_alone_is_sent_with_its_markers_in_its_text(
    body: str,
    stream_status: str | None,
    edit: bool,
) -> None:
    """Terminal tool-calls-only replies stay v2 so rollback to an older backend never breaks exact outbox recovery."""
    built = _content(body, "<p>🔧 <code>shell</code> [1]</p>\n<p>🔧 <code>shell</code> [2]</p>\n", 2, [_SHELL, _SHELL])
    if stream_status is not None:
        built[STREAM_STATUS_KEY] = stream_status
    content = build_edit_event_content(event_id="$reply", new_content=built, new_text=body) if edit else built
    client = _client()

    await send_message_result(client, _ROOM, content)

    wire = _on_the_wire(client)
    assert wire is content
    assert message_judgment_text(_message(wire.get("m.new_content", wire))) is not None


@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("stream_status", [{"state": "streaming"}, ["streaming"], 1, True])
def test_a_reply_of_calls_alone_whose_stream_status_is_no_text_is_sent_with_its_markers(
    stream_status: object,
    edit: bool,
) -> None:
    """A status that is no string names no stream, whether or not a set of statuses could hold it."""
    built = {**_marked("🔧 `shell` [1]", [_SHELL]), STREAM_STATUS_KEY: stream_status}
    content = build_edit_event_content(event_id="$reply", new_content=built, new_text=built["body"]) if edit else built

    assert tool_markers_out_of_text(content) is content


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_a_non_canonical_streaming_reply_of_calls_alone_stays_version_2(edit: bool) -> None:
    """Extra blank lines or leftover HTML keep v2 even while streaming so byte-exact round-trip is never relaxed."""
    body = "🔧 `shell` [1]\n\n \n\n🔧 `shell` [2]\n\n"
    built = {
        **_content(body, "<p>🔧 <code>shell</code> [1]</p>\n<p>🔧 <code>shell</code> [2]</p>\n", 2, [_SHELL, _SHELL]),
        STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
    }
    content = build_edit_event_content(event_id="$reply", new_content=built, new_text=body) if edit else built
    client = _client()

    await send_message_result(client, _ROOM, content)

    wire = _on_the_wire(client)
    assert wire is content
    assert message_judgment_text(_message(wire.get("m.new_content", wire))) is not None


@pytest.mark.asyncio
async def test_an_edit_with_prose_after_an_edit_of_calls_alone_is_read_as_it_was_built() -> None:
    """Streaming calls-alone edits use placeholder_body v3; terminal calls-alone stay v2; prose uses normal v3."""
    streaming_calls = {**_marked("🔧 `shell` [1]", [_SHELL]), STREAM_STATUS_KEY: STREAM_STATUS_STREAMING}
    terminal_calls = _marked("🔧 `shell` [1]", [_SHELL])
    prose = _marked("🔧 `shell` [1]\n\nFound it.", [_SHELL])
    edits = [
        build_edit_event_content(event_id="$reply", new_content=built, new_text=built["body"])
        for built in (streaming_calls, terminal_calls, prose)
    ]

    held = [tool_markers_out_of_text(edit) for edit in edits]

    assert held[0]["body"] == "* Thinking..."
    assert held[0]["m.new_content"]["body"] == "Thinking..."
    assert held[0]["m.new_content"][_TRACE_KEY]["version"] == 3
    assert held[0]["m.new_content"][_TRACE_KEY]["placeholder_body"] is True
    assert held[1] is edits[1]
    assert (held[2]["body"], held[2]["m.new_content"][_TRACE_KEY]["version"]) == ("* Found it.", 3)
    for built, wire in zip((streaming_calls, terminal_calls, prose), held, strict=True):
        assert await extract_edit_body(_event(wire, "$edit")) == (built["body"], built)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [None],
        [None, _SHELL],
        ["shell"],
        [_SHELL, 7],
        [{"tool_name": "shell"}],
        [_SHELL, {"type": "tool_call_completed"}],
    ],
)
async def test_a_reply_whose_trace_has_an_entry_that_is_no_call_is_sent_as_it_is(events: list[Any]) -> None:
    """Marker [n] names entry n, so a list with a broken entry is sent on untouched, never filtered and renumbered.

    A reader takes such a trace for none, so its marker lines are text to it, which must stay in the text.
    """
    index = events.index(_SHELL) + 1 if _SHELL in events else 1
    content = _marked(f"Before.\n\n🔧 `shell` [{index}]\n\nAfter.", events)
    client = _client()

    assert await send_message_result(client, _ROOM, content) is not None

    assert _on_the_wire(client) is content
    assert tool_markers_in_text(content) is content


def test_content_that_names_no_markers_to_take_out_is_sent_as_it_is() -> None:
    """Plain messages, content already converted, a trace without HTML and a warm-up frame stay the same object.

    So does a reply whose text would keep a marker line: readers count on text without marker lines having none.
    """
    sent = _sent(_case("calls between two paragraphs share one position"))
    plain = {"msgtype": "m.text", "body": "🔧 `shell` [1]"}
    no_html = {"msgtype": "m.text", "body": "🔧 `shell` [1]", _TRACE_KEY: {"version": 2, "events": [_SHELL]}}
    warming = {**sent, STREAM_VISIBLE_BODY_KEY: sent["body"]}
    stray = _marked("🔧 `shell` [1]\n\n🔧 `shell` [2]\n\nDone.", [_SHELL])
    for content in (plain, tool_markers_out_of_text(sent), no_html, warming, stray):
        assert tool_markers_out_of_text(content) is content
    assert tool_markers_in_text(sent) is sent


def _large(calls: int, prose: int) -> dict[str, Any]:
    body = "\n\n".join([*(f"🔧 `shell` [{index}]" for index in range(1, calls + 1)), "x" * prose])
    return _marked(body, [_SHELL] * calls)


_USABLE_SIDECAR = ("mxc://localhost/full", {"size": 100, "mimetype": "application/json"})
_FAILED_SIDECAR = (None, None)


def _assert_no_raw_markers_on_wire(wire: dict[str, Any]) -> None:
    """Assert no raw tool-marker glyph leaks into any wire preview layer."""
    layers = [wire]
    if isinstance(wire.get("m.new_content"), dict):
        layers.append(wire["m.new_content"])
    for layer in layers:
        assert "🔧" not in str(layer.get("body", ""))
        assert "🔧" not in str(layer.get("formatted_body", ""))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upload_result", "expect_sidecar"),
    [(_USABLE_SIDECAR, True), (_FAILED_SIDECAR, False)],
    ids=["usable-sidecar", "upload-failure-fallback"],
)
async def test_a_message_too_large_for_one_event_strips_markers_from_its_preview_and_keeps_v2_sidecar(
    upload_result: tuple[str | None, dict[str, Any] | None],
    expect_sidecar: bool,
) -> None:
    """Sidecar sends upload the marked v2 payload while stripping raw markers from wire previews and fallbacks."""
    content = _large(3, 80_000)
    client = _client()
    upload = AsyncMock(return_value=upload_result)

    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload):
        await send_message_result(client, _ROOM, content)

    preview = _on_the_wire(client)
    _assert_no_raw_markers_on_wire(preview)
    assert preview["body"].startswith("xxxx")
    assert _TRACE_KEY not in preview
    assert ("io.mindroom.long_text" in preview) is expect_sidecar
    assert upload.await_args.args[2] == content
    if expect_sidecar:
        uploaded_bytes = json.dumps(upload.await_args.args[2]).encode("utf-8")
        with patch("mindroom.matrix.message_content.download_mxc_bytes", new=AsyncMock(return_value=uploaded_bytes)):
            resolved = await resolve_event_source_content(_event(preview), client)
        assert resolved["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upload_result", "expect_sidecar"),
    [(_USABLE_SIDECAR, True), (_FAILED_SIDECAR, False)],
    ids=["usable-sidecar", "upload-failure-fallback"],
)
async def test_a_large_terminal_edit_strips_markers_from_every_preview_layer_and_keeps_v2_sidecar(
    upload_result: tuple[str | None, dict[str, Any] | None],
    expect_sidecar: bool,
) -> None:
    """Terminal sidecar edits strip raw markers from inner and outer fallback layers while uploading marked v2."""
    replacement = _large(3, 80_000)
    edit = build_edit_event_content(event_id="$reply", new_content=replacement, new_text=replacement["body"])
    client = _client()
    upload = AsyncMock(return_value=upload_result)

    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload):
        await edit_message_result(client, _ROOM, "$reply", replacement, replacement["body"])

    preview = _on_the_wire(client)
    _assert_no_raw_markers_on_wire(preview)
    assert preview["body"].startswith("* xxxx")
    assert preview["m.new_content"]["body"].startswith("xxxx")
    assert _TRACE_KEY not in preview
    assert _TRACE_KEY not in preview["m.new_content"]
    assert ("io.mindroom.long_text" in preview["m.new_content"]) is expect_sidecar
    assert upload.await_args.args[2] == edit
    if expect_sidecar:
        uploaded_bytes = json.dumps(upload.await_args.args[2]).encode("utf-8")
        with patch("mindroom.matrix.message_content.download_mxc_bytes", new=AsyncMock(return_value=uploaded_bytes)):
            resolved = await resolve_event_source_content(_event(preview, "$edit"), client)
        assert resolved["content"] == edit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upload_result", "expect_sidecar"),
    [(_USABLE_SIDECAR, True), (_FAILED_SIDECAR, False)],
    ids=["usable-sidecar", "upload-failure-fallback"],
)
async def test_a_large_nonterminal_streaming_edit_strips_markers_from_every_preview_layer(
    upload_result: tuple[str | None, dict[str, Any] | None],
    expect_sidecar: bool,
) -> None:
    """In-progress streaming sidecar edits keep the uploaded v2 payload and strip markers from all preview layers."""
    replacement = {**_large(3, 40_000), STREAM_STATUS_KEY: STREAM_STATUS_STREAMING}
    edit = build_edit_event_content(event_id="$reply", new_content=replacement, new_text=replacement["body"])
    client = _client()
    upload = AsyncMock(return_value=upload_result)

    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload):
        await edit_message_result(client, _ROOM, "$reply", replacement, replacement["body"])

    preview = _on_the_wire(client)
    _assert_no_raw_markers_on_wire(preview)
    assert preview["body"].startswith("* xxxx")
    assert preview["m.new_content"]["body"].startswith("xxxx")
    assert _TRACE_KEY not in preview
    assert _TRACE_KEY not in preview["m.new_content"]
    assert ("io.mindroom.long_text" in preview["m.new_content"]) is expect_sidecar
    assert upload.await_args.args[2] == edit
    if expect_sidecar:
        uploaded_bytes = json.dumps(upload.await_args.args[2]).encode("utf-8")
        with patch("mindroom.matrix.message_content.download_mxc_bytes", new=AsyncMock(return_value=uploaded_bytes)):
            resolved = await resolve_event_source_content(_event(preview, "$edit"), client)
        assert resolved["content"] == edit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upload_result", "expect_sidecar"),
    [(_USABLE_SIDECAR, True), (_FAILED_SIDECAR, False)],
    ids=["usable-sidecar", "upload-failure-fallback"],
)
async def test_an_oversized_streaming_edit_of_calls_alone_says_thinking_only_over_the_sidecar_that_holds_its_calls(
    upload_result: tuple[str | None, dict[str, Any] | None],
    expect_sidecar: bool,
) -> None:
    """The preview's placeholder stands for calls a reader finds in the sidecar, which holds the edit as it was built.

    When the upload fails nothing else on the wire names the calls, so the edit keeps their marker lines, which
    MindRoom Chat draws as rows of tool names, as it did before previews went out without markers.
    """
    calls = 150
    body = "\n\n".join(f"🔧 `shell` [{index}]" for index in range(1, calls + 1))
    trace = [{"type": "tool_call_completed", "tool_name": "shell", "args_preview": "x" * 200} for _ in range(calls)]
    replacement = {**_marked(body, trace), STREAM_STATUS_KEY: STREAM_STATUS_STREAMING}
    edit = build_edit_event_content(event_id="$reply", new_content=replacement, new_text=replacement["body"])
    assert calculate_event_size(edit) > EDIT_MESSAGE_SIZE_LIMIT
    client = _client()
    upload = AsyncMock(return_value=upload_result)

    with patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload):
        await edit_message_result(client, _ROOM, "$reply", replacement, replacement["body"])

    preview = _on_the_wire(client)
    assert upload.await_args.args[2] == edit
    assert _TRACE_KEY not in preview
    assert _TRACE_KEY not in preview["m.new_content"]
    assert ("io.mindroom.long_text" in preview["m.new_content"]) is expect_sidecar
    if not expect_sidecar:
        assert (preview["body"], preview["m.new_content"]["body"]) == (f"* {body}", body)
        assert preview["m.new_content"]["formatted_body"] == replacement["formatted_body"]
        return
    _assert_no_raw_markers_on_wire(preview)
    assert preview["body"] == "* Thinking..."
    assert preview["m.new_content"]["body"] == "Thinking..."
    assert preview["m.new_content"]["formatted_body"] == "<p>Thinking...</p>\n"
    uploaded_bytes = json.dumps(upload.await_args.args[2]).encode("utf-8")
    with patch("mindroom.matrix.message_content.download_mxc_bytes", new=AsyncMock(return_value=uploaded_bytes)):
        resolved = await resolve_event_source_content(_event(preview, "$edit"), client)
    assert resolved["content"] == edit


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
    upload = AsyncMock(return_value=_USABLE_SIDECAR)

    with (
        patch("mindroom.matrix.large_messages.EDIT_MESSAGE_SIZE_LIMIT", (marked + sent) // 2),
        patch("mindroom.matrix.large_messages.upload_json_sidecar", new=upload),
    ):
        await edit_message_result(client, _ROOM, "$reply", replacement, replacement["body"])

    preview = _on_the_wire(client)
    assert upload.await_args.args[2] == edit
    assert _TRACE_KEY not in preview["m.new_content"]
    assert "io.mindroom.long_text" in preview["m.new_content"]
    _assert_no_raw_markers_on_wire(preview)


# --- The read boundary: where the backend first reads content from Matrix.


@pytest.mark.parametrize("case", _SENT_CASES, ids=[case["name"] for case in _SENT_CASES])
def test_reading_what_was_sent_gives_the_reply_back_exactly(case: dict[str, Any]) -> None:
    """Restored content is the built reply itself, so every reader reads what it always read."""
    sent, wire = _sent(case), _wire(case)

    assert tool_markers_in_text(wire) == sent
    assert tool_markers_in_text(sent) is sent
    assert _readers(_message(tool_markers_in_text(wire))) == _readers(_message(sent))


_INTERNAL_USER = "@mindroom_user:localhost"
# Whom a read trusts for the text of a warm-up frame.
_TRUSTED = [_AGENT, _INTERNAL_USER]


@pytest.fixture
def runtime() -> ToolRuntimeContext:
    """Return a runtime with one agent that has its Matrix account."""
    return _make_context()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _SENT_CASES, ids=[case["name"] for case in _SENT_CASES])
async def test_every_way_into_the_backend_reads_an_agents_reply_as_it_was_built(case: dict[str, Any]) -> None:
    """A synced event, a fetched one and one a person's account reads all carry the reply with its markers."""
    sent, wire = _sent(case), _wire(case)
    edit = build_edit_event_content(event_id="$reply", new_content=sent, new_text=sent["body"])
    # The edit as Matrix holds it: the fallback repeats the replacement, which a reply of calls alone no longer sends.
    its_layer = {key: wire[key] for key in ("formatted_body", _TRACE_KEY)}
    wire_edit = {**edit, "m.new_content": wire, "body": f"* {wire['body']}", **its_layer}
    assert tool_markers_out_of_text(edit) == (wire_edit if case.get("written", True) else edit)

    views = ingestion_timeline_views(
        room_id=_ROOM,
        source=_event(wire),
        self_sender="@mindroom_other:localhost",
        provenance=nio.TimelineEventProvenance.LIVE,
    )
    assert views is not None
    inbound, projected = views
    assert inbound.source["content"] == sent
    assert projected is not None
    assert projected.content == sent

    fetched = await resolve_event_source_content(_event(wire))
    assert fetched["content"] == sent
    message = await extract_and_resolve_message(nio.RoomMessage.parse_event(_event(wire)))
    assert (message["body"], message["content"]) == (sent["body"], sent)
    edited = await extract_edit_body(_event(wire_edit, "$edit"))
    assert edited == (sent["body"], sent)

    def read_by_a_person(reply: dict[str, Any], its_edit: dict[str, Any]) -> list[list[str]]:
        events = [_event(reply), _event(its_edit, "$edit")]
        return [_message_lines(events[:1]), _message_lines(events)]

    assert read_by_a_person(wire, wire_edit) == read_by_a_person(sent, edit)


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [_PERSON, "@helper:localhost", [_AGENT], {"id": _AGENT}, None])
async def test_no_way_into_the_backend_puts_markers_in_a_message_that_is_no_agents(sender: object) -> None:
    """Anyone can write positions into a trace, and only an account named as an agent had marker lines to put back.

    A homeserver may serve a sender that is no ID.
    """
    wire = _wire(_case(_BETWEEN))
    wire_edit = {**wire, "body": f"* {wire['body']}", "m.new_content": wire}
    event, edit = _event(wire, sender=sender), _event(wire_edit, "$edit", sender)
    assert tool_markers_in_text(wire)["body"] != wire["body"]

    assert await resolve_event_source_content(event) is event
    assert await extract_edit_body(edit, None, trusted_sender_ids=_TRUSTED) == (wire["body"], wire)
    assert all("🔧" not in line for line in _message_lines([event, edit]))
    if not isinstance(sender, str):
        return

    views = ingestion_timeline_views(
        room_id=_ROOM,
        source=event,
        self_sender=_AGENT,
        provenance=nio.TimelineEventProvenance.LIVE,
        sender_is_managed=lambda _sender: True,
    )
    assert views is not None
    inbound, projected = views
    assert inbound.source["content"] == wire
    assert projected is not None
    assert projected.content == wire

    message = await extract_and_resolve_message(
        nio.RoomMessage.parse_event(event),
        trusted_sender_ids=_TRUSTED,
    )
    assert (message["body"], message["content"]) == (wire["body"], wire)


# Accounts named as MindRoom names its agents that this runtime does not run: another server's, and one of its own
# that has left its config. Their replies reached Matrix without markers too.
_ELSEWHERE, _FORMER = "@mindroom_code:elsewhere.example", "@mindroom_retired:localhost"


@pytest.mark.asyncio
async def test_a_read_puts_markers_back_for_just_the_accounts_a_send_takes_them_out_for(tmp_path: Path) -> None:
    """One rule both ways, by username alone: whom the runtime runs or trusts changes neither.

    An agent with a username of its own sends its reply as it was built, so positions in its trace are its own writing.
    """
    paths = test_runtime_paths(tmp_path)
    agents = {"general": AgentConfig(display_name="General"), "helper": AgentConfig(display_name="Helper")}
    config = bind_runtime_paths(Config(agents=agents, mindroom_user=MindRoomUserConfig()), paths)
    persist_entity_accounts(config, paths, usernames={"helper": "helper"})
    registry = entity_identity_registry(config, paths)
    agent, own_name = (registry.current_ids[name].full_id for name in agents)
    assert own_name == "@helper:localhost"
    assert _INTERNAL_USER in current_internal_sender_ids(config, paths)
    known = {"config": config, "runtime_paths": paths}
    case = _case("a separator after a call and extra blank lines stay")
    sent, wire = _sent(case), _wire(case)
    edit = build_edit_event_content(event_id="$reply", new_content=sent, new_text=sent["body"])
    wire_edit = tool_markers_out_of_text(edit)
    assert wire_edit["m.new_content"] == wire

    async def sends_without_markers(sender: str) -> bool:
        client = _client()
        client.user_id = sender
        await send_message_result(client, _ROOM, sent)
        return _on_the_wire(client) == wire

    async def read(sender: str) -> list[object]:
        event, its_edit = _event(wire, sender=sender), _event(wire_edit, "$edit", sender)
        edited = {**_event({"msgtype": "m.text", "body": "Before."}, sender=sender)}
        edited["unsigned"] = {"m.relations": {"m.replace": {"latest_event": its_edit}}}
        views = ingestion_timeline_views(
            room_id=_ROOM,
            source=event,
            self_sender="@mindroom_other:localhost",
            provenance=nio.TimelineEventProvenance.LIVE,
            sender_is_managed=registry.is_managed_user_id,
        )
        assert views is not None
        source, _body = await resolve_visible_event_source(event, None, fallback_body="", **known)
        _edit_body, edit_content = await extract_visible_edit_body(its_edit, None, **known)
        return [
            views[0].source["content"],
            (await resolve_event_source_content(event))["content"],
            source["content"],
            edit_content,
            await bundled_replacement_body(edited, client=_client(), **known),
            await thread_root_body_preview(nio.RoomMessage.parse_event(event), client=_client(), **known),
            _message_lines([event, its_edit]),
        ]

    built = [sent, sent, sent, sent, "After.", "After.", _message_lines([_event(sent), _event(edit, "$edit")])]
    for sender in (agent, _INTERNAL_USER, _ELSEWHERE, _FORMER):
        assert await sends_without_markers(sender)
        assert await read(sender) == [*built[:6], [line.replace(_AGENT, sender) for line in built[6]]]
    for sender in (own_name, _PERSON):
        assert not await sends_without_markers(sender)
        assert all(content == wire for content in (await read(sender))[:4])


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [_ELSEWHERE, _FORMER])
async def test_a_reply_of_calls_alone_by_an_agent_this_runtime_does_not_run_leaves_the_judge_its_history(
    runtime: ToolRuntimeContext,
    sender: str,
) -> None:
    """A message queued in a later turn is judged, as it is where every reply has its markers in its text.

    A reply without text is history the judge cannot read, and then nothing queued is judged.
    """
    config, paths = runtime.config, runtime.runtime_paths
    registry = entity_identity_registry(config, paths)
    agent = registry.current_ids["general"].full_id
    case = _case("a reply of calls alone")
    built, held = _sent(case), _wire(case)
    streaming_held = _wire(_case("a streaming reply of calls alone"))
    assert held["body"] == ""
    assert streaming_held["body"] == "Thinking..."

    def synced(event_id: str, ts: int, sender: str, content: dict[str, Any]) -> VisibleMessage:
        """Return the row of one event as this agent's sync stores it."""
        thread = {"m.relates_to": {"rel_type": "m.thread", "event_id": "$root"}} if event_id != "$root" else {}
        views = ingestion_timeline_views(
            room_id=_ROOM,
            source={**_event({**content, **thread}, event_id, sender), "origin_server_ts": ts},
            self_sender=agent,
            provenance=nio.TimelineEventProvenance.LIVE,
            sender_is_managed=registry.is_managed_user_id,
        )
        assert views is not None
        projected = views[1]
        assert projected is not None
        return VisibleMessage(event_id, _ROOM, "$root", sender, ts, event_id, ts, projected.content)

    async def judged(reply: dict[str, Any]) -> tuple[MidTurnOutcome, int]:
        """Return what a message queued in the turn after ``reply`` comes to, and how often the judge was asked."""
        rows = (
            synced("$root", 1, _PERSON, {"msgtype": "m.text", "body": "Count the walruses."}),
            synced("$reply", 2, sender, reply),
            synced("$next", 3, _PERSON, {"msgtype": "m.text", "body": "Now count the seals."}),
        )
        history = projected_thread_history(ConversationPage(rows, (), None), complete=True)
        context = conversation_context_before_sources(
            history,
            source_event_ids=("$next",),
            thread_id="$root",
            config=config,
            runtime_paths=paths,
        )
        requests = []

        async def can_wait(request: object) -> bool:
            requests.append(request)
            return True

        gate = MidTurnGate("Now count the seals.", can_wait, requester_id=_PERSON, conversation_context=context)
        queued = QueuedMessage("$queued", "And the otters after that.", requester_id=_PERSON)
        return await gate.decide((queued,), current=lambda: True), len(requests)

    assert await judged(built) == (MidTurnOutcome.WAIT, 1)
    assert await judged(held) == (MidTurnOutcome.WAIT, 1)
    assert await judged(streaming_held) == (MidTurnOutcome.WAIT, 1)


@pytest.mark.asyncio
async def test_a_reply_stored_as_matrix_holds_it_is_read_with_its_markers_in_its_text(
    runtime: ToolRuntimeContext,
    journal_store: EventJournalStore,
) -> None:
    """A receiver on the release before this one stored an upgraded agent's reply as Matrix holds it.

    After its own upgrade it reads that row as it reads every other. A reply of calls alone would otherwise be text
    no judge can read, and one such row drops all the conversation before the turn.
    """
    config, paths = runtime.config, runtime.runtime_paths
    held = _wire(_case("a reply of calls alone"))
    placeholder_held = {
        **held,
        "body": "2 tool calls",
        "formatted_body": "<p>2 tool calls</p>\n",
        _TRACE_KEY: {**held[_TRACE_KEY], "placeholder_body": True},
    }
    assert held["body"] == ""
    assert placeholder_held["body"] == "2 tool calls"

    def synced(event_id: str, ts: int, sender: str, content: dict[str, Any]) -> ProjectedEvent:
        """Return the row this release's sync stores for one event of the thread."""
        views = ingestion_timeline_views(
            room_id=_ROOM,
            source={**_event(content, event_id, sender), "origin_server_ts": ts},
            self_sender="@mindroom_other:localhost",
            provenance=nio.TimelineEventProvenance.LIVE,
        )
        assert views is not None
        assert views[1] is not None
        return replace(views[1], thread_id="$root")

    async def judged(principal: str, reply: ProjectedEvent) -> tuple[JudgmentMessage, ...] | None:
        """Return the conversation a judge reads before the turn that answers the message after ``reply``."""
        store = journal_store.principal(principal)
        events = (
            synced("$root", 1, _PERSON, {"msgtype": "m.text", "body": "Count the walruses."}),
            reply,
            synced("$next", 3, _PERSON, {"msgtype": "m.text", "body": "Now count the seals."}),
        )
        assert await store.install_hydrated_conversation(
            room_id=_ROOM,
            thread_id="$root",
            events=events,
            complete=True,
            expected_membership_epoch=await store.membership_epoch(_ROOM),
        )
        page = await store.read_conversation(room_id=_ROOM, thread_id="$root", limit=10)
        return conversation_context_before_sources(
            projected_thread_history(page, complete=True),
            source_event_ids=("$next",),
            thread_id="$root",
            config=config,
            runtime_paths=paths,
        )

    by_this_release = synced("$reply", 2, _AGENT, held)
    assert by_this_release.content["body"] != ""
    read = await judged("this release", by_this_release)
    assert read is not None
    assert "shell" in read[1].text
    assert await judged("the release before", replace(by_this_release, content=held)) == read
    assert await judged("placeholder v3", replace(by_this_release, content=placeholder_held)) == read


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

    An agent may have any username, and its calls would be gone for everyone who reads its reply there.
    A client that names no account is in doubt, and in doubt the reply goes out as it was built.
    """
    case = _case(_BETWEEN)
    sent = _sent(case)
    client = _client()
    client.user_id = user_id

    await send_message_result(client, _ROOM, sent)

    wire = _on_the_wire(client)
    assert (wire == _wire(case)) is markers_leave
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


@pytest.mark.parametrize("place", [True, 1.0, -1, 9, None, "1"])
@pytest.mark.parametrize("keys", [("body_line", "at"), ("body_line",), ("at",)])
def test_a_position_that_is_no_place_in_the_text_leaves_the_content_as_it_is(place: object, keys: tuple[str]) -> None:
    """The backend restores only what its write boundary wrote; MindRoom Chat alone guesses a place for the rest."""
    content = _wire(_case("calls between two paragraphs share one position"))
    content[_TRACE_KEY]["events"][-1].update(dict.fromkeys(keys, place))
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


def test_placeholder_body_reader_restores_non_streaming_placeholders_and_legacy_empty_v3() -> None:
    """The reader supports placeholder_body=True for any stream_status as well as legacy empty-body v3."""
    case = _case("a reply of calls alone")
    sent = _sent(case)
    legacy_wire = _wire(case)
    assert legacy_wire["body"] == ""
    assert "placeholder_body" not in legacy_wire[_TRACE_KEY]
    assert tool_markers_in_text(legacy_wire) == sent

    non_streaming_placeholder = {
        **legacy_wire,
        "body": "2 tool calls",
        "formatted_body": "<p>2 tool calls</p>\n",
        _TRACE_KEY: {**legacy_wire[_TRACE_KEY], "placeholder_body": True},
    }
    restored = tool_markers_in_text(non_streaming_placeholder)
    assert restored == sent
    assert "placeholder_body" not in restored[_TRACE_KEY]

    edit = build_edit_event_content(event_id="$reply", new_content=sent, new_text=sent["body"])
    placeholder_edit = {
        **edit,
        "body": "* 2 tool calls",
        "formatted_body": "<p>2 tool calls</p>\n",
        _TRACE_KEY: non_streaming_placeholder[_TRACE_KEY],
        "m.new_content": non_streaming_placeholder,
    }
    assert tool_markers_in_text(placeholder_edit) == edit


@pytest.mark.parametrize("edit", [False, True])
@pytest.mark.parametrize("kept", _KEPT_PLACEHOLDERS, ids=[kept["name"] for kept in _KEPT_PLACEHOLDERS])
def test_a_placeholder_trace_that_is_not_sound_is_read_as_it_is(kept: dict[str, Any], edit: bool) -> None:
    """The shared fixture pins the traces whose placeholder text this reader and MindRoom Chat both keep."""
    held = _content("Thinking...", "<p>Thinking...</p>\n", 3, kept["events"])
    held[_TRACE_KEY]["placeholder_body"] = True
    content = build_edit_event_content(event_id="$reply", new_content=held, new_text=held["body"]) if edit else held

    assert tool_markers_in_text(content) is content


@pytest.mark.parametrize("fallback_trace", [True, False])
@pytest.mark.parametrize("kept", _KEPT_FALLBACKS, ids=[kept["name"] for kept in _KEPT_FALLBACKS])
def test_a_fallback_that_is_not_its_placeholder_replacement_again_is_read_as_it_is(
    kept: dict[str, Any],
    fallback_trace: bool,
) -> None:
    """The shared fixture pins the fallbacks this reader and MindRoom Chat both keep beside the restored replacement."""
    case = _case("a streaming reply of calls alone")
    held = _wire(case)
    fallback = {key: value for key, value in held.items() if fallback_trace or key != _TRACE_KEY}
    edit = {
        **fallback,
        "body": f"* {held['body']}",
        **kept["fallback"],
        "m.new_content": held,
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$reply"},
    }

    restored = tool_markers_in_text(edit)
    assert restored == {**edit, "m.new_content": _sent(case)}
    if _TRACE_KEY in edit:
        assert restored[_TRACE_KEY] == edit[_TRACE_KEY]


def test_a_placeholder_position_of_zero_is_read_as_chat_reads_it_whether_json_wrote_it_as_a_float_or_not() -> None:
    """MindRoom Chat cannot tell a JSON ``0.0`` from ``0``, so neither counts as another position here."""
    case = _case("a streaming reply of calls alone")
    held = _wire(case)
    held[_TRACE_KEY]["events"][0].update({"body_line": 0.0, "at": -0.0})

    assert tool_markers_in_text(held) == _sent(case)


def test_a_placeholder_body_that_is_not_true_is_read_as_the_positions_of_the_text_it_has() -> None:
    """Only ``true`` says the text is a placeholder, and only then does the key leave the trace."""
    case = _case("calls between two paragraphs share one position")
    held = _wire(case)
    held[_TRACE_KEY]["placeholder_body"] = 1
    sent = _sent(case)
    sent[_TRACE_KEY]["placeholder_body"] = 1

    assert tool_markers_in_text(held) == sent
