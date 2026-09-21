"""Model-visible file entrypoint enforces sources and existing attachment authority."""

import io
import json
import stat
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from agno.tools.function import ToolResult
from PIL import Image

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
@pytest.mark.parametrize("entrypoint", ["view_file", "get_attachment"])
async def test_view_file_attachment_delivers_authorized_image(tmp_path: Path, entrypoint: str) -> None:
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
        tools = AttachmentTools()
        result = (
            await tools.view_file(attachment_id=record.attachment_id)
            if entrypoint == "view_file"
            else await tools.get_attachment(record.attachment_id, view=True)
        )
    assert isinstance(result, ToolResult)
    assert result.images
    assert result.images[0].content == image_bytes()
    assert result.images[0].id.startswith("mindroom_viewed_")
    assert json.loads(result.content)["attachment_id"] == record.attachment_id


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["view_file", "get_attachment"])
@pytest.mark.parametrize("case", ["large", "animation", "corrupt", "pixels"])
async def test_attachment_image_views_share_preparation(
    tmp_path: Path,
    entrypoint: str,
    case: str,
) -> None:
    """Every attachment image entrypoint enforces decoding and disclosed transformations."""
    path = tmp_path / "image.png"
    if case == "animation":
        frames = [Image.new("RGB", (20, 20), color) for color in ("red", "blue")]
        with path.open("wb") as output:
            frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=100)
    elif case == "corrupt":
        path.write_bytes(b"\x89PNG\r\n\x1a\nnot a decodable image")
    else:
        path.write_bytes(image_bytes((3000, 1000) if case == "large" else (6400, 6400)))
    original = path.read_bytes()
    context = _tool_context(tmp_path)
    tools = AttachmentTools(tool_output_workspace_root=tmp_path)
    with tool_runtime_context(context):
        attachment_id = json.loads(await tools.register_attachment("image.png"))["attachment_id"]
        result = (
            await tools.view_file(attachment_id=attachment_id)
            if entrypoint == "view_file"
            else await tools.get_attachment(attachment_id, view=True)
        )

    receipt = json.loads(result.content if isinstance(result, ToolResult) else result)
    assert receipt["attachment_id"] == attachment_id
    assert path.read_bytes() == original
    if case in {"corrupt", "pixels"}:
        if isinstance(result, ToolResult):
            assert not result.images
            assert receipt["view_status"] == "error"
        else:
            assert receipt["status"] == "error"
        assert ("decoded" if case == "corrupt" else "pixel") in receipt["message"]
        return
    assert isinstance(result, ToolResult)
    assert result.images
    assert receipt["view_status"] == "ready"
    with Image.open(io.BytesIO(result.images[0].content)) as image:
        if case == "large":
            assert image.size == (2048, 683)
            assert receipt["resized"] is True
        else:
            assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
            assert receipt["first_frame_only"] is True


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
@pytest.mark.parametrize("entrypoint", ["view_file", "get_attachment"])
async def test_view_file_attachment_denies_wrong_room_even_when_listed(tmp_path: Path, entrypoint: str) -> None:
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
        tools = AttachmentTools()
        result = (
            await tools.view_file(attachment_id=record.attachment_id)
            if entrypoint == "view_file"
            else await tools.get_attachment(record.attachment_id, view=True)
        )
    if isinstance(result, ToolResult):
        assert not result.images
        assert json.loads(result.content)["view_status"] == "error"
    else:
        assert json.loads(result)["status"] == "error"


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
@pytest.mark.parametrize("entrypoint", ["path", "view_file", "get_attachment"])
async def test_unsupported_adapter_retains_artifact_and_reports_limitation(tmp_path: Path, entrypoint: str) -> None:
    """An adapter that omits images must never report a successful view."""
    context = replace(
        _tool_context(tmp_path, process_env={"MINDROOM_EXECUTION_MODE": "off"}),
        active_model_name="text_only",
    )
    context.config.models["text_only"] = ModelConfig(provider="cerebras", id="test-model")
    path = tmp_path / "image.png"
    path.write_bytes(image_bytes())
    with tool_runtime_context(context):
        tools = AttachmentTools(tool_output_workspace_root=tmp_path)
        if entrypoint == "path":
            viewed = await tools.view_file(path="image.png")
        else:
            attachment_id = json.loads(await tools.register_attachment("image.png"))["attachment_id"]
            viewed = (
                await tools.view_file(attachment_id=attachment_id)
                if entrypoint == "view_file"
                else await tools.get_attachment(attachment_id, view=True)
            )
    assert isinstance(viewed, ToolResult)
    receipt = json.loads(viewed.content)
    assert receipt["view_status"] == "unsupported"
    assert not viewed.images
    retained = load_attachment(tmp_path, receipt["attachment_id"])
    assert retained is not None
    assert retained.local_path.read_bytes() == path.read_bytes() == image_bytes()
    assert not context.client.room_send.called


@pytest.mark.asyncio
async def test_retention_failure_preserves_viewable_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed retention reports its limitation without discarding model-visible pixels."""
    context = _tool_context(tmp_path, process_env={"MINDROOM_EXECUTION_MODE": "off"})
    (tmp_path / "image.png").write_bytes(image_bytes())
    monkeypatch.setattr(media_attachments, "register_image_bytes_attachment", lambda *_args, **_kwargs: None)
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
