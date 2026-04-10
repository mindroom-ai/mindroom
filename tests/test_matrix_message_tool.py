"""Tests for the native matrix_message tool."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom import interactive
from mindroom.attachments import register_local_attachment
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.attachments import AttachmentTools
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.interactive import parse_and_format_interactive
from mindroom.matrix.client import RoomThreadsPageError
from mindroom.tool_system.metadata import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import bind_runtime_paths, make_visible_message, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture(autouse=True)
def _reset_matrix_message_rate_limit() -> None:
    MatrixMessageTools._recent_actions.clear()


def _empty_async_iterator() -> AsyncIterator[object]:
    async def iterator() -> AsyncIterator[object]:
        if False:
            yield None

    return iterator()


@pytest.fixture(autouse=True)
def _reset_interactive_state() -> None:
    interactive._active_questions.clear()
    interactive._persistence_file = None


def _make_context(
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$thread:localhost",
    reply_to_event_id: str | None = "$reply:localhost",
    storage_path: Path | None = None,
    attachment_ids: tuple[str, ...] = (),
) -> ToolRuntimeContext:
    runtime_root = storage_path or Path(tempfile.mkdtemp())
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General Agent")}),
        test_runtime_paths(runtime_root),
    )
    client = AsyncMock()
    client.room_send = AsyncMock()
    client.room_messages = AsyncMock()
    client.room_get_event_relations = MagicMock(
        side_effect=lambda *_args, **_kwargs: _empty_async_iterator(),
    )
    return ToolRuntimeContext(
        agent_name="general",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_access=AsyncMock(),
        room=None,
        reply_to_event_id=reply_to_event_id,
        storage_path=storage_path,
        attachment_ids=attachment_ids,
    )


def _make_room_thread_root(
    *,
    event_id: str,
    sender: str,
    timestamp: int,
    body: str | None = None,
    reply_count: int | None = None,
    encrypted: bool = False,
) -> MagicMock:
    event = MagicMock(spec=nio.MegolmEvent if encrypted else nio.RoomMessageText)
    event.event_id = event_id
    event.sender = sender
    event.server_timestamp = timestamp
    if body is not None:
        event.body = body

    source: dict[str, object] = {
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": timestamp,
    }
    if encrypted:
        source["type"] = "m.room.encrypted"
        source["content"] = {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "ciphertext",
            "device_id": "DEVICE",
            "sender_key": "sender_key",
            "session_id": "session_id",
        }
    else:
        source["type"] = "m.room.message"
        source["content"] = {"msgtype": "m.text", "body": body or ""}
    if reply_count is not None:
        source["unsigned"] = {"m.relations": {"m.thread": {"count": reply_count}}}
    event.source = source
    return event


def _make_bundled_replacement(
    *,
    event_id: str,
    body: str,
    msgtype: str,
    bundle_key: str | None = None,
) -> dict[str, object]:
    replacement_event = {
        "type": "m.room.message",
        "event_id": f"{event_id}-edit",
        "sender": "@editor:localhost",
        "origin_server_ts": 9999,
        "content": {
            "body": f"* {body}",
            "msgtype": msgtype,
            "m.new_content": {
                "body": body,
                "msgtype": msgtype,
            },
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": event_id,
            },
        },
    }
    if bundle_key is None:
        return replacement_event
    return {bundle_key: replacement_event}


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
async def test_matrix_message_send_room_sentinel_stays_room_level() -> None:
    """thread_id='room' should disable thread metadata for sends."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_latest_thread_event_id_if_needed",
            new=AsyncMock(return_value=None),
        ) as mock_latest_thread,
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")) as mock_send,
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
    mock_latest_thread.assert_awaited_once_with(ctx.client, ctx.room_id, None)
    sent_content = mock_send.await_args.args[2]
    assert sent_content["body"] == "hello"
    assert "m.relates_to" not in sent_content


