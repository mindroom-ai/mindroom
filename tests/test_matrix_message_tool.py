"""Tests for the native matrix_message tool."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.attachments import register_local_attachment
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.tool_runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tools_metadata import TOOL_METADATA, get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_matrix_message_rate_limit() -> None:
    MatrixMessageTools._recent_actions.clear()


def _make_context(
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$thread:localhost",
    reply_to_event_id: str | None = "$reply:localhost",
    storage_path: Path | None = None,
    attachment_ids: tuple[str, ...] = (),
) -> ToolRuntimeContext:
    config = Config(agents={"general": AgentConfig(display_name="General Agent")})
    client = AsyncMock()
    client.room_send = AsyncMock()
    client.room_messages = AsyncMock()
    return ToolRuntimeContext(
        agent_name="general",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id="@user:localhost",
        client=client,
        config=config,
        room=None,
        reply_to_event_id=reply_to_event_id,
        storage_path=storage_path,
        attachment_ids=attachment_ids,
    )


def test_matrix_message_tool_registered_and_instantiates() -> None:
    """Matrix message tool should be available from metadata registry."""
    assert "matrix_message" in TOOL_METADATA
    assert isinstance(get_tool_by_name("matrix_message"), MatrixMessageTools)


@pytest.mark.asyncio
async def test_matrix_message_requires_runtime_context() -> None:
    """Tool should fail clearly when called without Matrix runtime context."""
    payload = json.loads(await MatrixMessageTools().matrix_message(action="send", message="hello"))
    assert payload["status"] == "error"
    assert payload["tool"] == "matrix_message"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_send_defaults_to_room_level() -> None:
    """Send action should stay room-level unless a thread is explicitly passed."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="send", message="hello"))

    assert payload["status"] == "ok"
    assert payload["action"] == "send"
    assert payload["thread_id"] is None
    sent_content = mock_send.await_args.args[2]
    assert sent_content["body"] == "hello"
    assert "m.relates_to" not in sent_content


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

    with (
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")) as mock_send,
        patch(
            "mindroom.custom_tools.matrix_message.send_file_message",
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
    assert payload["attachment_event_ids"] == ["$file_evt"]
    assert payload["resolved_attachment_ids"] == ["att_upload"]
    mock_send.assert_awaited_once()
    mock_send_file.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        attachment.local_path,
        thread_id=None,
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
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")) as mock_send,
        patch(
            "mindroom.custom_tools.matrix_message.send_file_message",
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
    assert payload["event_id"] is None
    assert payload["attachment_event_ids"] == ["$file_evt"]
    assert payload["resolved_attachment_ids"] == ["att_only"]
    mock_send.assert_not_awaited()
    mock_send_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_message_reply_defaults_to_context_thread() -> None:
    """Reply action should use current runtime thread when thread_id is omitted."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="reply", message="hello"))

    assert payload["status"] == "ok"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    sent_content = mock_send.await_args.args[2]
    relates_to = sent_content.get("m.relates_to", {})
    assert relates_to.get("event_id") == "$ctx-thread:localhost"


@pytest.mark.asyncio
async def test_matrix_message_thread_reply_defaults_to_context_thread() -> None:
    """thread-reply action should use current runtime thread when thread_id is omitted."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="thread-reply", message="hello"))

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
        payload = json.loads(await tool.matrix_message(action="react", message="🔥", target="$target"))

    assert payload["status"] == "ok"
    assert payload["action"] == "react"
    assert payload["target"] == "$target"
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
    )


@pytest.mark.asyncio
async def test_matrix_message_read_thread_enforces_max_limit() -> None:
    """Thread reads should be bounded by the configured max limit."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    thread_messages = [{"event_id": f"${index}", "timestamp": index, "body": f"m{index}"} for index in range(100)]

    with (
        patch(
            "mindroom.custom_tools.matrix_message.fetch_thread_history",
            new=AsyncMock(return_value=thread_messages),
        ) as mock_fetch,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="read", limit=999))

    assert payload["status"] == "ok"
    assert payload["limit"] == MatrixMessageTools._MAX_READ_LIMIT
    assert len(payload["messages"]) == MatrixMessageTools._MAX_READ_LIMIT
    mock_fetch.assert_awaited_once_with(ctx.client, ctx.room_id, ctx.thread_id)


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

    with (
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(return_value={"event_id": "$evt", "body": "hello"}),
        ) as mock_extract,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="read", limit=5))

    assert payload["status"] == "ok"
    assert payload["limit"] == 5
    assert payload["messages"] == [{"event_id": "$evt", "body": "hello"}]
    ctx.client.room_messages.assert_awaited_once_with(
        ctx.room_id,
        limit=5,
        direction=nio.MessageDirection.back,
        message_filter={"types": ["m.room.message"]},
    )
    mock_extract.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_message_send_validates_non_empty_message() -> None:
    """Send should reject calls where both message and attachments are empty."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="send", message="  "))

    assert payload["status"] == "error"
    assert "At least one of message or attachments" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_rejects_attachments_for_non_send_actions(tmp_path: Path) -> None:
    """Attachments should be accepted only by send/reply/thread-reply actions."""
    tool = MatrixMessageTools()
    ctx = _make_context(storage_path=tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(
                action="react",
                target="$target",
                attachments=["att_upload"],
            ),
        )

    assert payload["status"] == "error"
    assert "only supported for send, reply, and thread-reply" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_rejects_non_att_attachment_references(tmp_path: Path) -> None:
    """Attachment refs should require context-scoped att_* IDs."""
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
    assert "must be context attachment IDs" in payload["message"]


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
async def test_matrix_message_reply_requires_thread_when_context_has_none() -> None:
    """Reply action should fail when no thread is provided or active."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="reply", message="hello"))

    assert payload["status"] == "error"
    assert "thread_id is required" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_react_requires_target() -> None:
    """React action should validate that target event ID is provided."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="react", message="👍"))

    assert payload["status"] == "error"
    assert "target event_id is required" in payload["message"]


@pytest.mark.asyncio
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
        payload = json.loads(await tool.matrix_message(action="delete", message="hello"))

    assert payload["status"] == "error"
    assert payload["action"] == "delete"
    assert "Unsupported action" in payload["message"]
    assert "reply" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_rate_limit_guardrail() -> None:
    """Tool should block rapid repeated actions in the same room context."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with (
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")),
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
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")),
        patch(
            "mindroom.custom_tools.matrix_message.send_file_message",
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
async def test_matrix_message_context_returns_runtime_metadata() -> None:
    """Context action should expose room/thread/event identifiers for targeting."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$thread-root:localhost", reply_to_event_id="$event:localhost")

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="context"))

    assert payload["status"] == "ok"
    assert payload["action"] == "context"
    assert payload["room_id"] == ctx.room_id
    assert payload["thread_id"] == "$thread-root:localhost"
    assert payload["reply_to_event_id"] == "$event:localhost"


