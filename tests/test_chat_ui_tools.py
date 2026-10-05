"""Behavioral tests for agent-requested MindRoom Chat UI actions."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from nio.api import RelationshipType
from pydantic import ValidationError

import mindroom.tools  # noqa: F401
from mindroom.custom_tools.chat_ui import ChatUITools
from mindroom.event_journal import EventClass, EventKind
from mindroom.matrix.client_visible_messages import extract_visible_message, is_visible_room_message
from mindroom.matrix.journal_ingress import ingestion_timeline_views
from mindroom.matrix.room_history_reads import parse_room_message_event
from mindroom.message_target import MessageTarget
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.chat_ui_contract_fixture import (
    REQUESTER_ID,
    ROOM_ID,
    THREAD_ID,
)
from tests.chat_ui_contract_fixture import (
    make_chat_ui_context as _context,
)
from tests.chat_ui_contract_fixture import (
    sent_chat_ui_content as _sent_content,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


@pytest.fixture(params=["show_computer", "open_panel"])
def computer_request(request: pytest.FixtureRequest) -> Callable[[], Awaitable[str]]:
    """Exercise both Computer entry points against the same transport and safety contract."""
    tool = ChatUITools()
    if request.param == "show_computer":
        return tool.show_computer
    return partial(tool.open_panel, panel="computer")


def test_chat_ui_tool_registered_and_exposes_only_bounded_arguments(tmp_path: Path) -> None:
    """The opt-in toolkit must not let the caller choose URLs, credentials, or identities."""
    context = _context(tmp_path)
    metadata = TOOL_METADATA["chat_ui"]

    assert metadata.requires_room_context
    assert metadata.function_names == (
        "show_computer",
        "open_settings",
        "open_panel",
        "show_canvas",
        "read_canvas_state",
    )
    assert [(field.name, field.default) for field in metadata.config_fields] == [
        ("enable_show_canvas", False),
        ("enable_canvas_libraries", False),
    ]
    assert sorted(ChatUITools().async_functions) == ["open_panel", "open_settings", "show_computer"]
    assert {"show_canvas", "read_canvas_state"} <= set(ChatUITools(enable_show_canvas=True).async_functions)
    assert isinstance(get_tool_by_name("chat_ui", context.runtime_paths, worker_target=None), ChatUITools)
    assert tuple(inspect.signature(ChatUITools.show_computer).parameters) == ("self",)
    assert tuple(inspect.signature(ChatUITools.open_settings).parameters) == ("self", "section")
    assert tuple(inspect.signature(ChatUITools.open_panel).parameters) == ("self", "panel")
    assert tuple(inspect.signature(ChatUITools.show_canvas).parameters) == (
        "self",
        "title",
        "html",
        "path",
        "canvas_event_id",
        "share_state",
    )
    assert tuple(inspect.signature(ChatUITools.read_canvas_state).parameters) == ("self", "canvas_event_id")
    assert inspect.signature(ChatUITools.open_panel).parameters["panel"].default == "members"
    function = ChatUITools().get_async_functions()["open_panel"]
    function.process_entrypoint(strict=True)
    assert function.parameters["properties"]["panel"]["enum"] == ["members", "computer"]
    assert function.parameters["additionalProperties"] is False


@pytest.mark.asyncio
async def test_computer_request_sends_exact_wire_metadata_from_canonical_context(
    tmp_path: Path,
    computer_request: Callable[[], Awaitable[str]],
) -> None:
    """Changing any runtime-derived identity or canonical thread field must break this contract."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(await computer_request())

    assert result["status"] == "ok"
    content = _sent_content(context)
    expected = {
        "version": 1,
        "action": "show_computer",
        "requester_id": REQUESTER_ID,
        "agent_user_id": context.client.user_id,
        "room_id": ROOM_ID,
        "thread_id": THREAD_ID,
    }
    assert content["io.mindroom.ui_action"] == expected
    assert content["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": THREAD_ID,
        "is_falling_back": False,
        "m.in_reply_to": {"event_id": "$request"},
    }
    assert content["msgtype"] == "m.notice"
    assert content["body"] == "Open this agent's worker computer in MindRoom Chat."
    context.conversation_reader.latest_thread_event_id.assert_not_awaited()
    assert result == {
        "action": "show_computer",
        "event_id": "$ui-action",
        "message": "UI action request sent.",
        "room_id": ROOM_ID,
        "status": "ok",
        "thread_id": THREAD_ID,
        "tool": "chat_ui",
    }


