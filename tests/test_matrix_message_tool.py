"""Tests for the native matrix_message tool."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call, patch

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.attachments import register_local_attachment
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    SKIP_MENTIONS_KEY,
    SOURCE_KIND_KEY,
    STREAM_VISIBLE_BODY_KEY,
)
from mindroom.custom_tools.attachments import AttachmentTools
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.interactive import parse_and_format_interactive
from mindroom.matrix.client_delivery import build_edit_event_content
from mindroom.matrix.client_visible_messages import trusted_visible_sender_ids
from mindroom.matrix.conversation_hydration import HYDRATED_PROMPT_WINDOW_MESSAGES
from mindroom.matrix.message_extras import MINDROOM_MESSAGE_EXTRAS_KEY
from mindroom.matrix.state import MatrixState, _load_matrix_state_file_cached
from mindroom.message_target import MessageTarget
from mindroom.session_ids import create_session_id
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_event,
    delivered_matrix_side_effect,
    make_conversation_reader_mock,
    make_latest_thread_event_id_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    make_visible_message,
    runtime_paths_for,
    serve_conversation_reader,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.matrix.client import DeliveredMatrixEvent


_DEFAULT_RESOLVED_THREAD_ID = object()


@pytest.fixture(autouse=True)
def _reset_matrix_message_rate_limit() -> None:
    MatrixMessageTools._recent_actions.clear()


def _empty_async_iterator() -> AsyncIterator[object]:
    async def iterator() -> AsyncIterator[object]:
        if False:
            yield None

    return iterator()


def _make_context(
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$thread:localhost",
    requester_id: str = "@user:localhost",
    bot_accounts: list[str] | None = None,
    mindroom_user: MindRoomUserConfig | None = None,
    resolved_thread_id: object = _DEFAULT_RESOLVED_THREAD_ID,
    reply_to_event_id: str | None = "$reply:localhost",
    storage_path: Path | None = None,
    attachment_ids: tuple[str, ...] = (),
    agent_thread_mode: str = "thread",
    membership: object | None = None,
    membership_turn_id: str | None = None,
) -> ToolRuntimeContext:
    runtime_root = storage_path or Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    thread_mode=agent_thread_mode,
                ),
            },
            bot_accounts=bot_accounts or [],
            mindroom_user=mindroom_user,
        ),
        test_runtime_paths(runtime_root),
    )
    client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    room = nio.MatrixRoom(room_id, client.user_id)
    room.add_member(client.user_id, "General Agent", None)
    room.members_synced = True
    client.rooms = {room_id: room}
    client.room_send = AsyncMock()
    client.room_messages = AsyncMock()
    client.room_get_event_relations = MagicMock(
        side_effect=lambda *_args, **_kwargs: _empty_async_iterator(),
    )
    conversation_reader = make_conversation_reader_mock()
    conversation_reader.latest_thread_event_id = make_latest_thread_event_id_mock()
    if membership is None:
        membership = MagicMock()
        membership.membership_epoch = AsyncMock(return_value=0)
        membership.interactive_prompt_is_current = AsyncMock(return_value=True)
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget(
            room_id=room_id,
            source_thread_id=thread_id,
            resolved_thread_id=thread_id if resolved_thread_id is _DEFAULT_RESOLVED_THREAD_ID else resolved_thread_id,
            reply_to_event_id=reply_to_event_id,
            session_id=create_session_id(
                room_id,
                thread_id if resolved_thread_id is _DEFAULT_RESOLVED_THREAD_ID else resolved_thread_id,
            ),
        ),
        requester_id=requester_id,
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=conversation_reader,
        room=None,
        storage_path=storage_path,
        attachment_ids=attachment_ids,
        membership=membership,
        membership_turn_id=membership_turn_id,
    )


def test_matrix_message_tool_registered_and_instantiates() -> None:
    """Matrix message tool should be available from metadata registry."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(Path(tempfile.mkdtemp())),
    )
    assert "matrix_message" in TOOL_METADATA
    assert isinstance(
        get_tool_by_name("matrix_message", runtime_paths_for(config), worker_target=None),
        MatrixMessageTools,
    )


@pytest.mark.asyncio
async def test_matrix_message_requires_runtime_context() -> None:
    """Tool should fail clearly when called without Matrix runtime context."""
    payload = json.loads(await MatrixMessageTools().matrix_message(action="send", message="hello"))
    assert payload["status"] == "error"
    assert payload["tool"] == "matrix_message"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_send_defaults_to_current_conversation() -> None:
    """Send should inherit the current conversation."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="send", message="hello"))

    assert payload["status"] == "ok"
    assert payload["action"] == "send"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    sent_content = mock_send.await_args.args[2]
    assert sent_content["body"] == "hello"
    assert sent_content["m.relates_to"]["event_id"] == "$ctx-thread:localhost"
    assert sent_content[SKIP_MENTIONS_KEY] is True
    assert ORIGINAL_SENDER_KEY not in sent_content
    assert SOURCE_KIND_KEY not in sent_content


@pytest.mark.asyncio
async def test_matrix_message_active_mentions_mark_trusted_human_relay() -> None:
    """Intentional mention dispatch should preserve a trusted human requester."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="@general continue work",
                recipient="general",
            ),
        )

    assert payload["status"] == "ok"
    sent_content = mock_send.await_args.args[2]
    assert sent_content["m.mentions"] == {"user_ids": [ctx.client.user_id]}
    assert SKIP_MENTIONS_KEY not in sent_content
    assert sent_content[ORIGINAL_SENDER_KEY] == ctx.requester_id
    assert sent_content[SOURCE_KIND_KEY] == TRUSTED_INTERNAL_RELAY_SOURCE_KIND


@pytest.mark.asyncio
async def test_matrix_message_active_mentions_do_not_promote_managed_requester() -> None:
    """Intentional mention dispatch should not classify managed requesters as humans."""
    tool = MatrixMessageTools()
    ctx = _make_context(
        thread_id=None,
        requester_id="@mindroom_router:localhost",
    )

    ctx.config.agents["code"] = AgentConfig(display_name="Code")
    entity_ids(ctx.config, ctx.runtime_paths)
    ctx.client.rooms[ctx.room_id].add_member("@mindroom_code:localhost", "Code", None)
    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="@general continue work",
                recipient="code",
            ),
        )

    assert payload["status"] == "ok"
    sent_content = mock_send.await_args.args[2]
    assert sent_content["m.mentions"] == {"user_ids": ["@mindroom_code:localhost"]}
    assert SKIP_MENTIONS_KEY not in sent_content
    assert ORIGINAL_SENDER_KEY not in sent_content
    assert SOURCE_KIND_KEY not in sent_content


