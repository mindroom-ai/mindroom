"""Tests for the model-agnostic attachments toolkit."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.tools.function import FunctionCall, ToolResult

from mindroom.attachments import load_attachment, register_local_attachment
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.attachments import AttachmentTools
from mindroom.custom_tools.matrix_message import MatrixMessageTools
from mindroom.matrix.runtime_media import RuntimeEncryptedMediaAttachment
from mindroom.message_target import MessageTarget
from mindroom.session_ids import create_session_id
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
    list_tool_runtime_attachment_ids,
    register_tool_runtime_media_attachment,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.access_schema_support import with_current_room_member_access
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import bind_runtime_paths, make_latest_thread_event_id_mock, make_relation_lookup


def _tool_context(
    tmp_path: Path,
    *,
    attachment_ids: tuple[str, ...] = (),
    process_env: dict[str, str] | None = None,
) -> ToolRuntimeContext:
    client = MagicMock()
    client.rooms = {"!room:localhost": MagicMock()}
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env or {},
    )
    config = bind_runtime_paths(
        with_current_room_member_access(
            Config(
                agents={"openclaw": AgentConfig(display_name="OpenClaw")},
                authorization={},
            ),
        ),
        runtime_paths,
    )
    conversation_reader = AsyncMock()
    conversation_reader.latest_thread_event_id = make_latest_thread_event_id_mock()
    return make_test_tool_runtime_context(
        agent_name="openclaw",
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread:localhost",
            reply_to_event_id=None,
        ),
        requester_id="@user:localhost",
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=conversation_reader,
        storage_path=tmp_path,
        attachment_ids=attachment_ids,
    )


def _shared_worker_target() -> object:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="openclaw",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        session_id="!room:localhost:$thread:localhost",
    )
    return resolve_worker_target("shared", "openclaw", identity)


def _tool_context_with_thread_scope(
    tmp_path: Path,
    *,
    thread_id: str | None,
    resolved_thread_id: str | None,
    attachment_ids: tuple[str, ...] = (),
) -> ToolRuntimeContext:
    """Build a tool context with explicit raw and resolved thread scope values."""
    context = _tool_context(tmp_path, attachment_ids=attachment_ids)
    return dataclasses.replace(
        context,
        target=dataclasses.replace(
            context.target,
            source_thread_id=thread_id,
            resolved_thread_id=resolved_thread_id,
            session_id=create_session_id(context.room_id, resolved_thread_id),
        ),
    )


def test_attachments_tool_hides_send_method_from_exposed_tools() -> None:
    """Attachments tool should expose discovery, registration, and model viewing without send operations."""
    tool = AttachmentTools()
    exposed = {method.__name__ for method in tool.tools}
    assert exposed == {"list_attachments", "get_attachment", "register_attachment", "view_file"}
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
    assert payload["attachments"][0]["local_path"] == str(attachment.local_path)


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
    assert payload["attachment"]["local_path"] == str(attachment.local_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("view", [False, True])
async def test_attachments_tool_get_attachment_rejects_out_of_context_ids(tmp_path: Path, view: bool) -> None:
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
        payload = json.loads(await tool.get_attachment("att_sample", view=view))

    assert payload["status"] == "error"
    assert payload["tool"] == "attachments"
    assert "not available in this context" in payload["message"]


@pytest.mark.asyncio
async def test_get_attachment_view_returns_image_media(tmp_path: Path) -> None:
    """Registered images reach the model as media, not just a path in JSON."""
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=",
    )
    image_path = tmp_path / "plot.jpg"  # Byte detection must win over the extension.
    image_path.write_bytes(image_bytes)
    tool = AttachmentTools(tool_output_workspace_root=tmp_path)
    with tool_runtime_context(_tool_context(tmp_path)):
        registered = json.loads(await tool.register_attachment("plot.jpg"))
        attachment_id = registered["attachment_id"]
        metadata = await tool.get_attachment(attachment_id)
        execution = await FunctionCall(
            function=tool.async_functions["get_attachment"],
            arguments={"attachment_id": attachment_id, "view": True},
        ).aexecute()

    assert execution.status == "success"
    result = execution.result
    assert isinstance(result, ToolResult)
    receipt = json.loads(result.content)
    assert receipt.items() >= json.loads(metadata).items()
    assert receipt["view_status"] == "ready"
    assert result.images is not None
    assert len(result.images) == 1
    assert result.images[0].content == image_bytes
    assert result.images[0].mime_type == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "payload_bytes", "media_field", "mime_type"),
    [
        ("recording.mp3", b"ID3 audio bytes", "audios", "audio/mpeg"),
        ("document.pdf", b"%PDF-1.4 document bytes", "files", "application/pdf"),
        ("notes.txt", b"Read these notes", "files", "text/plain"),
        ("archive.zip", b"PK archive bytes", "files", "application/zip"),
        ("email.eml", b"Subject: Notes\n\nRead these notes", "files", "message/rfc822"),
        ("clip.mp4", b"video bytes", "videos", "video/mp4"),
    ],
)
async def test_get_attachment_view_returns_other_media(
    tmp_path: Path,
    filename: str,
    payload_bytes: bytes,
    media_field: str,
    mime_type: str,
) -> None:
    """Audio, documents, and video use their native model media fields."""
    (tmp_path / filename).write_bytes(payload_bytes)
    tool = AttachmentTools(tool_output_workspace_root=tmp_path)
    with tool_runtime_context(_tool_context(tmp_path)):
        registered = json.loads(await tool.register_attachment(filename))
        result = await tool.get_attachment(registered["attachment_id"], view=True)

    assert isinstance(result, ToolResult)
    media = {"audios": result.audios, "files": result.files, "videos": result.videos}[media_field]
    assert media is not None
    assert len(media) == 1
    assert media[0].content == payload_bytes
    assert media[0].mime_type == mime_type
    if result.audios:
        assert result.audios[0].format == "mp3"
    if result.files:
        assert result.files[0].filename == filename


@pytest.mark.asyncio
@pytest.mark.parametrize(("filename", "data"), [("empty.pdf", b""), ("archive.tar", b"tar archive bytes")])
async def test_get_attachment_view_rejects_unusable_documents(tmp_path: Path, filename: str, data: bytes) -> None:
    """Empty and unsupported documents give a recoverable tool error, not an exception."""
    (tmp_path / filename).write_bytes(data)
    tool = AttachmentTools(tool_output_workspace_root=tmp_path)
    with tool_runtime_context(_tool_context(tmp_path)):
        registered = json.loads(await tool.register_attachment(filename))
        result = await tool.get_attachment(registered["attachment_id"], view=True)

    assert isinstance(result, str)
    assert json.loads(result)["status"] == "error"


@pytest.mark.asyncio
async def test_get_attachment_view_uses_source_filename_when_none_is_given(tmp_path: Path) -> None:
    """Registration keeps the source name because the retained copy has an opaque name."""
    local_path = tmp_path / "document.pdf"
    local_path.write_bytes(b"%PDF-1.4 document bytes")
    attachment = register_local_attachment(tmp_path, local_path, kind="file", mime_type="application/pdf")
    assert attachment is not None
    assert attachment.filename == "document.pdf"
    assert attachment.local_path.name != "document.pdf"
    with tool_runtime_context(_tool_context(tmp_path, attachment_ids=(attachment.attachment_id,))):
        result = await AttachmentTools().get_attachment(attachment.attachment_id, view=True)

    assert isinstance(result, ToolResult)
    assert result.files is not None
    assert result.files[0].filename == "document.pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["non_image", "oversized", "missing", "save_and_view"])
async def test_get_attachment_view_rejects_unusable_images(tmp_path: Path, case: str) -> None:
    """Viewing must fail explicitly rather than forward unusable media or silently save."""
    image_path = tmp_path / "plot.png"
    image_path.write_bytes(b"not an image")
    tool = AttachmentTools(tool_output_workspace_root=tmp_path)
    with tool_runtime_context(_tool_context(tmp_path)):
        registered = json.loads(await tool.register_attachment("plot.png"))
        retained_path = Path(registered["attachment"]["local_path"])
        if case == "oversized":
            os.truncate(retained_path, 20 * 1024 * 1024 + 1)
        elif case == "missing":
            retained_path.unlink()
        result = await tool.get_attachment(
            registered["attachment_id"],
            view=True,
            mindroom_output_path="copy.png" if case == "save_and_view" else None,
        )

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["status"] == "error"
    expected = {
        "non_image": "PNG, JPEG, GIF or WebP",
        "oversized": "size limit",
        "missing": "missing on disk",
        "save_and_view": "cannot be combined",
    }
    assert expected[case] in payload["message"]
    assert not (tmp_path / "copy.png").exists()


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_mindroom_output_path_writes_primary_workspace(
    tmp_path: Path,
) -> None:
    """Unsafe-local opt-in should write attachment bytes into the primary workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = AttachmentTools(tool_output_workspace_root=workspace)
    sample_file = tmp_path / "sample.txt"
    sample_file.write_bytes(b"hello")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_sample",
    )
    assert attachment is not None

    with tool_runtime_context(
        _tool_context(
            tmp_path,
            attachment_ids=(attachment.attachment_id,),
            process_env={"MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS": "true"},
        ),
    ):
        payload = json.loads(await tool.get_attachment("att_sample", mindroom_output_path="inputs/sample.txt"))

    saved_path = workspace / "inputs" / "sample.txt"
    assert saved_path.read_bytes() == b"hello"
    assert stat.S_IMODE(saved_path.stat().st_mode) == 0o600
    assert payload["status"] == "ok"
    assert payload["attachment_id"] == "att_sample"
    assert payload["attachment"]["save_path"] == "inputs/sample.txt"
    assert payload["attachment"]["size_bytes"] == 5
    assert "sha256" in payload["attachment"]
    assert "local_path" not in payload["attachment"]
    assert payload["mindroom_tool_output"]["status"] == "saved_to_file"
    assert payload["mindroom_tool_output"]["path"] == "inputs/sample.txt"
    assert payload["mindroom_tool_output"]["format"] == "binary"
    assert payload["mindroom_tool_output"] == {
        "status": "saved_to_file",
        "path": "inputs/sample.txt",
        "bytes": 5,
        "format": "binary",
        "overwritten": False,
        "sha256": hashlib.sha256(b"hello").hexdigest(),
    }


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_schema_describes_output_path(
    tmp_path: Path,
) -> None:
    """The bespoke attachment save arg should still carry the canonical output-path description."""
    del tmp_path
    tool = AttachmentTools()

    parameters = tool.async_functions["get_attachment"].parameters
    schema = parameters["properties"]

    assert schema["attachment_id"]["description"] == "Context-scoped attachment ID returned by list_attachments."
    assert schema["mindroom_output_path"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert schema["mindroom_output_path"]["default"] is None
    assert "Use this for large output" in schema["mindroom_output_path"]["description"]
    assert "mindroom_output_path" not in parameters["required"]
    assert "save_to_disk" not in schema


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_path", ["", "  ", "..", "../escape.txt", "/abs/path", "foo\x00bar", "$HOME/x", "~/x"])
async def test_attachments_tool_get_attachment_mindroom_output_path_rejects_unsafe_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    """Attachment save paths should reuse the normal workspace output path policy."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = AttachmentTools(tool_output_workspace_root=workspace)
    sample_file = tmp_path / "sample.txt"
    sample_file.write_bytes(b"hello")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_sample",
    )
    assert attachment is not None

    with (
        tool_runtime_context(_tool_context(tmp_path, attachment_ids=(attachment.attachment_id,))),
        patch.object(type(sample_file), "read_bytes", side_effect=AssertionError("attachment bytes were read")),
        patch("mindroom.custom_tools.attachments.save_attachment_to_worker") as mocked_save,
    ):
        payload = json.loads(await tool.get_attachment("att_sample", mindroom_output_path=unsafe_path))

    assert payload["status"] == "error"
    assert "mindroom_output_path" in payload["message"]
    mocked_save.assert_not_called()
    assert not any(workspace.rglob("*"))


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_out_of_context_save_does_not_send_bytes(
    tmp_path: Path,
) -> None:
    """Out-of-context IDs should fail before reading or sending attachment bytes."""
    workspace = tmp_path / "workspace"
    tool = AttachmentTools(tool_output_workspace_root=workspace)
    sample_file = tmp_path / "sample.txt"
    sample_file.write_bytes(b"hello")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_sample",
    )
    assert attachment is not None

    with (
        tool_runtime_context(_tool_context(tmp_path, attachment_ids=())),
        patch("mindroom.custom_tools.attachments.save_attachment_to_worker") as mocked_save,
    ):
        payload = json.loads(await tool.get_attachment("att_sample", mindroom_output_path="sample.txt"))

    assert payload["status"] == "error"
    assert "not available in this context" in payload["message"]
    mocked_save.assert_not_called()
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_execution_mode_off_saves_primary_workspace(
    tmp_path: Path,
) -> None:
    """A worker target should not redirect attachments when workspace tools are configured local."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_env = {
        "MINDROOM_SANDBOX_EXECUTION_MODE": "off",
        "MINDROOM_WORKER_BACKEND": "kubernetes",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
    }
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=runtime_env,
    )
    tool = AttachmentTools(
        runtime_paths=runtime_paths,
        worker_target=_shared_worker_target(),
        tool_output_workspace_root=workspace,
    )
    sample_file = tmp_path / "sample.txt"
    sample_file.write_bytes(b"hello")
    attachment = register_local_attachment(tmp_path, sample_file, kind="file", attachment_id="att_sample")
    assert attachment is not None

    with (
        tool_runtime_context(
            _tool_context(tmp_path, attachment_ids=(attachment.attachment_id,), process_env=runtime_env),
        ),
        patch("mindroom.custom_tools.attachments.save_attachment_to_worker") as mocked_save,
    ):
        payload = json.loads(await tool.get_attachment("att_sample", mindroom_output_path="inputs/sample.txt"))

    assert payload["status"] == "ok"
    assert (workspace / "inputs" / "sample.txt").read_bytes() == b"hello"
    mocked_save.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker_tools_override",
    [
        ["coding"],
        ["python"],
        ["shell", "coding"],
    ],
)
async def test_attachments_tool_get_attachment_selective_proxy_uses_worker_for_workspace_consumers(
    tmp_path: Path,
    worker_tools_override: list[str],
) -> None:
    """Attachment saves should land on the worker when workspace tools can consume the workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_env = {
        "MINDROOM_SANDBOX_EXECUTION_MODE": "selective",
        "MINDROOM_SANDBOX_PROXY_TOOLS": ",".join(worker_tools_override),
        "MINDROOM_WORKER_BACKEND": "kubernetes",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
    }
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=runtime_env,
    )
    tool = AttachmentTools(
        runtime_paths=runtime_paths,
        worker_target=_shared_worker_target(),
        worker_tools_override=worker_tools_override,
        tool_output_workspace_root=workspace,
    )
    sample_file = tmp_path / "sample.txt"
    sample_file.write_bytes(b"hello")
    attachment = register_local_attachment(tmp_path, sample_file, kind="file", attachment_id="att_sample")
    assert attachment is not None

    with (
        tool_runtime_context(
            _tool_context(tmp_path, attachment_ids=(attachment.attachment_id,), process_env=runtime_env),
        ),
        patch(
            "mindroom.custom_tools.attachments.save_attachment_to_worker",
            return_value=SimpleNamespace(
                worker_path="inputs/sample.txt",
                size_bytes=5,
                sha256="sha256",
            ),
        ) as mocked_save,
    ):
        payload = json.loads(await tool.get_attachment("att_sample", mindroom_output_path="inputs/sample.txt"))

    assert payload["status"] == "ok"
    assert payload["attachment"]["save_path"] == "inputs/sample.txt"
    assert payload["mindroom_tool_output"] == {
        "status": "saved_to_file",
        "path": "inputs/sample.txt",
        "bytes": 5,
        "format": "binary",
        "sha256": "sha256",
    }
    assert not any(workspace.rglob("*"))
    mocked_save.assert_called_once()
    assert mocked_save.call_args.kwargs["worker_tools_override"] == worker_tools_override


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_worker_save_ignores_primary_workspace_conflicts(
    tmp_path: Path,
) -> None:
    """Worker saves should not validate against local-only filesystem state."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "inputs").write_text("local conflict", encoding="utf-8")
    runtime_env = {
        "MINDROOM_SANDBOX_EXECUTION_MODE": "selective",
        "MINDROOM_SANDBOX_PROXY_TOOLS": "file",
        "MINDROOM_WORKER_BACKEND": "kubernetes",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
    }
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=runtime_env,
    )
    tool = AttachmentTools(
        runtime_paths=runtime_paths,
        worker_target=_shared_worker_target(),
        worker_tools_override=["file"],
        tool_output_workspace_root=workspace,
    )
    sample_file = tmp_path / "sample.txt"
    sample_file.write_bytes(b"hello")
    attachment = register_local_attachment(tmp_path, sample_file, kind="file", attachment_id="att_sample")
    assert attachment is not None

    with (
        tool_runtime_context(
            _tool_context(tmp_path, attachment_ids=(attachment.attachment_id,), process_env=runtime_env),
        ),
        patch(
            "mindroom.custom_tools.attachments.save_attachment_to_worker",
            return_value=SimpleNamespace(
                worker_path="inputs/sample.txt",
                size_bytes=5,
                sha256="sha256",
            ),
        ) as mocked_save,
    ):
        payload = json.loads(await tool.get_attachment("att_sample", mindroom_output_path="inputs/sample.txt"))

    assert payload["status"] == "ok"
    assert payload["attachment"]["save_path"] == "inputs/sample.txt"
    mocked_save.assert_called_once()


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_worker_save_does_not_block_event_loop(
    tmp_path: Path,
) -> None:
    """The async attachment tool should not run the blocking worker upload on the event loop."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_env = {
        "MINDROOM_SANDBOX_EXECUTION_MODE": "selective",
        "MINDROOM_SANDBOX_PROXY_TOOLS": "file",
        "MINDROOM_WORKER_BACKEND": "kubernetes",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
    }
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=runtime_env,
    )
    tool = AttachmentTools(
        runtime_paths=runtime_paths,
        worker_target=_shared_worker_target(),
        worker_tools_override=["file"],
        tool_output_workspace_root=workspace,
    )
    sample_file = tmp_path / "sample.txt"
    sample_file.write_bytes(b"hello")
    attachment = register_local_attachment(tmp_path, sample_file, kind="file", attachment_id="att_sample")
    assert attachment is not None
    save_finished = False
    marker_observed_save_finished: bool | None = None

    def blocking_save(**_kwargs: object) -> SimpleNamespace:
        nonlocal save_finished
        time.sleep(0.05)
        save_finished = True
        return SimpleNamespace(
            worker_path="inputs/sample.txt",
            size_bytes=5,
            sha256="sha256",
        )

    async def marker() -> None:
        nonlocal marker_observed_save_finished
        await asyncio.sleep(0.01)
        marker_observed_save_finished = save_finished

    with (
        tool_runtime_context(
            _tool_context(tmp_path, attachment_ids=(attachment.attachment_id,), process_env=runtime_env),
        ),
        patch("mindroom.custom_tools.attachments.save_attachment_to_worker", side_effect=blocking_save),
    ):
        payload_task = asyncio.create_task(
            tool.get_attachment("att_sample", mindroom_output_path="inputs/sample.txt"),
        )
        marker_task = asyncio.create_task(marker())
        payload = json.loads(await payload_task)
        await marker_task

    assert payload["status"] == "ok"
    assert marker_observed_save_finished is False


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_worker_save_protocol_error_returns_payload(
    tmp_path: Path,
) -> None:
    """Worker-save transport/protocol exceptions should be normal attachment tool errors."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_env = {
        "MINDROOM_SANDBOX_EXECUTION_MODE": "selective",
        "MINDROOM_SANDBOX_PROXY_TOOLS": "file",
        "MINDROOM_WORKER_BACKEND": "kubernetes",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
    }
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=runtime_env,
    )
    tool = AttachmentTools(
        runtime_paths=runtime_paths,
        worker_target=_shared_worker_target(),
        worker_tools_override=["file"],
        tool_output_workspace_root=workspace,
    )
    sample_file = tmp_path / "sample.txt"
    sample_file.write_bytes(b"hello")
    attachment = register_local_attachment(tmp_path, sample_file, kind="file", attachment_id="att_sample")
    assert attachment is not None

    with (
        tool_runtime_context(
            _tool_context(tmp_path, attachment_ids=(attachment.attachment_id,), process_env=runtime_env),
        ),
        patch("mindroom.custom_tools.attachments.save_attachment_to_worker", side_effect=TypeError("bad receipt")),
    ):
        payload = json.loads(await tool.get_attachment("att_sample", mindroom_output_path="inputs/sample.txt"))

    assert payload["status"] == "error"
    assert payload["tool"] == "attachments"
    assert "bad receipt" in payload["message"]
    assert not any(workspace.rglob("*"))