@pytest.mark.asyncio
async def test_matrix_message_send_interactive_block_registers_question_and_adds_reactions() -> None:
    """Interactive sends should format the question and add reaction buttons."""
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
    formatted_text = parse_and_format_interactive(interactive_message, extract_mapping=False).formatted_text

    with (
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")) as mock_send,
        patch("mindroom.custom_tools.matrix_message.register_interactive_question") as mock_register,
        patch(
            "mindroom.custom_tools.matrix_message.add_reaction_buttons",
            new_callable=AsyncMock,
        ) as mock_add_reactions,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="send", message=interactive_message))

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$evt"
    sent_content = mock_send.await_args.args[2]
    assert sent_content["body"] == formatted_text
    mock_register.assert_called_once_with(
        "$evt",
        ctx.room_id,
        None,
        {
            "✅": "approve",
            "1": "approve",
            "❌": "reject",
            "2": "reject",
        },
        ctx.agent_name,
    )
    mock_add_reactions.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$evt",
        [
            {"emoji": "✅", "label": "Approve", "value": "approve"},
            {"emoji": "❌", "label": "Reject", "value": "reject"},
        ],
    )


@pytest.mark.asyncio
async def test_matrix_message_send_plain_text_skips_interactive_registration_and_reactions() -> None:
    """Plain-text sends should not register interactive state or add reactions."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with (
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")),
        patch(
            "mindroom.custom_tools.matrix_message.parse_and_format_interactive",
            wraps=parse_and_format_interactive,
        ) as mock_parse,
        patch(
            "mindroom.custom_tools.matrix_message.should_create_interactive_question",
            return_value=False,
        ) as mock_should_create,
        patch("mindroom.custom_tools.matrix_message.register_interactive_question") as mock_register,
        patch(
            "mindroom.custom_tools.matrix_message.add_reaction_buttons",
            new_callable=AsyncMock,
        ) as mock_add_reactions,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="send", message="hello"))

    assert payload["status"] == "ok"
    mock_parse.assert_called_once_with("hello", extract_mapping=False)
    mock_should_create.assert_called_once_with("hello")
    mock_register.assert_not_called()
    mock_add_reactions.assert_not_awaited()


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
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                message="hello",
                attachment_ids=["att_upload"],
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
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(return_value="$file_evt"),
        ) as mock_send_file,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="send",
                attachment_ids=["att_only"],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] is None
    assert payload["attachment_event_ids"] == ["$file_evt"]
    assert payload["resolved_attachment_ids"] == ["att_only"]
    mock_send.assert_not_awaited()
    mock_send_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_message_send_supports_attachment_file_paths(tmp_path: Path) -> None:
    """Send should auto-register local file paths and upload them."""
    tool = MatrixMessageTools()
    generated_file = tmp_path / "generated.txt"
    generated_file.write_text("artifact", encoding="utf-8")
    ctx = _make_context(storage_path=tmp_path)

    with (
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")) as mock_send,
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
                attachment_file_paths=[str(generated_file)],
            ),
        )

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$evt"
    assert payload["attachment_event_ids"] == ["$file_evt"]
    assert payload["resolved_attachment_ids"][0].startswith("att_")
    assert payload["newly_registered_attachment_ids"] == payload["resolved_attachment_ids"]
    mock_send.assert_awaited_once()
    mock_send_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_message_accepts_register_attachment_ids_across_task_boundaries(tmp_path: Path) -> None:
    """matrix_message should accept attachment IDs registered by a prior tool call in another task."""
    matrix_tool = MatrixMessageTools()
    attachment_tool = AttachmentTools()
    generated_file = tmp_path / "generated.txt"
    generated_file.write_text("artifact", encoding="utf-8")
    ctx = _make_context(storage_path=tmp_path)

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_latest_thread_event_id_if_needed",
            new=AsyncMock(return_value=ctx.thread_id),
        ),
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock(return_value="$evt")) as mock_send,
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
                    action="thread-reply",
                    message="hello",
                    attachment_ids=[attachment_id],
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
async def test_matrix_message_react_skips_interactive_processing() -> None:
    """React action should not touch interactive-question helpers."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    response = MagicMock(spec=nio.RoomSendResponse)
    response.event_id = "$react"
    ctx.client.room_send.return_value = response

    with (
        patch("mindroom.custom_tools.matrix_message.should_create_interactive_question") as mock_should_create,
        patch("mindroom.custom_tools.matrix_message.parse_and_format_interactive") as mock_parse,
        patch("mindroom.custom_tools.matrix_message.register_interactive_question") as mock_register,
        patch(
            "mindroom.custom_tools.matrix_message.add_reaction_buttons",
            new_callable=AsyncMock,
        ) as mock_add_reactions,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="react", message="🔥", target="$target"))

    assert payload["status"] == "ok"
    mock_should_create.assert_not_called()
    mock_parse.assert_not_called()
    mock_register.assert_not_called()
    mock_add_reactions.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_edit_processes_interactive_blocks() -> None:
    """Edit action should format interactive content and register reactions on the target event."""
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
    formatted_text = parse_and_format_interactive(interactive_message, extract_mapping=False).formatted_text
    ctx.conversation_access.get_thread_history.return_value = thread_messages

    with (
        patch(
            "mindroom.custom_tools.matrix_message.edit_message",
            new=AsyncMock(return_value="$edit_evt"),
        ) as mock_edit,
        patch("mindroom.custom_tools.matrix_message.register_interactive_question") as mock_register,
        patch(
            "mindroom.custom_tools.matrix_message.add_reaction_buttons",
            new_callable=AsyncMock,
        ) as mock_add_reactions,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="edit", message=interactive_message, target="$target"))

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$edit_evt"
    assert mock_edit.await_args.args[4] == formatted_text
    assert mock_edit.await_args.args[3]["body"] == formatted_text
    mock_register.assert_called_once_with(
        "$target",
        ctx.room_id,
        ctx.thread_id,
        {
            "✅": "approve",
            "1": "approve",
            "❌": "reject",
            "2": "reject",
        },
        ctx.agent_name,
    )
    mock_add_reactions.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$target",
        [
            {"emoji": "✅", "label": "Approve", "value": "approve"},
            {"emoji": "❌", "label": "Reject", "value": "reject"},
        ],
    )