@pytest.mark.parametrize(
    ("requester_id", "bot_accounts", "mindroom_user"),
    [
        ("@bridge_bot:localhost", ["@bridge_bot:localhost"], None),
        ("@mindroom_user:localhost", [], MindRoomUserConfig()),
    ],
)
@pytest.mark.asyncio
async def test_matrix_message_active_mentions_do_not_promote_non_human_requester(
    requester_id: str,
    bot_accounts: list[str],
    mindroom_user: MindRoomUserConfig | None,
) -> None:
    """Trusted relay provenance should require a human requester."""
    tool = MatrixMessageTools()
    ctx = _make_context(
        thread_id=None,
        requester_id=requester_id,
        bot_accounts=bot_accounts,
        mindroom_user=mindroom_user,
    )

    ctx.config.agents["code"] = AgentConfig(display_name="Code")
    entity_ids(ctx.config, ctx.runtime_paths)
    ctx.client.rooms[ctx.room_id].add_member("@mindroom_code:localhost", "Code", None)
    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="@general continue work",
                recipient="code",
            ),
        )

    assert payload["status"] == "ok"
    sent_content = mock_send.await_args.args[2]
    assert sent_content["m.mentions"] == {"user_ids": ["@mindroom_code:localhost"]}
    assert SKIP_MENTIONS_KEY not in sent_content
    assert ORIGINAL_SENDER_KEY not in sent_content
    assert SOURCE_KIND_KEY not in sent_content


@pytest.mark.asyncio
async def test_matrix_message_send_resolves_room_alias_before_send(tmp_path: Path) -> None:
    """Explicit room aliases should resolve to room IDs before authorization and delivery."""
    tool = MatrixMessageTools()
    ctx = _make_context(storage_path=tmp_path, thread_id=None)
    state = MatrixState()
    state.add_room("ops", room_id="!ops:localhost", alias="#ops:localhost", name="Ops")
    state.save(runtime_paths=ctx.runtime_paths)
    _load_matrix_state_file_cached.cache_clear()

    with (
        patch("mindroom.custom_tools.matrix_message.room_access_allowed", return_value=True) as mock_access,
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="send", message="hello", room_id="#ops:localhost"))

    mock_access.assert_called_once_with(ctx, "!ops:localhost")
    assert mock_send.await_args.args[1] == "!ops:localhost"
    assert payload["status"] == "ok"
    assert payload["room_id"] == "!ops:localhost"


@pytest.mark.asyncio
async def test_matrix_message_rejects_non_string_room_id_before_resolution(tmp_path: Path) -> None:
    """Explicit room IDs should return structured type errors before alias resolution."""
    tool = MatrixMessageTools()
    ctx = _make_context(storage_path=tmp_path, thread_id=None)

    with (
        patch("mindroom.custom_tools.attachment_helpers.resolve_optional_room_id") as mock_resolve,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(action="send", message="hello", room_id=123),  # type: ignore[arg-type]
        )

    mock_resolve.assert_not_called()
    assert payload["status"] == "error"
    assert payload["message"] == "room_id must be a non-empty string."


@pytest.mark.asyncio
async def test_matrix_message_send_includes_message_extras() -> None:
    """Send action should attach validated MindRoom message extras."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="Short answer.",
                message_extras=[
                    {
                        "title": "Evidence",
                        "content_type": "text/html",
                        "content": "<table><tr><td>42</td></tr></table>",
                        "collapsed": False,
                    },
                ],
            ),
        )

    assert payload["status"] == "ok"
    sent_content = mock_send.await_args.args[2]
    assert sent_content["body"] == "Short answer."
    assert sent_content[MINDROOM_MESSAGE_EXTRAS_KEY] == {
        "version": 2,
        "sections": [
            {
                "title": "Evidence",
                "content_type": "text/html",
                "content": "<table><tr><td>42</td></tr></table>",
                "collapsed": False,
            },
        ],
    }


@pytest.mark.asyncio
async def test_matrix_message_send_rejects_message_extras_without_text_event() -> None:
    """Extras should not be silently dropped on attachment-only sends."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                attachments=["att_context_file"],
                message_extras=[
                    {
                        "title": "Evidence",
                        "content": "details",
                    },
                ],
            ),
        )

    assert payload["status"] == "error"
    assert "non-empty message" in payload["message"]
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_rejects_invalid_message_extras() -> None:
    """Invalid extras should return a tool error instead of sending."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="Short answer.",
                message_extras=[
                    {
                        "title": "Raw",
                        "content_type": "application/json",
                        "content": "{}",
                    },
                ],
            ),
        )

    assert payload["status"] == "error"
    assert "content_type" in payload["message"]
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_send_room_sentinel_stays_room_level() -> None:
    """thread_id='room' should disable thread metadata for sends."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    ctx.conversation_reader.latest_thread_event_id = AsyncMock(return_value=None)

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(action="send", thread_id="room", message="hello"),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "send"
    assert payload["room_id"] == ctx.room_id
    assert payload["thread_id"] is None
    assert payload["event_id"] == "$evt"
    ctx.conversation_reader.latest_thread_event_id.assert_awaited_once_with(
        room_id=ctx.room_id,
        thread_id=None,
        known_latest_thread_event_id=None,
    )
    sent_content = mock_send.await_args.args[2]
    assert sent_content["body"] == "hello"
    assert "m.relates_to" not in sent_content


@pytest.mark.asyncio
async def test_matrix_message_send_rejects_interactive_prompts() -> None:
    """Direct tool sends have no durable identity for recoverable prompts."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    interactive_message = """Please choose.

```interactive
{
  "question": "Which option?",
  "options": [
    {"emoji": "✅", "label": "Approve", "value": "approve"},
    {"emoji": "❌", "label": "Reject", "value": "reject"}
  ]
}
```"""
    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="send", message=interactive_message))

    assert payload == {
        "status": "error",
        "tool": "matrix_message",
        "action": "send",
        "room_id": ctx.room_id,
        "message": "Interactive prompts are only supported in normal agent responses.",
    }
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_send_plain_text_skips_interactive_registration_and_reactions() -> None:
    """Plain-text sends should not register interactive state or add reactions."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.parse_and_format_interactive",
            wraps=parse_and_format_interactive,
        ) as mock_parse,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="send", message="hello"))

    assert payload["status"] == "ok"
    mock_parse.assert_called_once_with("hello", extract_mapping=True)


