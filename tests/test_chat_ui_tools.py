"""Behavioral tests for agent-requested MindRoom Chat UI actions."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import nio
import pytest

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
    from pathlib import Path


def test_chat_ui_tool_registered_and_exposes_only_bounded_arguments(tmp_path: Path) -> None:
    """The opt-in toolkit must not let the caller choose URLs, credentials, or identities."""
    context = _context(tmp_path)
    metadata = TOOL_METADATA["chat_ui"]

    assert metadata.requires_room_context
    assert metadata.function_names == ("show_computer", "open_settings", "open_panel")
    assert isinstance(get_tool_by_name("chat_ui", context.runtime_paths, worker_target=None), ChatUITools)
    assert tuple(inspect.signature(ChatUITools.show_computer).parameters) == ("self",)
    assert tuple(inspect.signature(ChatUITools.open_settings).parameters) == ("self", "section")
    assert tuple(inspect.signature(ChatUITools.open_panel).parameters) == ("self", "panel")


@pytest.mark.asyncio
async def test_show_computer_sends_exact_wire_metadata_from_canonical_context(tmp_path: Path) -> None:
    """Changing any runtime-derived identity or canonical thread field must break this contract."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_computer())

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
async def test_ui_notice_survives_live_ingress_and_history_projection(tmp_path: Path) -> None:
    """The normal notice path must retain action metadata without creating agent work."""
    context = _context(tmp_path)

    with tool_runtime_context(context):
        await ChatUITools().show_computer()

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
async def test_room_level_request_uses_null_thread_without_relation(tmp_path: Path) -> None:
    """Room-level requests must not invent a thread root or Matrix relation."""
    context = _context(tmp_path, thread_id=None, reply_to_event_id=None)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().open_panel())

    content = _sent_content(context)
    assert content["io.mindroom.ui_action"] == {
        "version": 1,
        "action": "open_panel",
        "requester_id": REQUESTER_ID,
        "agent_user_id": context.client.user_id,
        "room_id": ROOM_ID,
        "thread_id": None,
        "panel": "members",
    }
    assert "m.relates_to" not in content
    assert result["thread_id"] is None


@pytest.mark.asyncio
async def test_thread_continuation_uses_latest_projected_event_for_fallback(tmp_path: Path) -> None:
    """A non-reply thread action must fall back to the latest event, not blindly to the root."""
    context = _context(tmp_path, reply_to_event_id=None)
    context.conversation_reader.latest_thread_event_id.return_value = "$latest"

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_computer())

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
async def test_missing_thread_fallback_is_rejected_without_sending(tmp_path: Path) -> None:
    """A threaded action must not invent a fallback target when projection cannot resolve one."""
    context = _context(tmp_path, reply_to_event_id=None)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_computer())

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
async def test_missing_runtime_context_is_rejected() -> None:
    """Detached calls must not accept caller-supplied routing as a substitute for runtime authority."""
    result = json.loads(await ChatUITools().show_computer())

    assert result["status"] == "error"
    assert "runtime context" in result["message"]


@pytest.mark.asyncio
async def test_malformed_runtime_requester_is_rejected_without_sending(tmp_path: Path) -> None:
    """An invalid addressed-user identity must never be copied into trusted wire metadata."""
    context = replace(_context(tmp_path), requester_id="not-a-matrix-user")

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_computer())

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
) -> None:
    """Malformed runtime routing must fail before the Matrix delivery boundary."""
    context = replace(_context(tmp_path), target=target)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_computer())

    assert result["status"] == "error"
    assert message_fragment in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_transport_agent_mismatch_is_rejected_without_sending(tmp_path: Path) -> None:
    """A delegated agent must not point a UI request at its transport agent's worker."""
    context = _context(tmp_path, transport_agent_name="router")

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_computer())

    assert result["status"] == "error"
    assert "transport identity" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_sender_mismatch_is_rejected_without_sending(tmp_path: Path) -> None:
    """Metadata agent identity must equal both the configured agent and actual Matrix sender."""
    context = _context(tmp_path)
    context.client.user_id = "@mindroom_other:example.org"

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_computer())

    assert result["status"] == "error"
    assert "Matrix sender" in result["message"]
    context.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_team_context_is_rejected_without_sending(tmp_path: Path) -> None:
    """A team has no single agent worker identity for the client to select."""
    context = _context(tmp_path, agent_name="research", include_team=True)

    with tool_runtime_context(context):
        result = json.loads(await ChatUITools().show_computer())

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