@pytest.mark.asyncio
async def test_matrix_message_edit_plain_text_clears_existing_interactive_question() -> None:
    """Editing away an interactive block should clear the tracked question."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    thread_messages = [
        make_visible_message(event_id="$latest", timestamp=1, sender="@alice:localhost", body="latest"),
    ]
    ctx.conversation_access.get_thread_history.return_value = thread_messages
    interactive.register_interactive_question(
        "$target",
        ctx.room_id,
        ctx.thread_id,
        {"✅": "approve", "1": "approve"},
        ctx.agent_name,
    )

    with (
        patch(
            "mindroom.custom_tools.matrix_message.edit_message",
            new=AsyncMock(return_value="$edit_evt"),
        ),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="edit", message="updated text", target="$target"))

    assert payload["status"] == "ok"
    assert "$target" not in interactive._active_questions


@pytest.mark.asyncio
async def test_matrix_message_edit_re_registers_interactive_question() -> None:
    """Interactive edits should reformat the message and replace the question mapping."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    thread_messages = [
        make_visible_message(event_id="$latest", timestamp=1, sender="@alice:localhost", body="latest"),
    ]
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
    formatted_text = parse_and_format_interactive(interactive_message, extract_mapping=False).formatted_text
    ctx.conversation_access.get_thread_history.return_value = thread_messages

    with (
        patch(
            "mindroom.custom_tools.matrix_message.edit_message",
            new=AsyncMock(return_value="$edit_evt"),
        ) as mock_edit,
        patch("mindroom.custom_tools.matrix_message.clear_interactive_question") as mock_clear,
        patch("mindroom.custom_tools.matrix_message.register_interactive_question") as mock_register,
        patch(
            "mindroom.custom_tools.matrix_message.add_reaction_buttons",
            new_callable=AsyncMock,
        ) as mock_add_reactions,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="edit", message=interactive_message, target="$target"))

    assert payload["status"] == "ok"
    mock_clear.assert_called_once_with("$target")
    assert mock_edit.await_args.args[4] == formatted_text
    mock_register.assert_called_once_with(
        "$target",
        ctx.room_id,
        ctx.thread_id,
        {
            "✅": "approve",
            "1": "approve",
            "❌": "reject",
            "2": "reject",
        },
        ctx.agent_name,
    )
    mock_add_reactions.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        "$target",
        [
            {"emoji": "✅", "label": "Approve", "value": "approve"},
            {"emoji": "❌", "label": "Reject", "value": "reject"},
        ],
    )


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
    ctx.conversation_access.get_thread_history.return_value = thread_messages

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read", limit=999))

    assert payload["status"] == "ok"
    assert payload["limit"] == MatrixMessageTools._MAX_READ_LIMIT
    assert len(payload["messages"]) == MatrixMessageTools._MAX_READ_LIMIT
    assert "edit_options" in payload
    ctx.conversation_access.get_thread_history.assert_awaited_once_with(ctx.room_id, ctx.thread_id)


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
    ctx.conversation_access.get_thread_history.return_value = thread_messages

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="read"))

    assert payload["status"] == "ok"
    assert payload["action"] == "read"
    assert payload["thread_id"] == ctx.thread_id
    assert payload["edit_options"][0]["event_id"] == "$two"
    assert payload["edit_options"][0]["can_edit"] is True
    assert payload["edit_options"][0]["edit_action"] == {"action": "edit", "target": "$two"}
    assert payload["edit_options"][1]["event_id"] == "$one"
    assert payload["edit_options"][1]["can_edit"] is False
    assert "edit_action" not in payload["edit_options"][1]