@pytest.mark.asyncio
async def test_matrix_message_send_supports_context_attachments(tmp_path: Path) -> None:
    """Send should accept context att_* IDs and upload them after text."""
    tool = MatrixMessageTools()
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_upload",
    )
    assert attachment is not None
    ctx = _make_context(storage_path=tmp_path, attachment_ids=("att_upload",))
    ctx.conversation_reader.latest_thread_event_id = AsyncMock(return_value="$evt")

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="hello",
                attachments=["att_upload"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$evt"
    assert payload["thread_id"] == ctx.resolved_thread_id
    assert payload["attachment_event_ids"] == ["$file_evt"]
    assert payload["resolved_attachment_ids"] == ["att_upload"]
    ctx.conversation_reader.latest_thread_event_id.assert_has_awaits(
        [
            call(room_id=ctx.room_id, thread_id=ctx.resolved_thread_id, known_latest_thread_event_id=None),
            # The attachment is told what the text send returned rather than
            # being left to read a projection that has not seen it yet.
            call(room_id=ctx.room_id, thread_id=ctx.resolved_thread_id, known_latest_thread_event_id="$evt"),
        ],
    )
    mock_send.assert_awaited_once()
    mock_send_file.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        attachment.local_path,
        thread_id=ctx.resolved_thread_id,
        latest_thread_event_id="$evt",
    )


@pytest.mark.asyncio
async def test_matrix_message_send_with_attachment_in_room_mode_stays_room_level(tmp_path: Path) -> None:
    """Room-mode sends should not auto-thread attachments under the new text event."""
    tool = MatrixMessageTools()
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_room_mode",
    )
    assert attachment is not None
    ctx = _make_context(
        storage_path=tmp_path,
        attachment_ids=("att_room_mode",),
        thread_id="$ctx-thread:localhost",
        resolved_thread_id=None,
        agent_thread_mode="room",
    )

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="hello",
                attachments=["att_room_mode"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$evt"
    assert payload["thread_id"] is None
    assert payload["attachment_event_ids"] == ["$file_evt"]
    mock_send.assert_awaited_once()
    mock_send_file.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        attachment.local_path,
        thread_id=None,
        latest_thread_event_id=None,
    )


@pytest.mark.asyncio
async def test_matrix_message_send_with_attachments_keeps_existing_thread(tmp_path: Path) -> None:
    """Send attachments should stay in the existing thread instead of using the text event as a new root."""
    tool = MatrixMessageTools()
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_reply",
    )
    assert attachment is not None
    ctx = _make_context(storage_path=tmp_path, attachment_ids=("att_reply",))

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$reply_evt")),
        ) as mock_send,
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="hello",
                attachments=["att_reply"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$reply_evt"
    assert payload["thread_id"] == ctx.thread_id
    assert payload["attachment_event_ids"] == ["$file_evt"]
    mock_send.assert_awaited_once()
    mock_send_file.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        attachment.local_path,
        thread_id=ctx.thread_id,
        # The reply text this same call just sent, not the thread root a
        # projection read would still be answering with until its echo lands.
        latest_thread_event_id="$reply_evt",
    )


@pytest.mark.asyncio
async def test_matrix_message_send_with_explicit_thread_and_attachments_keeps_existing_thread(
    tmp_path: Path,
) -> None:
    """Send attachments should stay in the explicit thread instead of auto-threading under the new text event."""
    tool = MatrixMessageTools()
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_explicit_thread",
    )
    assert attachment is not None
    ctx = _make_context(storage_path=tmp_path, attachment_ids=("att_explicit_thread",), thread_id=None)
    explicit_thread_id = "$explicit-thread:localhost"

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$send_evt")),
        ) as mock_send,
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="hello",
                thread_id=explicit_thread_id,
                attachments=["att_explicit_thread"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$send_evt"
    assert payload["thread_id"] == explicit_thread_id
    assert payload["attachment_event_ids"] == ["$file_evt"]
    assert payload["resolved_attachment_ids"] == ["att_explicit_thread"]
    mock_send.assert_awaited_once()
    sent_content = mock_send.await_args.args[2]
    relates_to = sent_content.get("m.relates_to", {})
    assert relates_to.get("event_id") == explicit_thread_id
    mock_send_file.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        attachment.local_path,
        thread_id=explicit_thread_id,
        # The text this same call just sent into the explicit thread.
        latest_thread_event_id="$send_evt",
    )


@pytest.mark.asyncio
async def test_matrix_message_send_allows_attachment_only(tmp_path: Path) -> None:
    """Send should allow attachments without a text body."""
    tool = MatrixMessageTools()
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_only",
    )
    assert attachment is not None
    ctx = _make_context(storage_path=tmp_path, attachment_ids=("att_only",))

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                attachments=["att_only"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$file_evt"
    assert payload["thread_id"] == ctx.resolved_thread_id
    assert payload["attachment_event_ids"] == ["$file_evt"]
    assert payload["resolved_attachment_ids"] == ["att_only"]
    mock_send.assert_not_awaited()
    mock_send_file.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        attachment.local_path,
        thread_id=ctx.resolved_thread_id,
        latest_thread_event_id=ctx.resolved_thread_id,
    )