@pytest.mark.asyncio
async def test_matrix_message_attachments_sends_attachment_ids(tmp_path: Path) -> None:
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
        with tool_runtime_context(context):
            result = json.loads(await MatrixMessageTools().matrix_message(attachments=["att_upload"]))
        send_error = result.get("message") if result["status"] == "error" else None

    assert send_error is None
    assert result["status"] == "ok"
    assert result["attachment_event_ids"] == ["$file_evt"]
    assert result["resolved_attachment_ids"] == ["att_upload"]
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_message_attachments_reuses_ephemeral_encrypted_media(tmp_path: Path) -> None:
    """Turn-scoped media sends reuse MXC ciphertext and never create a local attachment file."""
    context = _tool_context(tmp_path)
    attachment = RuntimeEncryptedMediaAttachment(
        attachment_id="att_screenshot",
        filename="desktop-screenshot.png",
        url="mxc://example.org/screenshot",
        key="key",
        iv="iv",
        sha256="hash",
        mime_type="image/png",
        size=123,
    )
    register_tool_runtime_media_attachment(context, attachment)

    with (
        patch(
            "mindroom.custom_tools.attachments.send_runtime_encrypted_media_message",
            new=AsyncMock(return_value="$image_evt"),
        ) as send_runtime_media,
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock()) as send_file,
    ):
        with tool_runtime_context(context):
            result = json.loads(await MatrixMessageTools().matrix_message(attachments=[attachment.attachment_id]))
        send_error = result.get("message") if result["status"] == "error" else None

    assert send_error is None
    assert result["status"] == "ok"
    assert result["attachment_event_ids"] == ["$image_evt"]
    assert result["resolved_attachment_ids"] == [attachment.attachment_id]
    send_runtime_media.assert_awaited_once_with(
        context.client,
        context.room_id,
        attachment,
        thread_id=context.resolved_thread_id,
        latest_thread_event_id=context.resolved_thread_id,
    )
    send_file.assert_not_awaited()
    assert not (tmp_path / "attachments").exists()
    assert not (tmp_path / "incoming_media").exists()

    with tool_runtime_context(context):
        payload = json.loads(await AttachmentTools().list_attachments(attachment.attachment_id))
    assert payload["attachments"] == [attachment.tool_payload()]
    assert payload["missing_attachment_ids"] == []