@pytest.mark.asyncio
async def test_matrix_message_thread_list_requires_thread_context_or_target() -> None:
    """thread-list should fail when no thread can be resolved."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="thread-list"))

    assert payload["status"] == "error"
    assert payload["action"] == "thread-list"
    assert "thread_id is required" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_thread_list_returns_thread_messages() -> None:
    """thread-list should return thread messages and edit options."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    ctx.client.user_id = "@mindroom_general:localhost"
    thread_messages = [
        make_visible_message(event_id="$one", timestamp=1, sender="@mindroom_general:localhost", body="first"),
        make_visible_message(event_id="$two", timestamp=2, sender="@alice:localhost", body="second"),
    ]
    ctx.conversation_access.get_thread_history.return_value = thread_messages

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(
                action="thread-list",
                thread_id="$thread-other:localhost",
                limit=1,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "thread-list"
    assert payload["thread_id"] == "$thread-other:localhost"
    assert payload["messages"] == [thread_messages[-1].to_dict()]
    assert payload["edit_options"][0]["event_id"] == "$two"
    ctx.conversation_access.get_thread_history.assert_awaited_once_with(ctx.room_id, "$thread-other:localhost")


@pytest.mark.asyncio
async def test_matrix_message_thread_list_preserves_notice_messages() -> None:
    """thread-list should surface notice msgtypes unchanged."""
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
    ctx.conversation_access.get_thread_history.return_value = thread_messages

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(
                action="thread-list",
                thread_id="$thread-other:localhost",
                limit=2,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["messages"] == [message.to_dict() for message in thread_messages]
    assert payload["messages"][1]["msgtype"] == "m.notice"
    ctx.conversation_access.get_thread_history.assert_awaited_once_with(ctx.room_id, "$thread-other:localhost")


@pytest.mark.asyncio
async def test_matrix_message_room_threads_returns_paginated_thread_roots() -> None:
    """room-threads should serialize thread roots and forward page tokens."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    page_marker = "page_1"
    next_page = "page_2"
    thread_root = _make_room_thread_root(
        event_id="$thread-root",
        sender="@alice:localhost",
        timestamp=1234,
        body="Root message body",
        reply_count=4,
    )

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=([thread_root], next_page)),
        ) as mock_get_page,
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(return_value={"body": "Resolved root message body"}),
        ) as mock_extract,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(
                action="room-threads",
                limit=7,
                page_token=page_marker,
            ),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "room-threads"
    assert payload["room_id"] == ctx.room_id
    assert payload["count"] == 1
    assert payload["threads"] == [
        {
            "thread_id": "$thread-root",
            "sender": "@alice:localhost",
            "timestamp": 1234,
            "body_preview": "Resolved root message body",
            "reply_count": 4,
        },
    ]
    assert payload["next_token"] == next_page
    assert payload["has_more"] is True
    mock_get_page.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        limit=7,
        page_token=page_marker,
    )
    mock_extract.assert_awaited_once_with(thread_root, ctx.client)


@pytest.mark.asyncio
async def test_matrix_message_room_threads_includes_latest_activity_ts() -> None:
    """room-threads should expose latest activity separately from root creation time."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    thread_root = _make_room_thread_root(
        event_id="$thread-root",
        sender="@alice:localhost",
        timestamp=1234,
        body="Root message body",
        reply_count=4,
    )
    thread_root.source["unsigned"] = {
        "m.relations": {
            "m.thread": {
                "count": 4,
                "latest_event": {
                    "event_id": "$thread-reply",
                    "origin_server_ts": 5678,
                },
            },
        },
    }

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=([thread_root], None)),
        ),
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(return_value={"body": "Resolved root message body"}),
        ),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads", limit=1))

    assert payload["status"] == "ok"
    assert payload["threads"] == [
        {
            "thread_id": "$thread-root",
            "sender": "@alice:localhost",
            "timestamp": 1234,
            "latest_activity_ts": 5678,
            "body_preview": "Resolved root message body",
            "reply_count": 4,
        },
    ]