@pytest.mark.asyncio
async def test_matrix_message_send_multiple_attachments_only_auto_threads_under_first_attachment(
    tmp_path: Path,
) -> None:
    """Attachment-only sends should use the first room-level attachment as the thread root for the rest."""
    tool = MatrixMessageTools()
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    first_file.write_text("first", encoding="utf-8")
    second_file.write_text("second", encoding="utf-8")
    first_attachment = register_local_attachment(
        tmp_path,
        first_file,
        kind="file",
        attachment_id="att_first",
    )
    second_attachment = register_local_attachment(
        tmp_path,
        second_file,
        kind="file",
        attachment_id="att_second",
    )
    assert first_attachment is not None
    assert second_attachment is not None
    ctx = _make_context(storage_path=tmp_path, attachment_ids=("att_first", "att_second"), thread_id=None)

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_resolved_attachments",
            new=AsyncMock(side_effect=[(["$file_root"], None), (["$file_child"], None)]),
        ) as mock_send_attachments,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                attachments=["att_first", "att_second"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$file_root"
    assert payload["thread_id"] == "$file_root"
    assert payload["attachment_event_ids"] == ["$file_root", "$file_child"]
    assert payload["resolved_attachment_ids"] == ["att_first", "att_second"]
    assert mock_send_attachments.await_args_list[0].args == (ctx,)
    assert mock_send_attachments.await_args_list[0].kwargs == {
        "room_id": ctx.room_id,
        "thread_id": None,
        "attachments": [first_attachment.local_path],
    }
    assert mock_send_attachments.await_args_list[1].args == (ctx,)
    assert mock_send_attachments.await_args_list[1].kwargs == {
        "room_id": ctx.room_id,
        "thread_id": "$file_root",
        "attachments": [second_attachment.local_path],
        "known_latest_thread_event_id": "$file_root",
    }


@pytest.mark.asyncio
async def test_matrix_message_send_multiple_attachments_only_in_room_mode_stays_room_level(
    tmp_path: Path,
) -> None:
    """Room-mode sends should not create an attachment thread when sending multiple files."""
    tool = MatrixMessageTools()
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    first_file.write_text("first", encoding="utf-8")
    second_file.write_text("second", encoding="utf-8")
    first_attachment = register_local_attachment(
        tmp_path,
        first_file,
        kind="file",
        attachment_id="att_room_first",
    )
    second_attachment = register_local_attachment(
        tmp_path,
        second_file,
        kind="file",
        attachment_id="att_room_second",
    )
    assert first_attachment is not None
    assert second_attachment is not None
    ctx = _make_context(
        storage_path=tmp_path,
        attachment_ids=("att_room_first", "att_room_second"),
        thread_id="$ctx-thread:localhost",
        resolved_thread_id=None,
        agent_thread_mode="room",
    )

    with (
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(side_effect=["$file_one", "$file_two"]),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                attachments=["att_room_first", "att_room_second"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$file_one"
    assert payload["thread_id"] is None
    assert payload["attachment_event_ids"] == ["$file_one", "$file_two"]
    assert payload["resolved_attachment_ids"] == ["att_room_first", "att_room_second"]
    assert len(mock_send_file.await_args_list) == 2
    first_call = mock_send_file.await_args_list[0]
    second_call = mock_send_file.await_args_list[1]
    assert first_call.args == (ctx.client, ctx.room_id, first_attachment.local_path)
    assert first_call.kwargs == {"thread_id": None, "latest_thread_event_id": None}
    assert second_call.args == (ctx.client, ctx.room_id, second_attachment.local_path)
    assert second_call.kwargs == {"thread_id": None, "latest_thread_event_id": "$file_one"}


@pytest.mark.asyncio
async def test_matrix_message_send_supports_attachment_file_paths(tmp_path: Path) -> None:
    """Send should auto-register local file paths and upload them."""
    tool = MatrixMessageTools()
    generated_file = tmp_path / "generated.txt"
    generated_file.write_text("artifact", encoding="utf-8")
    ctx = _make_context(storage_path=tmp_path)

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="hello",
                attachments=[str(generated_file)],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$evt"
    assert payload["thread_id"] == ctx.resolved_thread_id
    assert payload["attachment_event_ids"] == ["$file_evt"]
    assert payload["resolved_attachment_ids"][0].startswith("att_")
    assert payload["newly_registered_attachment_ids"] == payload["resolved_attachment_ids"]
    mock_send.assert_awaited_once()
    mock_send_file.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        generated_file,
        thread_id=ctx.resolved_thread_id,
        latest_thread_event_id="$evt",
    )


@pytest.mark.asyncio
async def test_matrix_message_send_resolves_relative_attachment_file_paths_from_workspace(tmp_path: Path) -> None:
    """Relative attachment_file_paths should resolve from the agent workspace root."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    generated_file = workspace_root / "scratch" / "generated.txt"
    generated_file.parent.mkdir()
    generated_file.write_text("artifact", encoding="utf-8")
    tool = MatrixMessageTools(tool_output_workspace_root=workspace_root)
    ctx = _make_context(storage_path=tmp_path)

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ),
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="hello",
                attachments=["scratch/generated.txt"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["attachment_event_ids"] == ["$file_evt"]
    mock_send_file.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        generated_file.resolve(),
        thread_id=ctx.resolved_thread_id,
        latest_thread_event_id="$evt",
    )


@pytest.mark.asyncio
async def test_matrix_message_send_text_failure_does_not_attempt_attachments(tmp_path: Path) -> None:
    """Attachment sends should not start when the text send fails."""
    tool = MatrixMessageTools()
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_text_fail",
    )
    assert attachment is not None
    ctx = _make_context(storage_path=tmp_path, attachment_ids=("att_text_fail",))

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(return_value=None),
        ) as mock_send,
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_resolved_attachments",
            new=AsyncMock(),
        ) as mock_send_resolved_attachments,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="hello",
                attachments=["att_text_fail"],
            ),
        )

    assert payload["status"] == "error"
    assert payload["action"] == "send"
    assert payload["message"] == "Failed to send message to Matrix."
    mock_send.assert_awaited_once()
    mock_send_resolved_attachments.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_send_multiple_attachments_only_returns_error_when_first_send_fails(
    tmp_path: Path,
) -> None:
    """Attachment-only auto-threading should stop immediately if the root attachment fails to send."""
    tool = MatrixMessageTools()
    first_file = tmp_path / "first.txt"
    second_file = tmp_path / "second.txt"
    first_file.write_text("first", encoding="utf-8")
    second_file.write_text("second", encoding="utf-8")
    first_attachment = register_local_attachment(
        tmp_path,
        first_file,
        kind="file",
        attachment_id="att_first_fail",
    )
    second_attachment = register_local_attachment(
        tmp_path,
        second_file,
        kind="file",
        attachment_id="att_second_fail",
    )
    assert first_attachment is not None
    assert second_attachment is not None
    ctx = _make_context(
        storage_path=tmp_path,
        attachment_ids=("att_first_fail", "att_second_fail"),
        thread_id=None,
    )

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_resolved_attachments",
            new=AsyncMock(return_value=([], "Failed to send attachment: first")),
        ) as mock_send_attachments,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                attachments=["att_first_fail", "att_second_fail"],
            ),
        )

    assert payload["status"] == "error"
    assert payload["event_id"] is None
    assert payload["thread_id"] is None
    assert payload["attachment_event_ids"] == []
    assert payload["resolved_attachment_ids"] == ["att_first_fail", "att_second_fail"]
    assert payload["newly_registered_attachment_ids"] == []
    assert "Failed to send attachment" in payload["message"]
    mock_send_attachments.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        thread_id=None,
        attachments=[first_attachment.local_path],
    )


@pytest.mark.asyncio
async def test_matrix_message_accepts_register_attachment_ids_across_task_boundaries(tmp_path: Path) -> None:
    """matrix_message should accept attachment IDs registered by a prior tool call in another task."""
    matrix_tool = MatrixMessageTools()
    attachment_tool = AttachmentTools()
    generated_file = tmp_path / "generated.txt"
    generated_file.write_text("artifact", encoding="utf-8")
    ctx = _make_context(storage_path=tmp_path)
    ctx.conversation_reader.latest_thread_event_id = AsyncMock(return_value=ctx.thread_id)

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        register_payload = json.loads(
            await asyncio.create_task(attachment_tool.register_attachment(str(generated_file))),
        )
        attachment_id = register_payload["attachment_id"]
        payload = json.loads(
            await asyncio.create_task(
                matrix_tool.matrix_message(
                    action="send",
                    message="hello",
                    attachments=[attachment_id],
                ),
            ),
        )

    assert register_payload["status"] == "ok"
    assert payload["status"] == "ok"
    assert payload["event_id"] == "$evt"
    assert payload["attachment_event_ids"] == ["$file_evt"]
    assert payload["resolved_attachment_ids"] == [attachment_id]
    mock_send.assert_awaited_once()
    mock_send_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_message_send_defaults_to_context_thread() -> None:
    """Send action should use current runtime thread when thread_id is omitted."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="send", message="hello"))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    sent_content = mock_send.await_args.args[2]
    relates_to = sent_content.get("m.relates_to", {})
    assert relates_to.get("event_id") == "$ctx-thread:localhost"


@pytest.mark.asyncio
async def test_matrix_message_react_happy_path() -> None:
    """React action should send a Matrix annotation event to the target event."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    response = MagicMock(spec=nio.RoomSendResponse)
    response.event_id = "$react"
    ctx.client.room_send.return_value = response

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="react", message="🔥", event_id="$target"))

    assert payload["status"] == "ok"
    assert payload["action"] == "react"
    assert payload["reacted_event_id"] == "$target"
    ctx.client.room_send.assert_awaited_once_with(
        room_id=ctx.room_id,
        message_type="m.reaction",
        content={
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": "$target",
                "key": "🔥",
            },
        },
        ignore_unverified_devices=True,
    )


@pytest.mark.asyncio
async def test_matrix_message_react_skips_interactive_processing() -> None:
    """React action should not touch interactive-question helpers."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    response = MagicMock(spec=nio.RoomSendResponse)
    response.event_id = "$react"
    ctx.client.room_send.return_value = response

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.parse_and_format_interactive",
        ) as mock_parse,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="react", message="🔥", event_id="$target"))

    assert payload["status"] == "ok"
    mock_parse.assert_not_called()