@pytest.mark.asyncio
async def test_ui_notice_survives_live_ingress_and_history_projection(
    tmp_path: Path,
    computer_request: Callable[[], Awaitable[str]],
) -> None:
    """The normal notice path must retain action metadata without creating agent work."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        await computer_request()

    content = _sent_content(context)
    source = {
        "event_id": "$ui-action",
        "sender": context.client.user_id,
        "origin_server_ts": 1_000,
        "room_id": ROOM_ID,
        "type": "m.room.message",
        "content": content,
    }
    views = ingestion_timeline_views(
        room_id=ROOM_ID,
        source=source,
        self_sender=context.client.user_id,
        provenance=nio.TimelineEventProvenance.LIVE,
    )

    assert views is not None
    inbound, projected = views
    assert inbound.kind is EventKind.MESSAGE
    assert inbound.event_class is EventClass.CONTEXT_ONLY
    assert projected is not None
    assert projected.content["io.mindroom.ui_action"] == content["io.mindroom.ui_action"]

    parsed = parse_room_message_event(source)
    assert is_visible_room_message(parsed)
    history_message = await extract_visible_message(
        parsed,
        config=context.config,
        runtime_paths=context.runtime_paths,
        trusted_sender_ids={context.client.user_id},
    )
    assert history_message["content"]["io.mindroom.ui_action"] == content["io.mindroom.ui_action"]
    assert history_message["content"]["m.relates_to"] == content["m.relates_to"]


@pytest.mark.asyncio
async def test_canvas_notice_and_its_edit_create_no_agent_work(tmp_path: Path) -> None:
    """A canvas and its in-place update are context for the agent, never a new turn."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context))
    context.client.room_send.side_effect = [
        nio.RoomSendResponse("$canvas-new", ROOM_ID),
        nio.RoomSendResponse("$canvas-edit", ROOM_ID),
    ]

    with tool_runtime_context(context):
        await ChatUITools().show_canvas(title="Plans", html="<p>1</p>")
    await _update(context)

    sent = [call.kwargs["content"] for call in context.client.room_send.await_args_list]
    assert "m.new_content" in sent[1]
    for event_id, content in zip(("$canvas-new", "$canvas-edit"), sent, strict=True):
        views = ingestion_timeline_views(
            room_id=ROOM_ID,
            source={
                "event_id": event_id,
                "sender": context.client.user_id,
                "origin_server_ts": 1_000,
                "room_id": ROOM_ID,
                "type": "m.room.message",
                "content": content,
            },
            self_sender=context.client.user_id,
            provenance=nio.TimelineEventProvenance.LIVE,
        )
        assert views is not None
        assert views[0].event_class is EventClass.CONTEXT_ONLY


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", ["members", "computer"])
async def test_room_level_request_uses_null_thread_without_relation(tmp_path: Path, panel: str) -> None:
    """Room-level requests must not invent a thread root or Matrix relation."""
    context = _context(tmp_path, thread_id=None, reply_to_event_id=None)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().open_panel(panel=panel))  # type: ignore[arg-type]

    assert result["status"] == "ok"
    content = _sent_content(context)
    expected = {
        "version": 1,
        "action": "open_panel" if panel == "members" else "show_computer",
        "requester_id": REQUESTER_ID,
        "agent_user_id": context.client.user_id,
        "room_id": ROOM_ID,
        "thread_id": None,
    }
    if panel == "members":
        expected["panel"] = "members"
    assert content["io.mindroom.ui_action"] == expected
    assert result["message"] == "UI action request sent."
    assert "m.relates_to" not in content
    assert result["thread_id"] is None


@pytest.mark.asyncio
async def test_thread_continuation_uses_latest_projected_event_for_fallback(
    tmp_path: Path,
    computer_request: Callable[[], Awaitable[str]],
) -> None:
    """A non-reply thread action must fall back to the latest event, not blindly to the root."""
    context = _context(tmp_path, reply_to_event_id=None)
    context.conversation_reader.latest_thread_event_id.return_value = "$latest"

    with tool_runtime_context(context):
        result = json.loads(await computer_request())

    assert result["status"] == "ok"
    assert _sent_content(context)["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": THREAD_ID,
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$latest"},
    }
    context.conversation_reader.latest_thread_event_id.assert_awaited_once_with(
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
    )