@pytest.mark.asyncio
async def test_matrix_message_cross_room_reply_does_not_inherit_context_thread() -> None:
    """Authorized cross-room reply should not inherit the origin room's thread."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$origin-thread:localhost")

    with (
        patch("mindroom.custom_tools.matrix_message.is_authorized_sender", return_value=True),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(action="reply", message="hello", room_id="!other:localhost"),
        )

    assert payload["status"] == "error"
    assert "thread_id is required" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_cross_room_read_defaults_to_room_level() -> None:
    """Authorized cross-room read should not use the origin room's thread."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$origin-thread:localhost")
    response = MagicMock(spec=nio.RoomMessagesResponse)
    response.chunk = []
    ctx.client.room_messages.return_value = response

    with (
        patch("mindroom.custom_tools.matrix_message.is_authorized_sender", return_value=True),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(action="read", room_id="!other:localhost"),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "read"
    assert "thread_id" not in payload
    ctx.client.room_messages.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_message_cross_room_context_does_not_leak_thread() -> None:
    """Authorized cross-room context should not return the origin room's thread."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$origin-thread:localhost", reply_to_event_id="$evt:localhost")

    with (
        patch("mindroom.custom_tools.matrix_message.is_authorized_sender", return_value=True),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(action="context", room_id="!other:localhost"),
        )

    assert payload["status"] == "ok"
    assert payload["thread_id"] is None
    assert payload["reply_to_event_id"] is None