@pytest.mark.asyncio
async def test_matrix_message_edit_rejects_interactive_prompts() -> None:
    """Direct tool edits cannot durably own a prompt revision."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    thread_messages = [
        make_visible_message(event_id="$latest", timestamp=1, sender="@alice:localhost", body="latest"),
    ]
    interactive_message = """Updated prompt.

``` Interactive json
{
  "question": "Which option?",
  "options": [
    {"emoji": "✅", "label": "Approve", "value": "approve"},
    {"emoji": "❌", "label": "Reject", "value": "reject"}
  ]
}
```"""
    serve_conversation_reader(ctx.conversation_reader, thread_messages)

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit_evt")),
        ) as mock_edit,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="edit", message=interactive_message, event_id="$target"))

    assert payload == {
        "status": "error",
        "tool": "matrix_message",
        "action": "edit",
        "room_id": ctx.room_id,
        "message": "Interactive prompts are only supported in normal agent responses.",
    }
    mock_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_edit_includes_message_extras_on_replacement_wrapper() -> None:
    """Edit action should expose extras on both m.new_content and the outer edit event."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    thread_messages = [
        make_visible_message(event_id="$latest", timestamp=1, sender="@alice:localhost", body="latest"),
    ]
    serve_conversation_reader(ctx.conversation_reader, thread_messages)

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit_evt")),
        ) as mock_edit,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="edit",
                message="Updated answer.",
                event_id="$target",
                message_extras=[
                    {
                        "title": "Evidence",
                        "content": "extra details",
                    },
                ],
            ),
        )

    assert payload["status"] == "ok"
    new_content = mock_edit.await_args.args[3]
    extra_content = mock_edit.await_args.kwargs["extra_content"]
    expected_extras = {
        "version": 2,
        "sections": [
            {
                "title": "Evidence",
                "content_type": "text/markdown",
                "content": "extra details",
                "collapsed": True,
            },
        ],
    }
    assert new_content[MINDROOM_MESSAGE_EXTRAS_KEY] == expected_extras
    assert extra_content == {MINDROOM_MESSAGE_EXTRAS_KEY: expected_extras}


@pytest.mark.asyncio
async def test_matrix_message_edit_rejects_invalid_message_extras() -> None:
    """Invalid edit extras should fail before edit delivery."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit_evt")),
        ) as mock_edit,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="edit",
                message="Updated answer.",
                event_id="$target",
                message_extras=[
                    {
                        "title": "Raw",
                        "content_type": "application/json",
                        "content": "{}",
                    },
                ],
            ),
        )

    assert payload["status"] == "error"
    assert "content_type" in payload["message"]
    mock_edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_edit_plain_text_carries_no_interactive_prompt() -> None:
    """Editing away an interactive block should leave prompt metadata off the wire."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    thread_messages = [
        make_visible_message(event_id="$latest", timestamp=1, sender="@alice:localhost", body="latest"),
    ]
    serve_conversation_reader(ctx.conversation_reader, thread_messages)
    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit_evt")),
        ) as edit_result,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="edit", message="updated text", event_id="$target"))

    assert payload["status"] == "ok"
    assert "io.mindroom.interactive" not in edit_result.await_args.args[3]


def test_resolved_visible_message_to_dict_includes_msgtype() -> None:
    """Thread-list serialization should preserve the visible Matrix msgtype."""
    message = make_visible_message(
        event_id="$notice",
        body="notice",
        content={"body": "notice", "msgtype": "m.notice"},
    )

    assert message.to_dict()["msgtype"] == "m.notice"


@pytest.mark.asyncio
async def test_matrix_message_read_thread_enforces_max_limit() -> None:
    """Thread reads should be bounded by the configured max limit."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    thread_messages = [
        make_visible_message(event_id=f"${index}", timestamp=index, body=f"m{index}") for index in range(100)
    ]
    serve_conversation_reader(ctx.conversation_reader, thread_messages)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read", limit=999))

    assert payload["status"] == "ok"
    assert payload["limit"] == MatrixMessageTools._MAX_READ_LIMIT
    assert len(payload["messages"]) == MatrixMessageTools._MAX_READ_LIMIT
    assert "edit_options" in payload
    ctx.conversation_reader.read_strict.assert_awaited_once_with(
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
    )


@pytest.mark.asyncio
async def test_matrix_message_read_thread_includes_edit_options() -> None:
    """Thread reads should include event IDs that can be edited."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    ctx.client.user_id = "@mindroom_general:localhost"
    thread_messages = [
        make_visible_message(event_id="$one", timestamp=1, sender="@alice:localhost", body="earlier message"),
        make_visible_message(
            event_id="$two",
            timestamp=2,
            sender="@mindroom_general:localhost",
            body="latest message",
        ),
    ]
    serve_conversation_reader(ctx.conversation_reader, thread_messages)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read"))

    assert payload["status"] == "ok"
    assert payload["action"] == "read"
    assert payload["thread_id"] == ctx.thread_id
    assert payload["edit_options"][0]["event_id"] == "$two"
    assert payload["edit_options"][0]["can_edit"] is True
    assert payload["edit_options"][0]["edit_action"] == {"action": "edit", "event_id": "$two"}
    assert payload["edit_options"][1]["event_id"] == "$one"
    assert payload["edit_options"][1]["can_edit"] is False
    assert "edit_action" not in payload["edit_options"][1]


