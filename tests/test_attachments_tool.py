"""Tests for the model-agnostic attachments toolkit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.attachments import register_local_attachment
from mindroom.attachments_context import AttachmentToolContext, attachment_tool_context, get_attachment_tool_context
from mindroom.custom_tools.attachments import AttachmentTools

if TYPE_CHECKING:
    from pathlib import Path


def _tool_context(tmp_path: Path, *, attachment_ids: tuple[str, ...] = ()) -> AttachmentToolContext:
    return AttachmentToolContext(
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        storage_path=tmp_path,
        attachment_ids=attachment_ids,
    )


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

    with attachment_tool_context(_tool_context(tmp_path, attachment_ids=(attachment.attachment_id,))):
        payload = json.loads(await tool.list_attachments())

    assert payload["status"] == "ok"
    assert payload["tool"] == "attachments"
    assert payload["attachment_ids"] == ["att_sample"]
    assert payload["attachments"][0]["attachment_id"] == "att_sample"
    assert payload["attachments"][0]["available"] is True


@pytest.mark.asyncio
async def test_attachments_tool_sends_attachment_ids(tmp_path: Path) -> None:
    """Tool should resolve attachment IDs and upload them to Matrix."""
    tool = AttachmentTools()
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_upload",
    )
    assert attachment is not None

    with (
        attachment_tool_context(_tool_context(tmp_path, attachment_ids=("att_upload",))),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        payload = json.loads(await tool.send_attachments(attachments=["att_upload"]))

    assert payload["status"] == "ok"
    assert payload["tool"] == "attachments"
    assert payload["event_id"] == "$file_evt"
    assert payload["resolved_attachment_ids"] == ["att_upload"]
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_attachments_tool_rejects_local_paths_by_default(tmp_path: Path) -> None:
    """Tool should reject direct filesystem paths unless explicitly enabled."""
    tool = AttachmentTools()
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")

    with (
        attachment_tool_context(_tool_context(tmp_path)),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        payload = json.loads(await tool.send_attachments(attachments=[str(sample_file)]))

    assert payload["status"] == "error"
    assert payload["tool"] == "attachments"
    assert "Local file paths are disabled" in payload["message"]
    mocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachments_tool_rejects_local_paths_outside_storage_scope(tmp_path: Path) -> None:
    """Tool should reject local paths that resolve outside the context storage path."""
    tool = AttachmentTools()
    storage_scope = tmp_path / "storage_scope"
    storage_scope.mkdir(parents=True, exist_ok=True)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("payload", encoding="utf-8")

    with (
        attachment_tool_context(_tool_context(storage_scope)),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        payload = json.loads(await tool.send_attachments(attachments=[str(outside_file)], allow_local_paths=True))

    assert payload["status"] == "error"
    assert payload["tool"] == "attachments"
    assert "must be under storage path" in payload["message"]
    mocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachments_tool_requires_context() -> None:
    """Tool should return an explicit error when runtime context is unavailable."""
    tool = AttachmentTools()
    with attachment_tool_context(None):
        payload = json.loads(await tool.list_attachments())

    assert payload["status"] == "error"
    assert payload["tool"] == "attachments"
    assert "context" in payload["message"]


def test_attachment_context_none_temporarily_clears_nested_scope(tmp_path: Path) -> None:
    """attachment_tool_context(None) should clear and then restore an outer context."""
    ctx = _tool_context(tmp_path, attachment_ids=("att_upload",))
    with attachment_tool_context(ctx):
        assert get_attachment_tool_context() is ctx
        with attachment_tool_context(None):
            assert get_attachment_tool_context() is None
        assert get_attachment_tool_context() is ctx