@pytest.mark.parametrize("existing_registry", ["attachment_ids", "runtime_attachment_ids"])
def test_runtime_media_registration_rejects_attachment_id_namespace_collisions(
    tmp_path: Path,
    existing_registry: str,
) -> None:
    """Runtime media must not shadow another attachment registry entry."""
    attachment = RuntimeEncryptedMediaAttachment(
        attachment_id="att_collision",
        filename="desktop-screenshot.png",
        url="mxc://example.org/screenshot",
        key="key",
        iv="iv",
        sha256="hash",
        mime_type="image/png",
        size=123,
    )
    context = _tool_context(
        tmp_path,
        attachment_ids=(attachment.attachment_id,) if existing_registry == "attachment_ids" else (),
    )
    if existing_registry == "runtime_attachment_ids":
        context.runtime_attachment_ids.append(attachment.attachment_id)

    with pytest.raises(ValueError, match="Runtime attachment ID collision"):
        register_tool_runtime_media_attachment(context, attachment)

    assert context.runtime_media_attachments == {}


def test_runtime_media_registration_is_idempotent(tmp_path: Path) -> None:
    """The same runtime media handle may be registered repeatedly without duplication."""
    context = _tool_context(tmp_path)
    attachment = RuntimeEncryptedMediaAttachment(
        attachment_id="att_screenshot",
        filename="desktop-screenshot.png",
        url="mxc://example.org/screenshot",
        key="key",
        iv="iv",
        sha256="hash",
        mime_type="image/png",
        size=123,
    )

    register_tool_runtime_media_attachment(context, attachment)
    register_tool_runtime_media_attachment(context, attachment)

    assert context.runtime_media_attachments == {attachment.attachment_id: attachment}
    assert context.runtime_attachment_ids == [attachment.attachment_id]