@pytest.mark.asyncio
async def test_matrix_message_read_returns_thread_messages() -> None:
    """Thread reads should return thread messages and edit options."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    ctx.client.user_id = "@mindroom_general:localhost"
    thread_messages = [
        make_visible_message(event_id="$one", timestamp=1, sender="@mindroom_general:localhost", body="first"),
        make_visible_message(event_id="$two", timestamp=2, sender="@alice:localhost", body="second"),
    ]
    serve_conversation_reader(ctx.conversation_reader, thread_messages)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(
                action="read",
                thread_id="$thread-other:localhost",
                limit=1,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "read"
    assert payload["thread_id"] == "$thread-other:localhost"
    assert payload["messages"] == [thread_messages[-1].to_dict()]
    assert payload["edit_options"][0]["event_id"] == "$two"
    ctx.conversation_reader.read_strict.assert_awaited_once_with(
        room_id=ctx.room_id,
        thread_id="$thread-other:localhost",
        limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
    )


@pytest.mark.asyncio
async def test_matrix_message_read_preserves_notice_messages() -> None:
    """Thread reads should surface notice msgtypes unchanged."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    thread_messages = [
        make_visible_message(event_id="$one", timestamp=1, sender="@alice:localhost", body="first"),
        make_visible_message(
            event_id="$notice",
            timestamp=2,
            sender="@mindroom_general:localhost",
            body="Compacted 12 messages",
            content={"body": "Compacted 12 messages", "msgtype": "m.notice"},
        ),
    ]
    serve_conversation_reader(ctx.conversation_reader, thread_messages)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(
                action="read",
                thread_id="$thread-other:localhost",
                limit=2,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["messages"] == [message.to_dict() for message in thread_messages]
    assert payload["messages"][1]["msgtype"] == "m.notice"
    ctx.conversation_reader.read_strict.assert_awaited_once_with(
        room_id=ctx.room_id,
        thread_id="$thread-other:localhost",
        limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
    )


@pytest.mark.asyncio
async def test_matrix_message_read_room_happy_path() -> None:
    """Room reads should resolve message events when no thread is active."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    response = nio.RoomMessagesResponse.from_dict(
        {
            "chunk": [
                {
                    "type": "m.room.message",
                    "event_id": "$evt",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 1,
                    "content": {"msgtype": "m.text", "body": "hello"},
                },
            ],
            "start": "s",
            "end": "e",
        },
        ctx.room_id,
    )
    ctx.client.room_messages.return_value = response

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read", limit=5))

    assert payload["status"] == "ok"
    assert payload["limit"] == 5
    assert [(message["event_id"], message["body"]) for message in payload["messages"]] == [("$evt", "hello")]
    ctx.client.room_messages.assert_awaited_once_with(
        ctx.room_id,
        limit=5,
        direction=nio.MessageDirection.back,
        message_filter={"types": ["m.room.message", "m.room.encrypted"]},
    )


@pytest.mark.asyncio
async def test_matrix_message_read_room_includes_every_msgtype_that_carries_a_body() -> None:
    """A room read keeps text, notices, emotes, and media: one rule, not a curated list.

    The emote was missing, because this read kept its own copy of the visible
    msgtype list and that copy said text and notice. Sharing one rule with the
    thread read fixed that and left the picture missing for the same reason,
    one msgtype further out. The journal projection holds every
    `m.room.message` it admits, whether admission called it a message or media,
    so an agent reading the room saw a conversation with a `/me` and an image
    cut out of it while the same conversation watched live still had both.
    """
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    response = nio.RoomMessagesResponse.from_dict(
        {
            "chunk": [
                {
                    "type": "m.room.message",
                    "event_id": "$image",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 4,
                    "content": {
                        "msgtype": "m.image",
                        "body": "the original caption",
                        "url": "mxc://localhost/picture",
                    },
                },
                {
                    "type": "m.room.message",
                    "event_id": "$caption-edit",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 5,
                    "content": {
                        "msgtype": "m.image",
                        "body": "* the corrected caption",
                        "url": "mxc://localhost/picture",
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$image"},
                        "m.new_content": {
                            "msgtype": "m.image",
                            "body": "the corrected caption",
                            "url": "mxc://localhost/picture",
                        },
                    },
                },
                {
                    "type": "m.room.message",
                    "event_id": "$emote",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 3,
                    "content": {"msgtype": "m.emote", "body": "waves at the bot"},
                },
                {
                    "type": "m.room.message",
                    "event_id": "$notice",
                    "sender": "@mindroom:localhost",
                    "origin_server_ts": 2,
                    "content": {"msgtype": "m.notice", "body": "Compacted 12 messages"},
                },
                {
                    "type": "m.room.message",
                    "event_id": "$text",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 1,
                    "content": {"msgtype": "m.text", "body": "hello"},
                },
            ],
            "start": "s",
            "end": "e",
        },
        ctx.room_id,
    )
    ctx.client.room_messages.return_value = response

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read", limit=5))

    assert payload["status"] == "ok"
    assert [(message["event_id"], message["body"], message.get("msgtype")) for message in payload["messages"]] == [
        ("$text", "hello", None),
        ("$notice", "Compacted 12 messages", "m.notice"),
        ("$emote", "waves at the bot", "m.emote"),
        # One row, carrying the caption its sender corrected. The picture is
        # folded like any other edited message: the read is ordered by the
        # original's timestamp and reports the revision that is current.
        ("$image", "the corrected caption", "m.image"),
    ]


@pytest.mark.asyncio
async def test_matrix_message_read_room_folds_a_text_edit_onto_the_picture_it_corrects() -> None:
    """A replacement may change the msgtype, and dropping the original invented a message.

    This is the failure the other direction produced here, and it is worse than
    an absence. The image was filtered out of the read while the text edit that
    replaced it was not, so the fold found a replacement whose original it had
    never seen and reconstructed one from the replacement alone: a message with
    the edit's timestamp for a position it never had, and its placement
    reported as unknown. A model reading the room was handed a message the room
    does not contain, in place of the picture it does.
    """
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    response = nio.RoomMessagesResponse.from_dict(
        {
            "chunk": [
                {
                    "type": "m.room.message",
                    "event_id": "$edit",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 2,
                    "content": {
                        "msgtype": "m.text",
                        "body": "* words instead",
                        "m.relates_to": {"rel_type": "m.replace", "event_id": "$image"},
                        "m.new_content": {"msgtype": "m.text", "body": "words instead"},
                    },
                },
                {
                    "type": "m.room.message",
                    "event_id": "$image",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 1,
                    "content": {
                        "msgtype": "m.image",
                        "body": "the original caption",
                        "url": "mxc://localhost/picture",
                    },
                },
            ],
            "start": "s",
            "end": "e",
        },
        ctx.room_id,
    )
    ctx.client.room_messages.return_value = response

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read", limit=5))

    assert payload["status"] == "ok"
    assert len(payload["messages"]) == 1
    message = payload["messages"][0]
    assert message["event_id"] == "$image"
    assert message["body"] == "words instead"
    assert message["latest_event_id"] == "$edit"
    # The original's own position, and a placement that is a fact rather than a
    # reconstruction. Both were lost when the picture was filtered away.
    assert message["timestamp"] == 1
    assert "thread_id_unknown" not in message


@pytest.mark.asyncio
async def test_matrix_message_read_room_precomputes_trusted_sender_ids_once() -> None:
    """Room reads should derive the trust set once and resolve every message under it."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    response = nio.RoomMessagesResponse.from_dict(
        {
            "chunk": [
                {
                    "type": "m.room.message",
                    "event_id": "$agent",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 2,
                    "content": {
                        "msgtype": "m.notice",
                        "body": "Answer\n\n⏳ Preparing isolated worker...",
                        STREAM_VISIBLE_BODY_KEY: "Answer",
                    },
                },
                {
                    "type": "m.room.message",
                    "event_id": "$text",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 1,
                    "content": {"msgtype": "m.text", "body": "hello"},
                },
            ],
            "start": "s",
            "end": "e",
        },
        ctx.room_id,
    )
    ctx.client.room_messages.return_value = response

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.trusted_visible_sender_ids",
            wraps=trusted_visible_sender_ids,
        ) as mock_trusted_sender_ids,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="read", limit=5))

    assert payload["status"] == "ok"
    # The trusted sender's canonical body wins over its transport text, which is
    # only true if the derived trust set reached the resolution.
    assert [(message["event_id"], message["body"]) for message in payload["messages"]] == [
        ("$text", "hello"),
        ("$agent", "Answer"),
    ]
    mock_trusted_sender_ids.assert_called_once_with(ctx.config, ctx.runtime_paths)