@pytest.mark.asyncio
async def test_matrix_message_room_threads_uses_bundled_replacement_preview_for_text_root() -> None:
    """room-threads should prefer bundled replacement bodies for text roots."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    thread_root = _make_room_thread_root(
        event_id="$thread-root",
        sender="@alice:localhost",
        timestamp=1234,
        body="Thinking...",
        reply_count=4,
    )
    thread_root.source["unsigned"] = {
        "m.relations": {
            "m.thread": {"count": 4},
            "m.replace": _make_bundled_replacement(
                event_id="$thread-root",
                body="Final root message",
                msgtype="m.text",
            ),
        },
    }

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=([thread_root], None)),
        ) as mock_get_page,
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(),
        ) as mock_extract,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads", limit=1))

    assert payload["status"] == "ok"
    assert payload["threads"] == [
        {
            "thread_id": "$thread-root",
            "sender": "@alice:localhost",
            "timestamp": 1234,
            "body_preview": "Final root message",
            "reply_count": 4,
        },
    ]
    mock_get_page.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        limit=1,
        page_token=None,
    )
    mock_extract.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle_key", ["event", "latest_event"])
async def test_matrix_message_room_threads_uses_nested_bundled_replacement_preview_for_notice_root(
    bundle_key: str,
) -> None:
    """room-threads should read notice previews from nested bundled replacement events."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    thread_root = nio.RoomMessageNotice.from_dict(
        {
            "event_id": "$thread-notice",
            "sender": "@alice:localhost",
            "origin_server_ts": 1234,
            "content": {"msgtype": "m.notice", "body": "Thinking..."},
        },
    )
    thread_root.source["unsigned"] = {
        "m.relations": {
            "m.thread": {"count": 2},
            "m.replace": _make_bundled_replacement(
                event_id="$thread-notice",
                body="Compacted 12 messages",
                msgtype="m.notice",
                bundle_key=bundle_key,
            ),
        },
    }

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=([thread_root], None)),
        ) as mock_get_page,
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(),
        ) as mock_extract,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads", limit=1))

    assert payload["status"] == "ok"
    assert payload["threads"] == [
        {
            "thread_id": "$thread-notice",
            "sender": "@alice:localhost",
            "timestamp": 1234,
            "body_preview": "Compacted 12 messages",
            "reply_count": 2,
        },
    ]
    mock_get_page.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        limit=1,
        page_token=None,
    )
    mock_extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_room_threads_resolves_notice_root_without_replacement() -> None:
    """room-threads should resolve notice roots through the canonical message path."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    thread_root = nio.RoomMessageNotice.from_dict(
        {
            "event_id": "$thread-notice",
            "sender": "@alice:localhost",
            "origin_server_ts": 1234,
            "content": {"msgtype": "m.notice", "body": "Thinking..."},
        },
    )
    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=([thread_root], None)),
        ) as mock_get_page,
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(return_value={"body": "Resolved notice body"}),
        ) as mock_extract,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads", limit=1))

    assert payload["status"] == "ok"
    assert payload["threads"] == [
        {
            "thread_id": "$thread-notice",
            "sender": "@alice:localhost",
            "timestamp": 1234,
            "body_preview": "Resolved notice body",
            "reply_count": 0,
        },
    ]
    mock_get_page.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        limit=1,
        page_token=None,
    )
    mock_extract.assert_awaited_once_with(thread_root, ctx.client)


@pytest.mark.asyncio
async def test_matrix_message_room_threads_skips_malformed_roots() -> None:
    """room-threads should skip malformed roots instead of crashing the whole action."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    thread_root = _make_room_thread_root(
        event_id="$thread-root",
        sender="@alice:localhost",
        timestamp=1234,
        body="Root message body",
        reply_count=4,
    )

    class MalformedThreadRoot:
        source: ClassVar[dict[str, object]] = {
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "broken"},
        }

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=([MalformedThreadRoot(), thread_root], None)),
        ),
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(return_value={"body": "Resolved root message body"}),
        ) as mock_extract,
        patch("mindroom.custom_tools.matrix_message.logger.warning") as mock_warning,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads"))

    assert payload["status"] == "ok"
    assert payload["count"] == 1
    assert payload["threads"] == [
        {
            "thread_id": "$thread-root",
            "sender": "@alice:localhost",
            "timestamp": 1234,
            "body_preview": "Resolved root message body",
            "reply_count": 4,
        },
    ]
    mock_extract.assert_awaited_once_with(thread_root, ctx.client)
    mock_warning.assert_called_once()