@pytest.mark.asyncio
async def test_matrix_message_attachments_reuses_latest_thread_event_id_for_multiple_files(tmp_path: Path) -> None:
    """Threaded attachment batches should resolve the latest event once and advance it locally."""
    first_file = tmp_path / "one.txt"
    second_file = tmp_path / "two.txt"
    first_file.write_text("one", encoding="utf-8")
    second_file.write_text("two", encoding="utf-8")
    first_attachment = register_local_attachment(
        tmp_path,
        first_file,
        kind="file",
        attachment_id="att_one",
    )
    second_attachment = register_local_attachment(
        tmp_path,
        second_file,
        kind="file",
        attachment_id="att_two",
    )
    assert first_attachment is not None
    assert second_attachment is not None

    context = _tool_context(tmp_path, attachment_ids=("att_one", "att_two"))
    context.conversation_reader.latest_thread_event_id = AsyncMock(return_value="$latest:localhost")

    with patch(
        "mindroom.custom_tools.attachments.send_file_message",
        new=AsyncMock(side_effect=["$file_evt_1", "$file_evt_2"]),
    ) as mock_send:
        with tool_runtime_context(context):
            result = json.loads(await MatrixMessageTools().matrix_message(attachments=["att_one", "att_two"]))
        send_error = result.get("message") if result["status"] == "error" else None

    assert send_error is None
    assert result["status"] == "ok"
    assert result["attachment_event_ids"] == ["$file_evt_1", "$file_evt_2"]
    context.conversation_reader.latest_thread_event_id.assert_awaited_once_with(
        room_id=context.room_id,
        thread_id=context.thread_id,
        known_latest_thread_event_id=None,
    )
    first_call = mock_send.await_args_list[0]
    second_call = mock_send.await_args_list[1]
    assert first_call.kwargs["latest_thread_event_id"] == "$latest:localhost"
    assert second_call.kwargs["latest_thread_event_id"] == "$file_evt_1"


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
async def test_matrix_message_attachments_cross_room_send_does_not_inherit_source_thread(tmp_path: Path) -> None:
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
        with tool_runtime_context(ctx):
            result = json.loads(
                await MatrixMessageTools().matrix_message(attachments=["att_cross"], room_id="!other:localhost"),
            )
        send_error = result.get("message") if result["status"] == "error" else None

    assert send_error is None
    assert result["status"] == "ok"
    mocked.assert_awaited_once()
    call_kwargs = mocked.await_args.kwargs
    assert call_kwargs["thread_id"] is None  # must NOT inherit source thread