@pytest.mark.asyncio
async def test_missing_thread_fallback_is_rejected_without_sending(
    tmp_path: Path,
    computer_request: Callable[[], Awaitable[str]],
) -> None:
    """A threaded action must not invent a fallback target when projection cannot resolve one."""
    context = _context(tmp_path, reply_to_event_id=None)

    with tool_runtime_context(context):
        result = json.loads(await computer_request())

    assert result == {
        "action": "show_computer",
        "message": "Failed to resolve Matrix thread fallback for UI action request.",
        "room_id": ROOM_ID,
        "status": "error",
        "thread_id": THREAD_ID,
        "tool": "chat_ui",
    }
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section",
    ["general", "account", "notifications", "devices", "emojis-stickers", "developer", "about"],
)
async def test_open_settings_emits_each_supported_section(tmp_path: Path, section: str) -> None:
    """Every documented settings destination must survive into the wire payload unchanged."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().open_settings(section=section))  # type: ignore[arg-type]

    content = _sent_content(context)
    assert content["io.mindroom.ui_action"] == {
        "version": 1,
        "action": "open_settings",
        "requester_id": REQUESTER_ID,
        "agent_user_id": context.client.user_id,
        "room_id": ROOM_ID,
        "thread_id": THREAD_ID,
        "section": section,
    }
    assert result["status"] == "ok"
    assert result["event_id"] == "$ui-action"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "argument", "message_fragment"),
    [
        ("open_settings", "security", "settings section"),
        ("open_panel", "files", "side panel"),
        ("open_panel", "browser", "side panel"),
        ("open_panel", "settings", "side panel"),
    ],
)
async def test_invalid_action_argument_is_rejected_without_sending(
    tmp_path: Path,
    method_name: str,
    argument: str,
    message_fragment: str,
) -> None:
    """Unsupported UI targets must fail before they can become client instructions."""
    context = _context(tmp_path)
    method = getattr(ChatUITools(), method_name)

    with tool_runtime_context(context):
        result = json.loads(await method(argument))

    assert result["status"] == "error"
    assert message_fragment in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_runtime_context_is_rejected(computer_request: Callable[[], Awaitable[str]]) -> None:
    """Detached calls must not accept caller-supplied routing as a substitute for runtime authority."""
    result = json.loads(await computer_request())

    assert result["status"] == "error"
    assert "runtime context" in result["message"]


@pytest.mark.asyncio
async def test_malformed_runtime_requester_is_rejected_without_sending(
    tmp_path: Path,
    computer_request: Callable[[], Awaitable[str]],
) -> None:
    """An invalid addressed-user identity must never be copied into trusted wire metadata."""
    context = replace(_context(tmp_path), requester_id="not-a-matrix-user")

    with tool_runtime_context(context):
        result = json.loads(await computer_request())

    assert result["status"] == "error"
    assert "requester" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "message_fragment"),
    [
        (
            MessageTarget(
                room_id="room-without-matrix-sigil",
                source_thread_id=THREAD_ID,
                resolved_thread_id=THREAD_ID,
                reply_to_event_id="$request",
                session_id="invalid-room-context",
            ),
            "room context",
        ),
        (
            MessageTarget(
                room_id=ROOM_ID,
                source_thread_id="thread-without-matrix-sigil",
                resolved_thread_id="thread-without-matrix-sigil",
                reply_to_event_id="$request",
                session_id="invalid-thread-context",
            ),
            "thread context",
        ),
        (
            MessageTarget(
                room_id=ROOM_ID,
                source_thread_id=THREAD_ID,
                resolved_thread_id=THREAD_ID,
                reply_to_event_id="reply-without-matrix-sigil",
                session_id="invalid-reply-context",
            ),
            "reply context",
        ),
    ],
)
async def test_malformed_matrix_target_is_rejected_without_sending(
    tmp_path: Path,
    target: MessageTarget,
    message_fragment: str,
    computer_request: Callable[[], Awaitable[str]],
) -> None:
    """Malformed runtime routing must fail before the Matrix delivery boundary."""
    context = replace(_context(tmp_path), target=target)

    with tool_runtime_context(context):
        result = json.loads(await computer_request())

    assert result["status"] == "error"
    assert message_fragment in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_agent_mismatch_is_rejected_without_sending(
    tmp_path: Path,
    computer_request: Callable[[], Awaitable[str]],
) -> None:
    """A delegated agent must not point a UI request at its transport agent's worker."""
    context = _context(tmp_path, transport_agent_name="router")

    with tool_runtime_context(context):
        result = json.loads(await computer_request())

    assert result["status"] == "error"
    assert "transport identity" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_sender_mismatch_is_rejected_without_sending(
    tmp_path: Path,
    computer_request: Callable[[], Awaitable[str]],
) -> None:
    """Metadata agent identity must equal both the configured agent and actual Matrix sender."""
    context = _context(tmp_path)
    context.client.user_id = "@mindroom_other:example.org"

    with tool_runtime_context(context):
        result = json.loads(await computer_request())

    assert result["status"] == "error"
    assert "Matrix sender" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_context_is_rejected_without_sending(
    tmp_path: Path,
    computer_request: Callable[[], Awaitable[str]],
) -> None:
    """A team has no single agent worker identity for the client to select."""
    context = _context(tmp_path, agent_name="research", include_team=True)

    with tool_runtime_context(context):
        result = json.loads(await computer_request())

    assert result["status"] == "error"
    assert "configured agent" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_failure_returns_error_without_claiming_the_ui_opened(tmp_path: Path) -> None:
    """A failed Matrix send must not be reported as a sent or opened UI request."""
    context = _context(tmp_path)
    context.client.room_send.return_value = object()

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().open_settings())

    assert result == {
        "action": "open_settings",
        "message": "Failed to send the UI action request.",
        "room_id": ROOM_ID,
        "status": "error",
        "thread_id": THREAD_ID,
        "tool": "chat_ui",
    }
    assert "opened" not in json.dumps(result).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("panel", ["members", "computer", "browser"])
async def test_panel_rejects_unexpected_url_without_sending(tmp_path: Path, panel: str) -> None:
    """Panel requests cannot smuggle navigation or endpoints through extra arguments."""
    context = _context(tmp_path)
    function = ChatUITools().get_async_functions()["open_panel"]
    function.process_entrypoint(strict=False)
    assert function.entrypoint is not None
    with tool_runtime_context(context), pytest.raises((TypeError, ValidationError), match="url"):
        await function.entrypoint(panel=panel, url="https://example.org")
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_members_request_keeps_thread_transport_and_truthful_result(tmp_path: Path) -> None:
    """The default Members action must retain its existing wire shape and canonical thread."""
    context = _context(tmp_path)
    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().open_panel())
    assert _sent_content(context)["io.mindroom.ui_action"] == {
        "version": 1,
        "action": "open_panel",
        "panel": "members",
        "requester_id": REQUESTER_ID,
        "agent_user_id": context.client.user_id,
        "room_id": ROOM_ID,
        "thread_id": THREAD_ID,
    }
    assert result["status"] == "ok"
    assert result["message"] == "UI action request sent."


CANVAS_HTML = "<button onclick=\"mindroom.submit({plan: 'pro'}, {label: 'Pro'})\">Pro</button>"
CANVAS_BODY = "Interactive panel: Plans. Open it in MindRoom Chat to respond."