@pytest.mark.asyncio
async def test_matrix_message_room_threads_has_more_false_without_next_token() -> None:
    """room-threads should derive has_more solely from next_token."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    thread_roots = [
        _make_room_thread_root(
            event_id="$thread-one",
            sender="@alice:localhost",
            timestamp=1,
            body="First thread",
        ),
        _make_room_thread_root(
            event_id="$thread-two",
            sender="@bob:localhost",
            timestamp=2,
            body="Second thread",
        ),
    ]

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=(thread_roots, None)),
        ),
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(side_effect=[{"body": "First thread"}, {"body": "Second thread"}]),
        ),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads", limit=2))

    assert payload["status"] == "ok"
    assert payload["count"] == 2
    assert payload["next_token"] is None
    assert payload["has_more"] is False


@pytest.mark.asyncio
async def test_matrix_message_room_threads_empty_room() -> None:
    """room-threads should return an empty success payload for rooms without threads."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=([], None)),
        ) as mock_get_page,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads"))

    assert payload == {
        "action": "room-threads",
        "count": 0,
        "has_more": False,
        "next_token": None,
        "room_id": ctx.room_id,
        "status": "ok",
        "threads": [],
        "tool": "matrix_message",
    }
    mock_get_page.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        limit=MatrixMessageTools._DEFAULT_READ_LIMIT,
        page_token=None,
    )


@pytest.mark.asyncio
async def test_matrix_message_room_threads_returns_structured_api_error() -> None:
    """room-threads should surface Matrix API failures without a fallback scan."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    stale_page = "stale"

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(
                side_effect=RoomThreadsPageError(
                    response="RoomThreadsError: M_INVALID_PARAM Unknown or invalid from token",
                    errcode="M_INVALID_PARAM",
                ),
            ),
        ),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads", page_token=stale_page))

    assert payload["status"] == "error"
    assert payload["action"] == "room-threads"
    assert payload["room_id"] == ctx.room_id
    assert payload["response"] == "RoomThreadsError: M_INVALID_PARAM Unknown or invalid from token"
    assert payload["errcode"] == "M_INVALID_PARAM"


@pytest.mark.asyncio
async def test_matrix_message_room_threads_preserves_rate_limit_details() -> None:
    """room-threads should preserve retry metadata from Matrix rate limits."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(
                side_effect=RoomThreadsPageError(
                    response="RoomThreadsError: M_LIMIT_EXCEEDED Too many requests - retry after 1500ms",
                    errcode="M_LIMIT_EXCEEDED",
                    retry_after_ms=1500,
                ),
            ),
        ),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads"))

    assert payload["status"] == "error"
    assert payload["action"] == "room-threads"
    assert payload["room_id"] == ctx.room_id
    assert payload["response"] == "RoomThreadsError: M_LIMIT_EXCEEDED Too many requests - retry after 1500ms"
    assert payload["errcode"] == "M_LIMIT_EXCEEDED"
    assert payload["retry_after_ms"] == 1500