@pytest.mark.asyncio
async def test_attachments_tool_register_attachment_uses_resolved_thread_scope(tmp_path: Path) -> None:
    """Registering from a thread-start context should persist the resolved thread root."""
    tool = AttachmentTools()
    generated_file = tmp_path / "generated.txt"
    generated_file.write_text("artifact", encoding="utf-8")
    ctx = _tool_context_with_thread_scope(
        tmp_path,
        thread_id=None,
        resolved_thread_id="$thread-root:localhost",
    )

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.register_attachment(str(generated_file)))

    assert payload["status"] == "ok"
    attachment = load_attachment(tmp_path, payload["attachment_id"])
    assert attachment is not None
    assert attachment.thread_id == "$thread-root:localhost"


@pytest.mark.asyncio
async def test_attachments_tool_register_attachment_resolves_relative_paths_from_workspace(tmp_path: Path) -> None:
    """Registering a relative file path should use the agent workspace root."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    generated_file = workspace / "scratch" / "generated.txt"
    generated_file.parent.mkdir()
    generated_file.write_text("artifact", encoding="utf-8")
    tool = AttachmentTools(tool_output_workspace_root=workspace)
    ctx = _tool_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.register_attachment("scratch/generated.txt"))

    assert payload["status"] == "ok"
    attachment = load_attachment(tmp_path, payload["attachment_id"])
    assert attachment is not None
    assert payload["attachment"]["local_path"] == str(attachment.local_path)
    assert attachment.local_path.parent == (tmp_path / "incoming_media").resolve()
    assert attachment.local_path.read_text(encoding="utf-8") == "artifact"
    assert attachment.filename == "generated.txt"


@pytest.mark.asyncio
async def test_attachments_tool_register_attachment_accepts_workspace_below_linked_ancestor(tmp_path: Path) -> None:
    """A configured workspace reached through a linked ancestor directory still registers relative files."""
    real_storage = tmp_path / "real"
    (real_storage / "workspace" / "scratch").mkdir(parents=True)
    (real_storage / "workspace" / "scratch" / "generated.txt").write_text("artifact", encoding="utf-8")
    (tmp_path / "linked").symlink_to(real_storage)
    tool = AttachmentTools(tool_output_workspace_root=tmp_path / "linked" / "workspace")

    with tool_runtime_context(_tool_context(tmp_path)):
        payload = json.loads(await tool.register_attachment("scratch/../scratch/generated.txt"))

    assert payload["status"] == "ok"
    assert Path(payload["attachment"]["local_path"]).read_text(encoding="utf-8") == "artifact"


@pytest.mark.asyncio
async def test_register_attachment_rejects_workspace_root_replaced_by_link(tmp_path: Path) -> None:
    """A workspace root swapped for a link to primary storage must not be opened through the link."""
    agent_root = tmp_path / "agent"
    agent_root.mkdir()
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "gmail_credentials.json").write_text("SECRET_TOKEN", encoding="utf-8")
    (agent_root / "workspace").symlink_to(credentials)
    tool = AttachmentTools(tool_output_workspace_root=agent_root / "workspace")

    with tool_runtime_context(_tool_context(tmp_path)):
        payload = json.loads(await tool.register_attachment("gmail_credentials.json"))

    assert payload["status"] == "error"
    assert not (tmp_path / "attachments").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("use", ["view", "save", "send"])
async def test_registered_workspace_file_swapped_for_link_never_reads_link_target(
    tmp_path: Path,
    use: str,
) -> None:
    """Sandboxed code replacing a registered workspace file must not redirect later primary reads or sends."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "config.yaml"
    secret.write_text("SECRET_API_KEY", encoding="utf-8")
    notes = workspace / "notes.txt"
    notes.write_text("public notes", encoding="utf-8")
    tool = AttachmentTools(tool_output_workspace_root=workspace)
    ctx = _tool_context(tmp_path, process_env={"MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS": "true"})

    with (
        tool_runtime_context(ctx),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file")) as send_file,
    ):
        attachment_id = json.loads(await tool.register_attachment("notes.txt"))["attachment_id"]
        notes.unlink()
        notes.symlink_to(secret)
        if use == "view":
            result = await tool.get_attachment(attachment_id, view=True)
            assert isinstance(result, ToolResult)
            assert result.files is not None
            assert result.files[0].content == b"public notes"
        elif use == "save":
            payload = json.loads(await tool.get_attachment(attachment_id, mindroom_output_path="copy.txt"))
            assert payload["status"] == "ok"
            assert (workspace / "copy.txt").read_text(encoding="utf-8") == "public notes"
        else:
            payload = json.loads(await MatrixMessageTools().matrix_message(attachments=[attachment_id]))
            assert payload["status"] == "ok"
            uploaded = send_file.await_args.args[2]
            assert not uploaded.is_relative_to(workspace)
            assert uploaded.read_text(encoding="utf-8") == "public notes"
            assert send_file.await_args.kwargs["filename"] == "notes.txt"
            assert send_file.await_args.kwargs["mimetype"] == "text/plain"