def _canvas_source(
    context: ToolRuntimeContext,
    *,
    content: dict[str, object] | None = None,
    **metadata: object,
) -> dict[str, object]:
    return {
        "type": "m.room.message",
        "event_id": "$canvas",
        "sender": context.client.user_id,
        "content": {
            "msgtype": "m.notice",
            "body": CANVAS_BODY,
            "m.relates_to": {"rel_type": "m.thread", "event_id": THREAD_ID},
            "io.mindroom.ui_action": {
                "version": 1,
                "action": "show_canvas",
                "requester_id": REQUESTER_ID,
                "agent_user_id": context.client.user_id,
                "room_id": ROOM_ID,
                "thread_id": THREAD_ID,
                "canvas": {"title": "Plans", "html": "<p>1</p>"},
                **metadata,
            },
            **(content or {}),
        },
    }


def _serve_event(
    context: ToolRuntimeContext,
    source: dict[str, object],
    *,
    sender: str | None = None,
    undecryptable: bool = False,
) -> None:
    source = {**source, "sender": sender or source["sender"], "origin_server_ts": 1_000}
    if undecryptable:
        event = MagicMock(spec=nio.MegolmEvent)
        event.source = source
    else:
        # nio parses the source as it would from the server (a redacted source becomes a RedactedEvent).
        event = nio.Event.parse_event(source)
    response = nio.RoomGetEventResponse()
    response.event = event
    context.client.room_get_event = AsyncMock(return_value=response)


async def _update(context: ToolRuntimeContext, *, html: str = "<p>2</p>") -> dict[str, object]:
    with tool_runtime_context(context):
        return json.loads(await ChatUITools().show_canvas(title="Plans", html=html, canvas_event_id="$canvas"))


@pytest.mark.asyncio
async def test_canvas_request_carries_title_and_html_in_the_ui_action(tmp_path: Path) -> None:
    """Chat renders exactly what the agent wrote; identities still come from the runtime."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="  Plans  ", html=CANVAS_HTML))

    content = _sent_content(context)
    assert content["msgtype"] == "m.notice"
    assert content["body"] == CANVAS_BODY
    assert content["formatted_body"] == CANVAS_BODY
    assert content["io.mindroom.ui_action"] == {
        "version": 1,
        "action": "show_canvas",
        "requester_id": REQUESTER_ID,
        "agent_user_id": context.client.user_id,
        "room_id": ROOM_ID,
        "thread_id": THREAD_ID,
        "canvas": {"title": "Plans", "html": CANVAS_HTML},
    }
    assert content["m.relates_to"]["rel_type"] == "m.thread"
    assert content["m.relates_to"]["event_id"] == THREAD_ID
    assert result["status"] == "ok"
    assert result["event_id"] == "$ui-action"


@pytest.mark.asyncio
async def test_canvas_fallback_shows_the_title_as_text(tmp_path: Path) -> None:
    """Markdown or HTML in a title is shown literally by other clients."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        await ChatUITools().show_canvas(title="*Pick* <b>one</b>", html="<p>1</p>")

    content = _sent_content(context)
    assert content["formatted_body"] == (
        "Interactive panel: *Pick* &lt;b&gt;one&lt;/b&gt;. Open it in MindRoom Chat to respond."
    )


@pytest.mark.asyncio
async def test_empty_canvas_event_id_shows_a_new_canvas(tmp_path: Path) -> None:
    """Models often send an empty string for an omitted optional argument."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Plans", html="<p>1</p>", canvas_event_id=""))

    assert result["status"] == "ok"
    assert "m.new_content" not in _sent_content(context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "html", "canvas_event_id", "message"),
    [
        ("", "<p></p>", None, "one line"),
        ("Two\nlines", "<p></p>", None, "one line"),
        ("Carriage\rreturn", "<p></p>", None, "one line"),
        ("Line\u2028separator", "<p></p>", None, "one line"),
        ("x" * 121, "<p></p>", None, "one line"),
        ("\U0001f600" * 61, "<p></p>", None, "one line"),
        ("Bell\x07", "<p></p>", None, "control characters"),
        ("Lone \ud800 surrogate", "<p></p>", None, "valid text"),
        ("Plans", "   ", None, "must not be empty"),
        ("Plans", "<p></p>", "canvas" * 6000, "earlier show_canvas call"),
    ],
)
async def test_invalid_canvas_is_rejected_without_sending(
    tmp_path: Path,
    title: str,
    html: str,
    canvas_event_id: str | None,
    message: str,
) -> None:
    """The agent gets an actionable error instead of an empty or malformed panel."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(
            await ChatUITools().show_canvas(title=title, html=html, canvas_event_id=canvas_event_id),
        )

    assert result["status"] == "error"
    assert message in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["\U0001f469\u200d\U0001f4bb Coding", "Q3\u00a0report", "Ship\u00adping"])
async def test_titles_with_joined_emoji_and_typographic_spaces_are_accepted(tmp_path: Path, title: str) -> None:
    """Zero-width joiners, no-break spaces, and soft hyphens are ordinary in titles."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title=title, html="<p>1</p>"))

    assert result["status"] == "ok"
    assert _sent_content(context)["io.mindroom.ui_action"]["canvas"]["title"] == title


@pytest.mark.asyncio
async def test_title_limit_counts_utf16_units_like_chat(tmp_path: Path) -> None:
    """Sixty emoji are 120 UTF-16 units, the longest title both sides accept."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="\U0001f600" * 60, html="<p>1</p>"))

    assert result["status"] == "ok"