@pytest.mark.asyncio
async def test_matrix_message_room_threads_returns_structured_transport_error() -> None:
    """room-threads should convert transport exceptions into structured tool errors."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(
                side_effect=RoomThreadsPageError(
                    response="TimeoutError: request timed out",
                ),
            ),
        ),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads"))

    assert payload["status"] == "error"
    assert payload["action"] == "room-threads"
    assert payload["room_id"] == ctx.room_id
    assert payload["response"] == "TimeoutError: request timed out"
    assert "errcode" not in payload
    assert "retry_after_ms" not in payload


@pytest.mark.asyncio
async def test_matrix_message_room_threads_clamps_limit() -> None:
    """room-threads should reuse the existing read-limit clamp."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=([], None)),
        ) as mock_get_page,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads", limit=999))

    assert payload["status"] == "ok"
    mock_get_page.assert_awaited_once_with(
        ctx.client,
        ctx.room_id,
        limit=MatrixMessageTools._MAX_READ_LIMIT,
        page_token=None,
    )


@pytest.mark.asyncio
async def test_matrix_message_room_threads_encrypted_preview_is_redacted() -> None:
    """Encrypted thread roots should use the explicit encrypted preview."""
    tool = MatrixMessageTools()
    ctx = _make_context()
    encrypted_root = _make_room_thread_root(
        event_id="$thread-encrypted",
        sender="@alice:localhost",
        timestamp=1234,
        encrypted=True,
    )

    with (
        patch(
            "mindroom.custom_tools.matrix_message.get_room_threads_page",
            new=AsyncMock(return_value=([encrypted_root], None)),
        ),
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(),
        ) as mock_extract,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="room-threads"))

    assert payload["status"] == "ok"
    assert payload["threads"][0]["body_preview"] == "[encrypted]"
    assert payload["threads"][0]["reply_count"] == 0
    mock_extract.assert_not_awaited()


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
async def test_matrix_message_read_room_includes_notice_events() -> None:
    """Room reads should keep both text and notice events."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id=None)
    response = nio.RoomMessagesResponse.from_dict(
        {
            "chunk": [
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
    extracted_messages = {
        "$text": {"event_id": "$text", "body": "hello"},
        "$notice": {"event_id": "$notice", "body": "Compacted 12 messages", "msgtype": "m.notice"},
    }

    async def _extract(event: nio.Event, _client: nio.AsyncClient) -> dict[str, object]:
        return extracted_messages[event.event_id]

    with (
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(side_effect=_extract),
        ) as mock_extract,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="read", limit=5))

    assert payload["status"] == "ok"
    assert payload["messages"] == [
        {"event_id": "$text", "body": "hello"},
        {"event_id": "$notice", "body": "Compacted 12 messages", "msgtype": "m.notice"},
    ]
    assert mock_extract.await_count == 2


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

    with (
        patch(
            "mindroom.custom_tools.matrix_message.extract_and_resolve_message",
            new=AsyncMock(return_value={"event_id": "$evt", "body": "hello from room"}),
        ) as mock_extract,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="read", thread_id="room", limit=5))

    assert payload["status"] == "ok"
    assert payload["action"] == "read"
    assert payload["limit"] == 5
    assert payload["messages"] == [{"event_id": "$evt", "body": "hello from room"}]
    assert "thread_id" not in payload
    ctx.client.room_messages.assert_awaited_once_with(
        ctx.room_id,
        limit=5,
        direction=nio.MessageDirection.back,
        message_filter={"types": ["m.room.message"]},
    )
    mock_extract.assert_awaited_once()
    ctx.conversation_access.get_thread_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_read_explicit_thread_id_still_reads_that_thread() -> None:
    """Explicit thread IDs should win over runtime thread fallback for read."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    thread_messages = [
        make_visible_message(event_id="$one", timestamp=1, body="first"),
        make_visible_message(event_id="$two", timestamp=2, body="second"),
    ]
    ctx.conversation_access.get_thread_history.return_value = thread_messages

    with tool_runtime_context(ctx):
        payload = json.loads(
            await tool.matrix_message(action="read", thread_id="$thread-other:localhost", limit=1),
        )

    assert payload["status"] == "ok"
    assert payload["action"] == "read"
    assert payload["thread_id"] == "$thread-other:localhost"
    assert payload["messages"] == [thread_messages[-1].to_dict()]
    ctx.conversation_access.get_thread_history.assert_awaited_once_with(ctx.room_id, "$thread-other:localhost")
    ctx.client.room_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_edit_happy_path() -> None:
    """Edit should update an existing message by target event ID."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")
    thread_messages = [
        make_visible_message(event_id="$one", timestamp=1, sender="@alice:localhost", body="first"),
        make_visible_message(event_id="$latest", timestamp=2, sender="@alice:localhost", body="latest"),
    ]
    ctx.conversation_access.get_thread_history.return_value = thread_messages

    with (
        patch(
            "mindroom.custom_tools.matrix_message.edit_message",
            new=AsyncMock(return_value="$edit_evt"),
        ) as mock_edit,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="edit", message="updated text", target="$target"))

    assert payload["status"] == "ok"
    assert payload["action"] == "edit"
    assert payload["target"] == "$target"
    assert payload["event_id"] == "$edit_evt"
    mock_edit.assert_awaited_once()
    args = mock_edit.await_args.args
    assert args[1] == ctx.room_id
    assert args[2] == "$target"
    assert args[4] == "updated text"
    assert args[3]["body"] == "updated text"
    assert args[3]["m.relates_to"]["rel_type"] == "m.thread"
    assert args[3]["m.relates_to"]["event_id"] == "$ctx-thread:localhost"
    assert args[3]["m.relates_to"]["is_falling_back"] is True
    assert args[3]["m.relates_to"]["m.in_reply_to"]["event_id"] == "$latest"
    ctx.conversation_access.get_thread_history.assert_awaited_once_with(ctx.room_id, "$ctx-thread:localhost")


@pytest.mark.asyncio
async def test_matrix_message_edit_requires_target() -> None:
    """Edit action should require target event ID."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="edit", message="updated text"))

    assert payload["status"] == "error"
    assert "target event_id is required for edit" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_edit_requires_message() -> None:
    """Edit action should require non-empty replacement text."""
    tool = MatrixMessageTools()
    ctx = _make_context()

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="edit", target="$target", message="  "))

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
    assert "At least one of message, attachment_ids, or attachment_file_paths" in payload["message"]


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
                attachment_ids=["att_upload"],
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
                attachment_ids=["output.txt"],
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
                attachment_ids=["att_over"] * 6,
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
async def test_matrix_message_reply_room_sentinel_disables_context_thread_fallback() -> None:
    """thread_id='room' should disable reply thread inheritance and keep reply invalid."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$ctx-thread:localhost")

    with (
        patch("mindroom.custom_tools.matrix_message.send_message", new=AsyncMock()) as mock_send,
        tool_runtime_context(ctx),
    ):
        payload = json.loads(await tool.matrix_message(action="reply", thread_id="room", message="hello"))

    assert payload["status"] == "error"
    assert payload["action"] == "reply"
    assert "thread_id is required" in payload["message"]
    mock_send.assert_not_awaited()


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
                attachment_ids=["att_weighted"],
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
async def test_matrix_message_context_room_sentinel_normalizes_to_room_level() -> None:
    """Context should not leak the room sentinel as a fake thread ID."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$thread-root:localhost", reply_to_event_id="$event:localhost")

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.matrix_message(action="context", thread_id="room"))

    assert payload["status"] == "ok"
    assert payload["action"] == "context"
    assert payload["room_id"] == ctx.room_id
    assert payload["thread_id"] is None
    assert payload["reply_to_event_id"] == "$event:localhost"


@pytest.mark.asyncio
async def test_matrix_message_cross_room_reply_does_not_inherit_context_thread() -> None:
    """Authorized cross-room reply should not inherit the origin room's thread."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$origin-thread:localhost")

    with (
        patch("mindroom.custom_tools.matrix_message.room_access_allowed", return_value=True),
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


@pytest.mark.asyncio
async def test_matrix_message_cross_room_context_does_not_leak_thread() -> None:
    """Authorized cross-room context should not return the origin room's thread."""
    tool = MatrixMessageTools()
    ctx = _make_context(thread_id="$origin-thread:localhost", reply_to_event_id="$evt:localhost")

    with (
        patch("mindroom.custom_tools.matrix_message.room_access_allowed", return_value=True),
        tool_runtime_context(ctx),
    ):
        payload = json.loads(
            await tool.matrix_message(action="context", room_id="!other:localhost"),
        )

    assert payload["status"] == "ok"
    assert payload["thread_id"] is None
    assert payload["reply_to_event_id"] is None