@pytest.mark.asyncio
async def test_matrix_message_read_room_collapses_edits_into_one_message() -> None:
    """A room read must report an edited message once, at its newest revision.

    The thread read reads the projection, which stores one row per logical
    message, so an edit revises a message rather than adding one. A room read
    paginates the raw timeline, where every revision is its own
    ``m.room.message`` event, and a model handed one message per revision reads
    a corrected sentence as several people saying nearly the same thing.
    """
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    revisions = [
        {
            "type": "m.room.message",
            "event_id": f"$edit-{index}",
            "sender": "@alice:localhost",
            "origin_server_ts": index,
            "content": {
                "msgtype": "m.text",
                "body": f"* revision {index}",
                "m.new_content": {"msgtype": "m.text", "body": f"revision {index}"},
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
            },
        }
        for index in (3, 2)
    ]
    response = nio.RoomMessagesResponse.from_dict(
        {
            "chunk": [
                *revisions,
                {
                    "type": "m.room.message",
                    "event_id": "$original",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 1,
                    "content": {"msgtype": "m.text", "body": "first draft"},
                },
            ],
            "start": "s",
            "end": "e",
        },
        ctx.room_id,
    )
    ctx.client.room_messages.return_value = response

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read", limit=5))

    assert payload["status"] == "ok"
    assert [message["event_id"] for message in payload["messages"]] == ["$original"]
    assert payload["messages"][0]["body"] == "revision 3"
    assert payload["messages"][0]["latest_event_id"] == "$edit-3"


@pytest.mark.asyncio
async def test_matrix_message_read_room_does_not_place_an_off_window_message_in_the_room() -> None:
    """A revision whose message scrolled out of the window must not be reported as room-level.

    A streaming answer is one message and many revisions, so a window of raw events easily holds
    the revisions without the reply they revise. The reply's thread lives on that reply alone --
    Matrix has the replacement inherit ``m.relates_to`` rather than restate it, so no client puts
    it on an edit -- which leaves the fold with a message it cannot place. Reporting no thread
    reads as room level, and an agent following up on its own answer then posts outside the thread
    the answer belongs to.
    """
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    edit_content = build_edit_event_content(
        event_id="$agent-reply",
        new_content={"msgtype": "m.notice", "body": "final answer"},
        new_text="final answer",
    )
    response = nio.RoomMessagesResponse.from_dict(
        {
            "chunk": [
                {
                    "type": "m.room.message",
                    "event_id": "$edit",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 20,
                    "content": edit_content,
                },
            ],
            "start": "s",
            "end": "e",
        },
        ctx.room_id,
    )
    ctx.client.room_messages.return_value = response

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read", limit=5))

    assert payload["status"] == "ok"
    message = payload["messages"][0]
    assert (message["event_id"], message["body"]) == ("$agent-reply", "final answer")
    assert "thread_id" not in message
    assert message["thread_id_unknown"] is True