def _serve_upload(context: ToolRuntimeContext, mxc_uri: str = "mxc://example.org/canvas") -> None:
    context.client.upload = AsyncMock(return_value=(nio.UploadResponse.from_dict({"content_uri": mxc_uri}), None))


@pytest.mark.asyncio
@pytest.mark.parametrize("html", ["x" * 26_000, "\U0001f600" * 2_500, '"' * 13_000])
async def test_large_canvas_is_uploaded_and_referenced(tmp_path: Path, html: str) -> None:
    """A page whose edit would not fit travels as media; the event keeps only a reference."""
    context = _context(tmp_path)
    _serve_upload(context)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Big", html=html))

    assert result["status"] == "ok"
    upload = context.client.upload.await_args.kwargs
    assert upload["content_type"] == "text/html"
    assert upload["filesize"] == len(html.encode("utf-8"))
    assert upload["data_provider"](None, None).read() == html.encode("utf-8")
    canvas = _sent_content(context)["io.mindroom.ui_action"]["canvas"]
    assert canvas == {
        "title": "Big",
        "document": {"mimetype": "text/html", "size": len(html.encode("utf-8")), "url": "mxc://example.org/canvas"},
    }


@pytest.mark.asyncio
async def test_canvas_over_the_page_limit_is_rejected(tmp_path: Path) -> None:
    """Pages above 4 MB are refused before anything is uploaded or sent."""
    context = _context(tmp_path)
    context.client.upload = AsyncMock()

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Huge", html="x" * (4 * 1024 * 1024 + 1)))

    assert result["status"] == "error"
    assert "limit" in result["message"]
    context.client.upload.assert_not_awaited()
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_canvas_upload_is_reported(tmp_path: Path) -> None:
    """A page that could not be uploaded is not reported as shown."""
    context = _context(tmp_path)
    context.client.upload = AsyncMock(return_value=(nio.UploadError("no space"), None))

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Big", html="x" * 26_000))

    assert result == {
        "action": "show_canvas",
        "message": "Failed to upload the canvas page.",
        "status": "error",
        "tool": "chat_ui",
    }
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_canvas_just_under_the_edit_ceiling_stays_inline(tmp_path: Path) -> None:
    """A page that fits as a later edit is carried inside the event."""
    context = _context(tmp_path)
    context.client.upload = AsyncMock()

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Big", html="x" * 23_000))

    assert result["status"] == "ok"
    assert _sent_content(context)["io.mindroom.ui_action"]["canvas"]["html"] == "x" * 23_000
    context.client.upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_canvas_from_a_workspace_file(tmp_path: Path) -> None:
    """Agents can show a page they keep in their workspace, such as slides they are building."""
    workspace = tmp_path / "workspace"
    (workspace / "slides").mkdir(parents=True)
    (workspace / "slides" / "deck.html").write_text("<section>Slide 1</section>", encoding="utf-8")
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(
            await ChatUITools(tool_output_workspace_root=workspace).show_canvas(title="Deck", path="slides/deck.html"),
        )

    assert result["status"] == "ok"
    assert _sent_content(context)["io.mindroom.ui_action"]["canvas"] == {
        "title": "Deck",
        "html": "<section>Slide 1</section>",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setup", "path", "message"),
    [
        (None, "missing.html", "must be an existing file"),
        (None, "../outside.html", "Canvas path"),
        ("binary", "deck.html", "UTF-8"),
        ("empty", "deck.html", "empty"),
    ],
)
async def test_canvas_path_is_confined_to_readable_workspace_text(
    tmp_path: Path,
    setup: str | None,
    path: str,
    message: str,
) -> None:
    """Paths follow the agent's file access and must name a non-empty UTF-8 file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.html").write_text("<p>secret</p>", encoding="utf-8")
    if setup == "binary":
        (workspace / "deck.html").write_bytes(b"\xff\xfe\x00")
    if setup == "empty":
        (workspace / "deck.html").write_text("  ", encoding="utf-8")
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(
            await ChatUITools(tool_output_workspace_root=workspace).show_canvas(title="Deck", path=path),
        )

    assert result["status"] == "error"
    assert message in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("html", "path"), [(None, None), ("<p>1</p>", "deck.html"), ("", "")])
async def test_canvas_needs_exactly_one_page_source(tmp_path: Path, html: str | None, path: str | None) -> None:
    """Either inline HTML or a workspace path, never both or neither."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Deck", html=html, path=path))

    assert result["message"] == "Give exactly one of html or path."
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_canvas_update_edits_the_agents_own_canvas_in_place(tmp_path: Path) -> None:
    """A multi-step flow keeps one timeline card; only the replacement carries the new page."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context))
    context.client.room_send.return_value = nio.RoomSendResponse("$edit", ROOM_ID)

    result = await _update(context)

    content = _sent_content(context)
    assert content == {
        "msgtype": "m.notice",
        "body": f"* {CANVAS_BODY}",
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$canvas"},
        "m.new_content": {
            "msgtype": "m.notice",
            "body": CANVAS_BODY,
            "format": "org.matrix.custom.html",
            "formatted_body": CANVAS_BODY,
            "io.mindroom.ui_action": {
                "version": 1,
                "action": "show_canvas",
                "requester_id": REQUESTER_ID,
                "agent_user_id": context.client.user_id,
                "room_id": ROOM_ID,
                "thread_id": THREAD_ID,
                "canvas": {"title": "Plans", "html": "<p>2</p>"},
            },
        },
    }
    assert result == {
        "action": "show_canvas",
        "event_id": "$canvas",
        "message": "Canvas update sent.",
        "revision_event_id": "$edit",
        "room_id": ROOM_ID,
        "status": "ok",
        "thread_id": THREAD_ID,
        "tool": "chat_ui",
    }


@pytest.mark.asyncio
async def test_canvas_update_without_a_title_keeps_the_first_title(tmp_path: Path) -> None:
    """An update may omit the title; a new canvas may not."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context))
    context.client.room_send.return_value = nio.RoomSendResponse("$edit", ROOM_ID)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(html="<p>2</p>", canvas_event_id="$canvas"))
        missing = json.loads(await ChatUITools().show_canvas(html="<p>2</p>"))

    assert result["status"] == "ok"
    replacement = _sent_content(context)["m.new_content"]
    assert replacement["body"] == CANVAS_BODY
    assert replacement["io.mindroom.ui_action"]["canvas"] == {"title": "Plans", "html": "<p>2</p>"}
    assert missing["status"] == "error"
    assert "Canvas title must be one line" in missing["message"]


