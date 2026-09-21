"""Model-visible file entrypoint enforces sources and existing attachment authority."""

import json
import stat
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.attachments import load_attachment, register_local_attachment
from mindroom.config.models import ModelConfig
from mindroom.custom_tools.attachments import AttachmentTools
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.tool_system import media_attachments
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.test_attachments_tool import _tool_context
from tests.test_media_delivery import image_bytes


@pytest.mark.asyncio
async def test_view_file_path_delivers_image_in_one_call(tmp_path: Path) -> None:
    """View file path delivers image in one call."""
    context = _tool_context(tmp_path, process_env={"MINDROOM_EXECUTION_MODE": "off"})
    (tmp_path / "result.png").write_bytes(image_bytes())
    tools = AttachmentTools(tool_output_workspace_root=tmp_path)
    with tool_runtime_context(context):
        result = await tools.view_file(path="result.png")
    assert result.images
    assert result.images[0].content == image_bytes()
    receipt = json.loads(result.content)
    assert receipt["view_status"] == "ready"
    assert receipt["path"] == "result.png"
    assert receipt["attachment_id"].startswith("att_")
    assert not context.client.room_send.called


@pytest.mark.asyncio
async def test_view_file_attachment_delivers_authorized_image(tmp_path: Path) -> None:
    """View file attachment delivers authorized image."""
    path = tmp_path / "image.png"
    path.write_bytes(image_bytes())
    record = register_local_attachment(
        tmp_path,
        path,
        kind="image",
        mime_type="image/png",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
    )
    assert record
    context = _tool_context(tmp_path, attachment_ids=(record.attachment_id,))
    with tool_runtime_context(context):
        result = await AttachmentTools().view_file(attachment_id=record.attachment_id)
    assert result.images
    assert result.images[0].content == image_bytes()
    assert json.loads(result.content)["attachment_id"] == record.attachment_id


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{}, {"path": "x", "attachment_id": "att_x"}, {"path": ""}, {"attachment_id": ""}])
async def test_view_file_requires_exactly_one_valid_source(tmp_path: Path, kwargs: dict[str, str]) -> None:
    """View file requires exactly one valid source."""
    with tool_runtime_context(_tool_context(tmp_path)):
        result = await AttachmentTools().view_file(**kwargs)
    assert not result.images
    assert json.loads(result.content)["view_status"] == "error"


@pytest.mark.asyncio
async def test_view_file_attachment_denies_unknown_id(tmp_path: Path) -> None:
    """View file attachment denies unknown id."""
    with tool_runtime_context(_tool_context(tmp_path)):
        result = await AttachmentTools().view_file(attachment_id="att_other")
    assert not result.images
    assert "context" in result.content


@pytest.mark.asyncio
async def test_view_file_attachment_denies_wrong_room_even_when_listed(tmp_path: Path) -> None:
    """View file attachment denies wrong room even when listed."""
    path = tmp_path / "image.png"
    path.write_bytes(image_bytes())
    record = register_local_attachment(
        tmp_path,
        path,
        kind="image",
        room_id="!elsewhere:localhost",
        thread_id="$thread:localhost",
    )
    assert record
    context = _tool_context(tmp_path, attachment_ids=(record.attachment_id,))
    with tool_runtime_context(context):
        result = await AttachmentTools().view_file(attachment_id=record.attachment_id)
    assert not result.images
    assert json.loads(result.content)["view_status"] == "error"


@pytest.mark.asyncio
async def test_viewed_path_handle_can_be_reopened(tmp_path: Path) -> None:
    """Viewed path handle can be reopened."""
    context = _tool_context(tmp_path, process_env={"MINDROOM_EXECUTION_MODE": "off"})
    path = tmp_path / "image.png"
    path.write_bytes(image_bytes())
    tools = AttachmentTools(tool_output_workspace_root=tmp_path)
    with tool_runtime_context(context):
        first = await tools.view_file(path="image.png")
        path.unlink()
        reopened = await tools.view_file(attachment_id=json.loads(first.content)["attachment_id"])
    assert reopened.images
    assert reopened.images[0].content == image_bytes()


@pytest.mark.asyncio
async def test_viewed_image_can_be_saved_and_explicitly_shared(tmp_path: Path) -> None:
    """Viewing retains bytes for later save/send without itself sending anything."""
    context = _tool_context(tmp_path, process_env={"MINDROOM_EXECUTION_MODE": "off"})
    (tmp_path / "image.png").write_bytes(image_bytes())
    tools = AttachmentTools(tool_output_workspace_root=tmp_path)
    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$image")) as send,
    ):
        viewed = await tools.view_file(path="image.png")
        handle = json.loads(viewed.content)["attachment_id"]
        saved = json.loads(await tools.get_attachment(attachment_id=handle, mindroom_output_path="copy.png"))
        assert saved["status"] == "ok"
        assert (tmp_path / "copy.png").read_bytes() == image_bytes()
        send.assert_not_awaited()
        assert not context.client.room_send.called
        shared = json.loads(await MatrixMessageTools().matrix_message(attachments=[handle]))
        assert shared["status"] == "ok"
        assert shared["resolved_attachment_ids"] == [handle]
        send.assert_awaited_once()


@pytest.mark.asyncio
async def test_unsupported_adapter_retains_artifact_and_reports_limitation(tmp_path: Path) -> None:
    """An adapter that omits images must never report a successful view."""
    context = replace(
        _tool_context(tmp_path, process_env={"MINDROOM_EXECUTION_MODE": "off"}),
        active_model_name="text_only",
    )
    context.config.models["text_only"] = ModelConfig(provider="cerebras", id="test-model")
    path = tmp_path / "image.png"
    path.write_bytes(image_bytes())
    with tool_runtime_context(context):
        viewed = await AttachmentTools(tool_output_workspace_root=tmp_path).view_file(path="image.png")
    receipt = json.loads(viewed.content)
    assert receipt["view_status"] == "unsupported"
    assert not viewed.images
    retained = load_attachment(tmp_path, receipt["attachment_id"])
    assert retained is not None
    assert retained.local_path.read_bytes() == path.read_bytes() == image_bytes()
    assert not context.client.room_send.called


@pytest.mark.asyncio
async def test_retention_failure_removes_unregistered_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed registration does not leave untracked copies in storage."""
    context = _tool_context(tmp_path, process_env={"MINDROOM_EXECUTION_MODE": "off"})
    (tmp_path / "image.png").write_bytes(image_bytes())
    monkeypatch.setattr(media_attachments, "register_local_attachment", lambda *_args, **_kwargs: None)
    with tool_runtime_context(context):
        result = await AttachmentTools(tool_output_workspace_root=tmp_path).view_file(path="image.png")
    assert result.images
    assert "attachment_warning" in json.loads(result.content)
    assert not list((tmp_path / "incoming_media").glob("att_*"))


@pytest.mark.asyncio
async def test_retained_copy_is_created_with_private_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retained copy must be private before its first byte is written."""
    context = _tool_context(tmp_path, process_env={"MINDROOM_EXECUTION_MODE": "off"})
    (tmp_path / "image.png").write_bytes(image_bytes())
    original_chmod = Path.chmod

    def check_chmod(path: Path, mode: int, **kwargs: object) -> None:
        if path.name.startswith("att_"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        original_chmod(path, mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", check_chmod)
    with tool_runtime_context(context):
        result = await AttachmentTools(tool_output_workspace_root=tmp_path).view_file(path="image.png")
    assert result.images
    saved = list((tmp_path / "incoming_media").glob("att_*"))
    assert len(saved) == 1
    assert stat.S_IMODE(saved[0].stat().st_mode) == 0o600