@pytest.mark.asyncio
async def test_get_attachment_view_rejects_linked_retained_media(tmp_path: Path) -> None:
    """Reads of retained media must not follow a link even inside primary-owned storage."""
    secret = tmp_path / "config.yaml"
    secret.write_text("SECRET_API_KEY", encoding="utf-8")
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("hello", encoding="utf-8")
    attachment = register_local_attachment(tmp_path, sample_file, kind="file", mime_type="text/plain")
    assert attachment is not None
    attachment.local_path.unlink()
    attachment.local_path.symlink_to(secret)

    with tool_runtime_context(_tool_context(tmp_path, attachment_ids=(attachment.attachment_id,))):
        result = await AttachmentTools().get_attachment(attachment.attachment_id, view=True)

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert "missing on disk" in payload["message"]


@pytest.mark.asyncio
async def test_matrix_message_attachments_inherits_resolved_thread_scope(tmp_path: Path) -> None:
    """Attachment sends should stay in the resolved thread even when raw thread_id is absent."""
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        attachment_id="att_threaded",
        room_id="!room:localhost",
        thread_id="$thread-root:localhost",
    )
    assert attachment is not None

    ctx = _tool_context_with_thread_scope(
        tmp_path,
        thread_id=None,
        resolved_thread_id="$thread-root:localhost",
        attachment_ids=("att_threaded",),
    )

    with patch(
        "mindroom.custom_tools.attachments.send_file_message",
        new=AsyncMock(return_value="$file_evt"),
    ) as mocked:
        with tool_runtime_context(ctx):
            result = json.loads(await MatrixMessageTools().matrix_message(attachments=["att_threaded"]))
        send_error = result.get("message") if result["status"] == "error" else None

    assert send_error is None
    assert result["status"] == "ok"
    assert result["thread_id"] == "$thread-root:localhost"
    ctx.conversation_reader.latest_thread_event_id.assert_awaited_once_with(
        room_id=ctx.room_id,
        thread_id="$thread-root:localhost",
        known_latest_thread_event_id=None,
    )
    assert mocked.await_args.kwargs["thread_id"] == "$thread-root:localhost"