@pytest.mark.asyncio
async def test_canvas_update_without_a_title_needs_a_valid_first_title(tmp_path: Path) -> None:
    """A canvas whose first title is unusable needs a title on every update."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context, canvas={"title": "", "html": "<p>1</p>"}))
    context.client.room_send.return_value = nio.RoomSendResponse("$edit", ROOM_ID)

    with tool_runtime_context(context):
        missing = json.loads(await ChatUITools().show_canvas(html="<p>2</p>", canvas_event_id="$canvas"))
        given = json.loads(await ChatUITools().show_canvas(title="Seats", html="<p>2</p>", canvas_event_id="$canvas"))

    assert missing["status"] == "error"
    assert "Canvas title must be one line" in missing["message"]
    assert given["status"] == "ok"
    assert _sent_content(context)["m.new_content"]["io.mindroom.ui_action"]["canvas"]["title"] == "Seats"


@pytest.mark.asyncio
async def test_room_level_canvas_can_be_updated_from_the_thread_its_answer_started(tmp_path: Path) -> None:
    """The user's reply to a room-level canvas starts a thread; the edit keeps the canvas room-level."""
    context = _context(tmp_path)
    source = _canvas_source(context, thread_id=None)
    del source["content"]["m.relates_to"]
    _serve_event(context, source)

    result = await _update(context)

    assert result["status"] == "ok"
    assert _sent_content(context)["m.new_content"]["io.mindroom.ui_action"]["thread_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "content", "sender", "message"),
    [
        ({}, {}, "@mindroom_other:example.org", "Only your own canvases"),
        ({"action": "show_computer"}, {}, None, "Only your own canvases"),
        ({"version": 2}, {}, None, "Only your own canvases"),
        ({"agent_user_id": "@mindroom_other:example.org"}, {}, None, "Only your own canvases"),
        ({}, {"msgtype": "m.text"}, None, "Only your own canvases"),
        ({}, {"m.relates_to": {"rel_type": "m.replace", "event_id": "$older"}}, None, "is a revision of a canvas"),
        ({"requester_id": "@bob:example.org"}, {}, None, "another conversation"),
        ({"room_id": "!other:example.org"}, {}, None, "another conversation"),
        ({"thread_id": "$other"}, {}, None, "another conversation"),
    ],
)
async def test_canvas_update_rejects_targets_outside_this_agents_conversation(
    tmp_path: Path,
    metadata: dict[str, object],
    content: dict[str, object],
    sender: str | None,
    message: str,
) -> None:
    """An agent cannot rewrite another agent's, another person's, or another thread's panel."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context, content=content, **metadata), sender=sender)

    result = await _update(context)

    assert result["status"] == "error"
    assert message in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_canvas_update_rejects_unreadable_undecryptable_or_deleted_targets(tmp_path: Path) -> None:
    """Missing, encrypted-but-undecryptable, and redacted canvases are not updated."""
    context = _context(tmp_path)
    context.client.room_get_event = AsyncMock(return_value=nio.RoomGetEventError(message="not found"))
    missing = await _update(context)
    _serve_event(context, _canvas_source(context), undecryptable=True)
    undecryptable = await _update(context)
    source = _canvas_source(context)
    source["unsigned"] = {"redacted_because": {"event_id": "$redaction"}}
    _serve_event(context, source)
    deleted = await _update(context)

    assert "could not be read" in missing["message"]
    assert "could not be read" in undecryptable["message"]
    assert "was deleted" in deleted["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_aimed_at_a_revision_names_the_canvas_id(tmp_path: Path) -> None:
    """Answers name both IDs; passing the revision tells the agent which ID to use instead."""
    context = _context(tmp_path)
    revision = {
        "type": "m.room.message",
        "event_id": "$canvas",
        "sender": context.client.user_id,
        "content": {
            "msgtype": "m.notice",
            "body": f"* {CANVAS_BODY}",
            "m.new_content": _canvas_source(context)["content"],
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        },
    }
    _serve_event(context, revision)

    result = await _update(context)

    assert result["status"] == "error"
    assert "canvas_event_id='$original'" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_over_limit_update_is_rejected_before_reading_the_target(tmp_path: Path) -> None:
    """A page that cannot be sent is refused without a Matrix round trip."""
    context = _context(tmp_path)
    context.client.room_get_event = AsyncMock()

    result = await _update(context, html="x" * (4 * 1024 * 1024 + 1))

    assert "limit" in result["message"]
    context.client.room_get_event.assert_not_awaited()
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_update_references_its_uploaded_page_from_the_edit(tmp_path: Path) -> None:
    """A new version of a large page is uploaded again and the edit carries the new reference."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context))
    _serve_upload(context, "mxc://example.org/v2")
    context.client.room_send.return_value = nio.RoomSendResponse("$edit", ROOM_ID)

    result = await _update(context, html="y" * 26_000)

    assert result["status"] == "ok"
    canvas = _sent_content(context)["m.new_content"]["io.mindroom.ui_action"]["canvas"]
    assert canvas["document"]["url"] == "mxc://example.org/v2"
    assert "html" not in canvas


