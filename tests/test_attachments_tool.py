"""Tests for the model-agnostic attachments toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.attachments import register_local_attachment
from mindroom.custom_tools.attachments import AttachmentTools, send_context_attachments
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context, tool_runtime_context

if TYPE_CHECKING:
    from pathlib import Path


def _tool_context(tmp_path: Path, *, attachment_ids: tuple[str, ...] = ()) -> ToolRuntimeContext:
    client = MagicMock()
    client.rooms = {"!room:localhost": MagicMock()}
    return ToolRuntimeContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=client,
        config=MagicMock(),
        storage_path=tmp_path,
        attachment_ids=attachment_ids,
    )


def test_attachments_tool_hides_send_method_from_exposed_tools() -> None:
    """Attachments tool should expose only list/get/register operations."""
    tool = AttachmentTools()
    exposed = {method.__name__ for method in tool.tools}
    assert exposed == {"list_attachments", "get_attachment", "register_attachment"}
    assert not hasattr(tool, "send_attachments")


@pytest.mark.asyncio
async def test_attachments_tool_lists_context_attachments(tmp_path: Path) -> None:
    """Tool should list attachment metadata scoped to current runtime context."""
    tool = AttachmentTools()
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("hello", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_sample",
    )
    assert attachment is not None

    with tool_runtime_context(_tool_context(tmp_path, attachment_ids=(attachment.attachment_id,))):
        payload = json.loads(await tool.list_attachments())

    assert payload["status"] == "ok"
    assert payload["tool"] == "attachments"
    assert payload["attachment_ids"] == ["att_sample"]
    assert payload["attachments"][0]["attachment_id"] == "att_sample"
    assert payload["attachments"][0]["available"] is True
    assert payload["attachments"][0]["local_path"] == str(sample_file.resolve())


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_returns_local_path(tmp_path: Path) -> None:
    """Tool should resolve one context attachment by ID with local_path included."""
    tool = AttachmentTools()
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("hello", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_sample",
    )
    assert attachment is not None

    with tool_runtime_context(_tool_context(tmp_path, attachment_ids=(attachment.attachment_id,))):
        payload = json.loads(await tool.get_attachment("att_sample"))

    assert payload["status"] == "ok"
    assert payload["tool"] == "attachments"
    assert payload["attachment_id"] == "att_sample"
    assert payload["attachment"]["attachment_id"] == "att_sample"
    assert payload["attachment"]["local_path"] == str(sample_file.resolve())


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_rejects_out_of_context_ids(tmp_path: Path) -> None:
    """Tool should reject attachment IDs not present in runtime context."""
    tool = AttachmentTools()
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("hello", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_sample",
    )
    assert attachment is not None

    with tool_runtime_context(_tool_context(tmp_path, attachment_ids=())):
        payload = json.loads(await tool.get_attachment("att_sample"))

    assert payload["status"] == "error"
    assert payload["tool"] == "attachments"
    assert "not available in this context" in payload["message"]


@pytest.mark.asyncio
async def test_send_context_attachments_sends_attachment_ids(tmp_path: Path) -> None:
    """Helper should resolve attachment IDs and upload them to Matrix."""
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_upload",
    )
    assert attachment is not None

    context = _tool_context(tmp_path, attachment_ids=("att_upload",))
    with patch(
        "mindroom.custom_tools.attachments.send_file_message",
        new=AsyncMock(return_value="$file_evt"),
    ) as mocked:
        result, send_error = await send_context_attachments(
            context,
            attachment_ids=["att_upload"],
            attachment_file_paths=[],
        )

    assert send_error is None
    assert result is not None
    assert result.attachment_event_ids == ["$file_evt"]
    assert result.resolved_attachment_ids == ["att_upload"]
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_context_attachments_rejects_non_attachment_id_references(tmp_path: Path) -> None:
    """Helper should require att_* values for attachment_ids."""
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")

    context = _tool_context(tmp_path)
    with patch(
        "mindroom.custom_tools.attachments.send_file_message",
        new=AsyncMock(return_value="$file_evt"),
    ) as mocked:
        result, send_error = await send_context_attachments(
            context,
            attachment_ids=[str(sample_file)],
            attachment_file_paths=[],
        )

    assert result is None
    assert send_error is not None
    assert "must be context attachment IDs" in send_error
    mocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_context_attachments_rejects_non_att_prefix_references(tmp_path: Path) -> None:
    """Helper should reject attachment_ids values without the att_ prefix."""
    context = _tool_context(tmp_path)
    with patch(
        "mindroom.custom_tools.attachments.send_file_message",
        new=AsyncMock(return_value="$file_evt"),
    ) as mocked:
        result, send_error = await send_context_attachments(
            context,
            attachment_ids=["upload.txt"],
            attachment_file_paths=[],
        )

    assert result is None
    assert send_error is not None
    assert "must be context attachment IDs" in send_error
    mocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachments_tool_requires_context() -> None:
    """Tool should return an explicit error when runtime context is unavailable."""
    tool = AttachmentTools()
    with tool_runtime_context(None):
        payload = json.loads(await tool.list_attachments())

    assert payload["status"] == "error"
    assert payload["tool"] == "attachments"
    assert "context" in payload["message"]


@pytest.mark.asyncio
async def test_send_context_attachments_cross_room_send_does_not_inherit_source_thread(tmp_path: Path) -> None:
    """Cross-room sends without explicit thread_id should not inherit source thread."""
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_cross",
    )
    assert attachment is not None

    ctx = _tool_context(tmp_path, attachment_ids=("att_cross",))
    assert ctx.thread_id is not None  # context has a thread
    # Add the target room so the join check passes
    ctx.client.rooms["!other:localhost"] = MagicMock()

    with patch(
        "mindroom.custom_tools.attachments.send_file_message",
        new=AsyncMock(return_value="$file_evt"),
    ) as mocked:
        result, send_error = await send_context_attachments(
            ctx,
            attachment_ids=["att_cross"],
            attachment_file_paths=[],
            room_id="!other:localhost",  # different room
            # thread_id intentionally omitted
        )

    assert send_error is None
    assert result is not None
    mocked.assert_awaited_once()
    call_kwargs = mocked.await_args.kwargs
    assert call_kwargs["thread_id"] is None  # must NOT inherit source thread


@pytest.mark.asyncio
async def test_send_context_attachments_rejects_send_to_unjoined_room(tmp_path: Path) -> None:
    """Helper should reject sending to a room the bot has not joined."""
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_unjoin",
    )
    assert attachment is not None

    ctx = _tool_context(tmp_path, attachment_ids=("att_unjoin",))
    # !other:localhost is NOT in ctx.client.rooms

    with patch(
        "mindroom.custom_tools.attachments.send_file_message",
        new=AsyncMock(return_value="$file_evt"),
    ) as mocked:
        result, send_error = await send_context_attachments(
            ctx,
            attachment_ids=["att_unjoin"],
            attachment_file_paths=[],
            room_id="!other:localhost",
        )

    assert result is not None
    assert send_error is not None
    assert "not joined" in send_error
    mocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachments_tool_registers_file_and_updates_runtime_context(tmp_path: Path) -> None:
    """Registering a file should make it available for send_context_attachments in the same context."""
    tool = AttachmentTools()
    generated_file = tmp_path / "generated.txt"
    generated_file.write_text("artifact", encoding="utf-8")
    ctx = _tool_context(tmp_path)

    with (
        tool_runtime_context(ctx),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        register_payload = json.loads(await tool.register_attachment(str(generated_file)))
        current_context = get_tool_runtime_context()
        assert current_context is not None
        attachment_id = register_payload["attachment_id"]
        send_result, send_error = await send_context_attachments(
            current_context,
            attachment_ids=[attachment_id],
            attachment_file_paths=[],
        )

    assert register_payload["status"] == "ok"
    assert register_payload["tool"] == "attachments"
    assert register_payload["attachment_id"].startswith("att_")
    assert register_payload["attachment"]["local_path"] == str(generated_file.resolve())
    assert attachment_id in current_context.attachment_ids
    assert send_error is None
    assert send_result is not None
    assert send_result.resolved_attachment_ids == [attachment_id]
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_context_attachments_cross_room_send_requires_authorization(tmp_path: Path) -> None:
    """Cross-room sends should reject unauthorized targets even when joined."""
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_authz",
    )
    assert attachment is not None

    ctx = _tool_context(tmp_path, attachment_ids=("att_authz",))
    ctx.client.rooms["!other:localhost"] = MagicMock()

    with (
        patch("mindroom.custom_tools.attachment_helpers.is_authorized_sender", return_value=False),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        result, send_error = await send_context_attachments(
            ctx,
            attachment_ids=["att_authz"],
            attachment_file_paths=[],
            room_id="!other:localhost",
        )

    assert result is not None
    assert send_error is not None
    assert "Not authorized" in send_error
    mocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_context_attachments_sends_local_file_paths_by_auto_registering(tmp_path: Path) -> None:
    """Helper should auto-register local file paths and send them in the same call."""
    generated_file = tmp_path / "generated.txt"
    generated_file.write_text("artifact", encoding="utf-8")
    ctx = _tool_context(tmp_path)

    with (
        tool_runtime_context(ctx),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        result, send_error = await send_context_attachments(
            ctx,
            attachment_ids=[],
            attachment_file_paths=[str(generated_file)],
        )
        current_context = get_tool_runtime_context()
        assert current_context is not None

    assert send_error is None
    assert result is not None
    assert result.resolved_attachment_ids[0].startswith("att_")
    assert result.newly_registered_attachment_ids == result.resolved_attachment_ids
    assert result.newly_registered_attachment_ids[0] in current_context.attachment_ids
    mocked.assert_awaited_once()


def test_tool_runtime_context_none_temporarily_clears_nested_scope(tmp_path: Path) -> None:
    """tool_runtime_context(None) should clear and then restore an outer context."""
    ctx = _tool_context(tmp_path, attachment_ids=("att_upload",))
    with tool_runtime_context(ctx):
        assert get_tool_runtime_context() is ctx
        with tool_runtime_context(None):
            assert get_tool_runtime_context() is None
        assert get_tool_runtime_context() is ctx