@pytest.mark.asyncio
async def test_attachments_tool_registers_file_and_updates_runtime_context(tmp_path: Path) -> None:
    """Registering a file should make it available for matrix_message_attachments in the same context."""
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
        with tool_runtime_context(current_context):
            send_result = json.loads(await MatrixMessageTools().matrix_message(attachments=[attachment_id]))
        send_error = send_result.get("message") if send_result["status"] == "error" else None

    assert register_payload["status"] == "ok"
    assert register_payload["tool"] == "attachments"
    assert register_payload["attachment_id"].startswith("att_")
    assert Path(register_payload["attachment"]["local_path"]).read_text(encoding="utf-8") == "artifact"
    assert attachment_id in list_tool_runtime_attachment_ids(current_context)
    assert send_error is None
    assert send_result["status"] == "ok"
    assert send_result["resolved_attachment_ids"] == [attachment_id]
    mocked.assert_awaited_once()
    assert mocked.await_args.kwargs["filename"] == "generated.txt"


@pytest.mark.asyncio
async def test_attachments_tool_register_attachment_infers_file_metadata(tmp_path: Path) -> None:
    """Registering a local path should preserve filename, MIME type, and media kind."""
    tool = AttachmentTools()
    generated_file = tmp_path / "clip.wav"
    generated_file.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    ctx = _tool_context(tmp_path)

    with tool_runtime_context(ctx):
        payload = json.loads(await tool.register_attachment(str(generated_file)))

    assert payload["status"] == "ok"
    assert payload["attachment"]["filename"] == "clip.wav"
    assert payload["attachment"]["mime_type"].startswith("audio/")
    assert payload["attachment"]["kind"] == "audio"

    attachment = load_attachment(tmp_path, payload["attachment_id"])
    assert attachment is not None
    assert attachment.filename == "clip.wav"
    assert attachment.mime_type is not None
    assert attachment.mime_type.startswith("audio/")
    assert attachment.kind == "audio"