@pytest.mark.asyncio
async def test_failed_canvas_update_is_reported(tmp_path: Path) -> None:
    """A failed edit is not reported as an updated canvas."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context))
    context.client.room_send.return_value = object()

    result = await _update(context)

    assert result["status"] == "error"
    assert result["message"] == "Failed to send the canvas update."


@pytest.mark.asyncio
async def test_team_context_cannot_show_a_canvas(tmp_path: Path) -> None:
    """Canvases keep the same single-agent identity rule as other UI requests."""
    context = _context(tmp_path, agent_name="research", include_team=True)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Plans", html="<p>1</p>"))

    assert result["status"] == "error"
    assert "configured agent" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_canvas_in_an_encrypted_room_uploads_ciphertext(tmp_path: Path) -> None:
    """Encrypted rooms get an encrypted upload; the reference carries the key Chat needs to decrypt it."""
    context = _context(tmp_path)
    context.client.rooms[ROOM_ID].encrypted = True
    context.client.olm = MagicMock()
    _serve_upload(context, "mxc://example.org/enc")
    page = "x" * 26_000

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Big", html=page))

    assert result["status"] == "ok"
    upload = context.client.upload.await_args.kwargs
    assert upload["content_type"] == "application/octet-stream"
    assert upload["data_provider"](None, None).read() != page.encode("utf-8")
    document = _sent_content(context)["io.mindroom.ui_action"]["canvas"]["document"]
    assert document["mimetype"] == "text/html"
    assert document["size"] == 26_000
    assert "url" not in document
    assert document["file"]["url"] == "mxc://example.org/enc"
    assert document["file"]["v"] == "v2"
    assert document["file"]["key"]["k"]
    assert document["file"]["iv"]
    assert document["file"]["hashes"]["sha256"]


@pytest.mark.asyncio
async def test_large_canvas_is_not_uploaded_when_the_encrypted_room_refuses_sends(tmp_path: Path) -> None:
    """A send the room's trust policy would refuse leaves no orphan upload behind."""
    context = _context(tmp_path)
    context.client.rooms[ROOM_ID].encrypted = True
    context.client.olm = None
    _serve_upload(context)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Big", html="x" * 26_000))

    assert result["status"] == "error"
    assert "encrypted room" in result["message"]
    context.client.upload.assert_not_awaited()
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_canvas_rejects_html_that_is_not_unicode_text(tmp_path: Path) -> None:
    """A lone surrogate from a model cannot crash the tool."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_canvas(title="Bad", html="<p>\ud800</p>"))

    assert result["message"] == "Canvas HTML must be valid Unicode text."


@pytest.mark.asyncio
async def test_canvas_file_over_the_limit_names_the_limit(tmp_path: Path) -> None:
    """The agent learns the limit, not only that the file was too big."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "big.html").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(
            await ChatUITools(tool_output_workspace_root=workspace).show_canvas(title="Big", path="big.html"),
        )

    assert result["message"] == "Canvas file is larger than the 4194304-byte limit."


@pytest.mark.asyncio
async def test_slides_from_a_file_update_in_place_after_an_edit(tmp_path: Path) -> None:
    """The live-development flow: edit the file, show it again with the canvas ID."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "deck.html").write_text("<section>Version 2</section>", encoding="utf-8")
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context))
    context.client.room_send.return_value = nio.RoomSendResponse("$edit", ROOM_ID)

    with tool_runtime_context(context):
        result = json.loads(
            await ChatUITools(tool_output_workspace_root=workspace).show_canvas(
                title="Deck",
                path="deck.html",
                canvas_event_id="$canvas",
            ),
        )

    assert result["revision_event_id"] == "$edit"
    canvas = _sent_content(context)["m.new_content"]["io.mindroom.ui_action"]["canvas"]
    assert canvas == {"title": "Deck", "html": "<section>Version 2</section>"}


@pytest.mark.asyncio
async def test_large_update_of_a_foreign_canvas_uploads_nothing(tmp_path: Path) -> None:
    """The target is checked before a large page is uploaded."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context), sender="@mindroom_other:example.org")
    context.client.upload = AsyncMock()

    result = await _update(context, html="y" * 26_000)

    assert "Only your own canvases" in result["message"]
    context.client.upload.assert_not_awaited()
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_canvas_that_shares_its_state_says_so_from_the_start(tmp_path: Path) -> None:
    """Sharing is an authority field of the request, so Chat can tell the user before anything is shared."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        await ChatUITools().show_canvas(title="Plans", html=CANVAS_HTML, share_state=True)

    assert _sent_content(context)["io.mindroom.ui_action"]["share_state"] is True


@pytest.mark.asyncio
async def test_updates_keep_a_canvas_sharing_and_cannot_start_it(tmp_path: Path) -> None:
    """An edit must repeat the original's sharing, and cannot turn sharing on behind the user's back."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context, share_state=True))
    context.client.room_send.return_value = nio.RoomSendResponse("$edit", ROOM_ID)
    await _update(context)
    assert _sent_content(context)["m.new_content"]["io.mindroom.ui_action"]["share_state"] is True

    unshared = _context(tmp_path)
    _serve_event(unshared, _canvas_source(unshared))
    with tool_runtime_context(unshared):
        result = json.loads(
            await ChatUITools().show_canvas(html="<p>2</p>", canvas_event_id="$canvas", share_state=True),
        )
    assert result["status"] == "error"
    assert "decided when a canvas is first shown" in result["message"]
    unshared.client.room_send.assert_not_awaited()