@pytest.mark.asyncio
async def test_matrix_message_read_room_sentinel_uses_room_timeline() -> None:
    """thread_id='room' should bypass the current thread and read the room timeline."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    response = nio.RoomMessagesResponse.from_dict(
        {
            "chunk": [
                {
                    "type": "m.room.message",
                    "event_id": "$evt",
                    "sender": "@alice:localhost",
                    "origin_server_ts": 1,
                    "content": {"msgtype": "m.text", "body": "hello from room"},
                },
            ],
            "start": "s",
            "end": "e",
        },
        ctx.room_id,
    )
    ctx.client.room_messages.return_value = response

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read", thread_id="room", limit=5))

    assert payload["status"] == "ok"
    assert payload["action"] == "read"
    assert payload["limit"] == 5
    assert [(message["event_id"], message["body"]) for message in payload["messages"]] == [
        ("$evt", "hello from room"),
    ]
    assert "thread_id" not in payload
    ctx.client.room_messages.assert_awaited_once_with(
        ctx.room_id,
        limit=5,
        direction=nio.MessageDirection.back,
        message_filter={"types": ["m.room.message", "m.room.encrypted"]},
    )
    ctx.conversation_reader.read_strict.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_read_explicit_thread_id_still_reads_that_thread() -> None:
    """Explicit thread IDs should win over runtime thread fallback for read."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    thread_messages = [
        make_visible_message(event_id="$one", timestamp=1, body="first"),
        make_visible_message(event_id="$two", timestamp=2, body="second"),
    ]
    serve_conversation_reader(ctx.conversation_reader, thread_messages)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(action="read", thread_id="$thread-other:localhost", limit=1),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "read"
    assert payload["thread_id"] == "$thread-other:localhost"
    assert payload["messages"] == [thread_messages[-1].to_dict()]
    ctx.conversation_reader.read_strict.assert_awaited_once_with(
        room_id=ctx.room_id,
        thread_id="$thread-other:localhost",
        limit=HYDRATED_PROMPT_WINDOW_MESSAGES,
    )
    ctx.client.room_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_edit_happy_path() -> None:
    """Edit should update an existing message by target event ID."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    ctx.conversation_reader.latest_thread_event_id = AsyncMock(return_value="$latest")
    sent_content: dict[str, object] = {}

    async def _deliver_edit(
        _client: object,
        _room_id: str,
        content: dict[str, object],
        **_kwargs: object,
    ) -> DeliveredMatrixEvent:
        sent_content.update(content)
        return delivered_matrix_event("$edit_evt", content)

    with (
        patch(
            "mindroom.matrix.client_delivery.send_message_outcome",
            new=AsyncMock(side_effect=_deliver_edit),
        ),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="edit", message="updated text", event_id="$target"))

    assert payload["status"] == "ok"
    assert payload["action"] == "edit"
    assert payload["edited_event_id"] == "$target"
    assert payload["event_id"] == "$edit_evt"
    relation = sent_content["m.relates_to"]
    assert relation == {"rel_type": "m.replace", "event_id": "$target"}
    replacement = sent_content["m.new_content"]
    assert isinstance(replacement, dict)
    assert replacement["body"] == "updated text"
    assert "m.relates_to" not in replacement
    ctx.conversation_reader.latest_thread_event_id.assert_not_awaited()
    ctx.conversation_reader.read_strict.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_edit_requires_target() -> None:
    """Edit action should require target event ID."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="edit", message="updated text"))

    assert payload["status"] == "error"
    assert "event_id is required for edit" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_edit_requires_message() -> None:
    """Edit action should require non-empty replacement text."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="edit", event_id="$target", message="  "))

    assert payload["status"] == "error"
    assert "message is required for edit" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_send_validates_non_empty_message() -> None:
    """Send should reject calls where both message and attachments are empty."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="send", message="  "))

    assert payload["status"] == "error"
    assert "Provide message, attachments, or both" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_rejects_attachments_for_non_send_actions(tmp_path: Path) -> None:
    """Attachments should be accepted only by send/reply/thread-reply actions."""
    tool = MatrixMessageTools()
    ctx = _make_context(storage_path=tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(
                action="react",
                event_id="$target",
                attachments=["att_upload"],
            ),
        )

    assert payload["status"] == "error"
    assert "only supported for send" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_rejects_missing_attachment_paths(tmp_path: Path) -> None:
    """Missing attachment paths must fail before sending."""
    tool = MatrixMessageTools()
    ctx = _make_context(storage_path=tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                attachments=["output.txt"],
            ),
        )

    assert payload["status"] == "error"
    assert "Failed to register attachment file" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_rejects_attachment_count_over_limit(tmp_path: Path) -> None:
    """Send should enforce a maximum attachment count per call."""
    tool = MatrixMessageTools()
    ctx = _make_context(storage_path=tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                attachments=["att_over"] * 6,
            ),
        )

    assert payload["status"] == "error"
    assert "cannot exceed 5" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_react_requires_target() -> None:
    """React action should validate that target event ID is provided."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="react", message="👍"))

    assert payload["status"] == "error"
    assert "event_id is required" in payload["message"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_matrix_message_explicit_room_target_requires_authorization() -> None:
    """Explicit room targeting should enforce authorization checks."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="send", message="hello", room_id="!other:localhost"))

    assert payload["status"] == "error"
    assert "Not authorized" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_rejects_unsupported_action() -> None:
    """Unsupported actions should return a clear validation error."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="delete", message="hello"))  # type: ignore[arg-type]

    assert payload["status"] == "error"
    assert "Unsupported action" in payload["message"]
    assert "send, read, edit, or react" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_rate_limit_guardrail() -> None:
    """Tool should block rapid repeated actions in the same room context."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ),
        patch.object(MatrixMessageTools, "_RATE_LIMIT_MAX_ACTIONS", 1),
        patch.object(MatrixMessageTools, "_RATE_LIMIT_WINDOW_SECONDS", 60.0),
        tool_runtime_context(ctx),
    ):
        first = json.loads(await tool.matrix_message(action="send", message="first"))
        second = json.loads(await tool.matrix_message(action="send", message="second"))

    assert first["status"] == "ok"
    assert second["status"] == "error"
    assert "Rate limit exceeded" in second["message"]


@pytest.mark.asyncio
async def test_matrix_message_rate_limit_counts_attachments_weight(tmp_path: Path) -> None:
    """Rate limiting should charge one tick for message plus one per attachment."""
    tool = MatrixMessageTools()
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_weighted",
    )
    assert attachment is not None
    ctx = _make_context(storage_path=tmp_path, attachment_ids=("att_weighted",))

    with (
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$evt")),
        ),
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ),
        patch.object(MatrixMessageTools, "_RATE_LIMIT_MAX_ACTIONS", 2),
        patch.object(MatrixMessageTools, "_RATE_LIMIT_WINDOW_SECONDS", 60.0),
        tool_runtime_context(ctx),
    ):
        first = json.loads(
            await tool.matrix_message(
                action="send",
                message="first",
                attachments=["att_weighted"],
            ),
        )
        second = json.loads(await tool.matrix_message(action="send", message="second"))

    assert first["status"] == "ok"
    assert second["status"] == "error"
    assert "Rate limit exceeded" in second["message"]


@pytest.mark.asyncio
async def test_matrix_message_cross_room_send_does_not_inherit_context_thread() -> None:
    """Authorized cross-room send should not inherit the origin room's thread."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$origin-thread:localhost")

    with (
        patch("mindroom.custom_tools.matrix_message.room_access_allowed", return_value=True),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$sent")),
        ) as send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(action="send", message="hello", room_id="!other:localhost"),
        )

    assert payload["status"] == "ok"
    assert payload["thread_id"] is None
    assert "m.relates_to" not in send.await_args.args[2]


@pytest.mark.asyncio
async def test_matrix_message_cross_room_read_defaults_to_room_level() -> None:
    """Authorized cross-room read should not use the origin room's thread."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$origin-thread:localhost")
    response = MagicMock(spec=nio.RoomMessagesResponse)
    response.chunk = []
    ctx.client.room_messages.return_value = response

    with (
        patch("mindroom.custom_tools.matrix_message.room_access_allowed", return_value=True),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(action="read", room_id="!other:localhost"),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "read"
    assert "thread_id" not in payload
    ctx.client.room_messages.assert_awaited_once()