@pytest.mark.asyncio
async def test_attachments_tool_register_attachment_available_after_task_boundary(tmp_path: Path) -> None:
    """Registered attachments should remain available when a later tool call runs in another task."""
    tool = AttachmentTools()
    generated_file = tmp_path / "generated.txt"
    generated_file.write_text("artifact", encoding="utf-8")
    ctx = _tool_context(tmp_path)

    with (
        tool_runtime_context(ctx),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        register_payload = json.loads(await asyncio.create_task(tool.register_attachment(str(generated_file))))
        current_context = get_tool_runtime_context()
        assert current_context is not None
        attachment_id = register_payload["attachment_id"]
        with tool_runtime_context(current_context):
            send_result = json.loads(await MatrixMessageTools().matrix_message(attachments=[attachment_id]))
        send_error = send_result.get("message") if send_result["status"] == "error" else None

    assert register_payload["status"] == "ok"
    assert send_error is None
    assert send_result["status"] == "ok"
    assert send_result["resolved_attachment_ids"] == [attachment_id]
    assert attachment_id in list_tool_runtime_attachment_ids(current_context)
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_message_attachments_cross_room_send_requires_authorization(tmp_path: Path) -> None:
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
        patch("mindroom.custom_tools.attachment_helpers.is_sender_allowed_for_responder", return_value=False),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        with tool_runtime_context(ctx):
            result = json.loads(
                await MatrixMessageTools().matrix_message(attachments=["att_authz"], room_id="!other:localhost"),
            )
        send_error = result.get("message") if result["status"] == "error" else None

    assert result["status"] == "error"
    assert send_error is not None
    assert "Not authorized" in send_error
    mocked.assert_not_awaited()


@pytest.mark.asyncio
async def test_matrix_message_attachments_sends_local_file_paths_by_auto_registering(tmp_path: Path) -> None:
    """Helper should auto-register local file paths and send them in the same call."""
    generated_file = tmp_path / "generated.txt"
    generated_file.write_text("artifact", encoding="utf-8")
    ctx = _tool_context(tmp_path)

    with (
        tool_runtime_context(ctx),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        with tool_runtime_context(ctx):
            result = json.loads(await MatrixMessageTools().matrix_message(attachments=[str(generated_file)]))
        send_error = result.get("message") if result["status"] == "error" else None
        current_context = get_tool_runtime_context()
        assert current_context is not None

    assert send_error is None
    assert result["status"] == "ok"
    assert result["resolved_attachment_ids"][0].startswith("att_")
    assert result["newly_registered_attachment_ids"] == result["resolved_attachment_ids"]
    assert result["newly_registered_attachment_ids"][0] in list_tool_runtime_attachment_ids(current_context)
    mocked.assert_awaited_once()


@pytest.mark.asyncio
async def test_matrix_message_attachments_rejects_workspace_relative_file_path_escape(tmp_path: Path) -> None:
    """Workspace-relative attachment paths must not resolve outside the agent workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("secret", encoding="utf-8")
    ctx = _tool_context(tmp_path)

    with (
        tool_runtime_context(ctx),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")) as mocked,
    ):
        with tool_runtime_context(ctx):
            result = json.loads(
                await MatrixMessageTools(tool_output_workspace_root=workspace).matrix_message(
                    attachments=["../outside.txt"],
                ),
            )
        send_error = result.get("message") if result["status"] == "error" else None

    assert result["status"] == "error"
    assert send_error is not None
    assert "workspace" in send_error
    mocked.assert_not_awaited()


def test_tool_runtime_context_none_temporarily_clears_nested_scope(tmp_path: Path) -> None:
    """tool_runtime_context(None) should clear and then restore an outer context."""
    ctx = _tool_context(tmp_path, attachment_ids=("att_upload",))
    with tool_runtime_context(ctx):
        assert get_tool_runtime_context() is ctx
        with tool_runtime_context(None):
            assert get_tool_runtime_context() is None
        assert get_tool_runtime_context() is ctx


@pytest.fixture(autouse=True)
def _reset_matrix_message_rate_limit() -> None:
    MatrixMessageTools._recent_actions.clear()