def _state_copy(
    *,
    sender: str = REQUESTER_ID,
    event_type: str = "io.mindroom.canvas_state",
    ts: int = 2_000,
    **content: object,
) -> nio.Event:
    return nio.Event.parse_event(
        {
            "type": event_type,
            "event_id": f"$copy-{ts}",
            "sender": sender,
            "origin_server_ts": ts,
            "content": {
                "version": 1,
                "m.relates_to": {"rel_type": "m.reference", "event_id": "$canvas"},
                **content,
            },
        },
    )


def _serve_relations(context: ToolRuntimeContext, *events: object) -> MagicMock:
    async def newest_first() -> AsyncIterator[object]:
        for event in events:
            yield event

    relations = MagicMock(side_effect=lambda *_args, **_kwargs: newest_first())
    context.client.room_get_event_relations = relations
    return relations


async def _read(context: ToolRuntimeContext, canvas_event_id: str = "$canvas") -> dict[str, object]:
    with tool_runtime_context(context):
        return json.loads(await ChatUITools(enable_show_canvas=True).read_canvas_state(canvas_event_id))


@pytest.mark.asyncio
async def test_reading_a_canvas_state_returns_the_requesters_newest_copy(tmp_path: Path) -> None:
    """Copies from anyone else, and other references to the canvas, are skipped."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context, share_state=True))
    relations = _serve_relations(
        context,
        _state_copy(sender="@mallory:example.org", ts=4_000, json='{"done":["forged"]}'),
        _state_copy(event_type="m.room.message", ts=3_500, msgtype="m.text", body="a reply"),
        _state_copy(ts=3_000, json='{"done":["tent"]}', inputs='{"#rate":"7"}'),
        _state_copy(ts=2_000, json='{"done":[]}'),
    )

    result = await _read(context)

    assert result["status"] == "ok"
    assert result["state"] == {"done": ["tent"]}
    assert result["inputs"] == {"#rate": "7"}
    assert result["shared_at"] == "1970-01-01T00:00:03+00:00"
    assert relations.call_args.args[:3] == (ROOM_ID, "$canvas", RelationshipType.reference)


@pytest.mark.asyncio
async def test_reading_a_canvas_state_decrypts_copies_in_encrypted_rooms(tmp_path: Path) -> None:
    """The server sees only m.room.encrypted, so the type is checked after decrypting."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context, share_state=True))
    encrypted = MagicMock(spec=nio.MegolmEvent)
    encrypted.sender = REQUESTER_ID
    encrypted.server_timestamp = 5_000
    _serve_relations(context, encrypted)
    context.client.olm = MagicMock()
    context.client.decrypt_event = MagicMock(return_value=_state_copy(json='{"done":["map"]}'))

    result = await _read(context)

    assert result["state"] == {"done": ["map"]}
    context.client.decrypt_event.assert_called_once_with(encrypted)


@pytest.mark.asyncio
async def test_reading_a_large_canvas_state_follows_its_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State too large for one event arrives as a long-text sidecar."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context, share_state=True))
    _serve_relations(context, _state_copy(msgtype="m.file", url="mxc://example.org/state"))

    async def resolved(source: dict[str, object], _client: object) -> dict[str, object]:
        return {**source, "content": {"version": 1, "json": '{"notes":"long"}'}}

    monkeypatch.setattr("mindroom.custom_tools.chat_ui.resolve_event_source_content", resolved)

    assert (await _read(context))["state"] == {"notes": "long"}


@pytest.mark.asyncio
async def test_reading_a_canvas_state_reports_nothing_shared_and_unshared_canvases(tmp_path: Path) -> None:
    """The agent learns why there is no state, and only its own shared canvases can be read."""
    context = _context(tmp_path)
    _serve_event(context, _canvas_source(context, share_state=True))
    _serve_relations(context)
    nothing = await _read(context)
    assert nothing["status"] == "ok"
    assert "Nothing shared yet" in nothing["message"]

    unshared = _context(tmp_path)
    _serve_event(unshared, _canvas_source(unshared))
    result = await _read(unshared)
    assert result["status"] == "error"
    assert "does not share its state" in result["message"]

    foreign = _context(tmp_path)
    _serve_event(foreign, _canvas_source(foreign, share_state=True), sender="@mindroom_other:example.org")
    result = await _read(foreign)
    assert result["action"] == "read_canvas_state"
    assert "Only your own canvases can be read." in result["message"]
