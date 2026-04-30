"""Tests for the model-agnostic attachments toolkit."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import stat
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.attachments import load_attachment, register_local_attachment
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.attachments import AttachmentTools, send_context_attachments
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    get_tool_runtime_context,
    list_tool_runtime_attachment_ids,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.conftest import make_event_cache_mock

if TYPE_CHECKING:
    from pathlib import Path


def _tool_context(
    tmp_path: Path,
    *,
    attachment_ids: tuple[str, ...] = (),
    process_env: dict[str, str] | None = None,
) -> ToolRuntimeContext:
    async def _latest_thread_event_id(
        _room_id: str,
        thread_id: str | None,
        *_args: object,
        **_kwargs: object,
    ) -> str | None:
        return thread_id

    client = MagicMock()
    client.rooms = {"!room:localhost": MagicMock()}
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env=process_env or {},
    )
    conversation_cache = AsyncMock()
    conversation_cache.get_latest_thread_event_id_if_needed.side_effect = _latest_thread_event_id
    return ToolRuntimeContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=client,
        config=MagicMock(),
        runtime_paths=runtime_paths,
        event_cache=make_event_cache_mock(),
        conversation_cache=conversation_cache,
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
        thread_id=thread_id,
        resolved_thread_id=resolved_thread_id,
        session_id=(context.room_id if resolved_thread_id is None else f"{context.room_id}:{resolved_thread_id}"),
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
async def test_attachments_tool_get_attachment_mindroom_output_path_writes_primary_workspace(
    tmp_path: Path,
) -> None:
    """Saving an attachment without a worker target should write bytes into the primary workspace."""
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

    with tool_runtime_context(_tool_context(tmp_path, attachment_ids=(attachment.attachment_id,))):
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


@pytest.mark.asyncio
async def test_attachments_tool_get_attachment_schema_describes_output_path(
    tmp_path: Path,
) -> None:
    """The bespoke attachment save arg should still carry the canonical output-path description."""
    del tmp_path
    tool = AttachmentTools()

    schema = tool.async_functions["get_attachment"].parameters["properties"]

    assert "Use this for large output" in schema["mindroom_output_path"]["description"]
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
    assert payload["mindroom_tool_output"]["path"] == "inputs/sample.txt"
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
async def test_send_context_attachments_reuses_latest_thread_event_id_for_multiple_files(tmp_path: Path) -> None:
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

    event_cache = MagicMock()
    context = _tool_context(tmp_path, attachment_ids=("att_one", "att_two"))
    context = dataclasses.replace(context, event_cache=event_cache)
    context.conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest:localhost")

    with (
        patch(
            "mindroom.custom_tools.attachments.send_file_message",
            new=AsyncMock(side_effect=["$file_evt_1", "$file_evt_2"]),
        ) as mock_send,
    ):
        result, send_error = await send_context_attachments(
            context,
            attachment_ids=["att_one", "att_two"],
            attachment_file_paths=[],
        )

    assert send_error is None
    assert result is not None
    assert result.attachment_event_ids == ["$file_evt_1", "$file_evt_2"]
    context.conversation_cache.get_latest_thread_event_id_if_needed.assert_awaited_once_with(
        context.room_id,
        context.thread_id,
    )
    first_call = mock_send.await_args_list[0]
    second_call = mock_send.await_args_list[1]
    assert first_call.kwargs["latest_thread_event_id"] == "$latest:localhost"
    assert second_call.kwargs["latest_thread_event_id"] == "$file_evt_1"
    assert "event_cache" not in first_call.kwargs
    assert "event_cache" not in second_call.kwargs


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
async def test_send_context_attachments_inherits_resolved_thread_scope(tmp_path: Path) -> None:
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
        result, send_error = await send_context_attachments(
            ctx,
            attachment_ids=["att_threaded"],
            attachment_file_paths=[],
        )

    assert send_error is None
    assert result is not None
    assert result.thread_id == "$thread-root:localhost"
    ctx.conversation_cache.get_latest_thread_event_id_if_needed.assert_awaited_once_with(
        ctx.room_id,
        "$thread-root:localhost",
    )
    assert mocked.await_args.kwargs["thread_id"] == "$thread-root:localhost"


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
    assert attachment_id in list_tool_runtime_attachment_ids(current_context)
    assert send_error is None
    assert send_result is not None
    assert send_result.resolved_attachment_ids == [attachment_id]
    mocked.assert_awaited_once()


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
        send_result, send_error = await send_context_attachments(
            current_context,
            attachment_ids=[attachment_id],
            attachment_file_paths=[],
        )

    assert register_payload["status"] == "ok"
    assert send_error is None
    assert send_result is not None
    assert send_result.resolved_attachment_ids == [attachment_id]
    assert attachment_id in list_tool_runtime_attachment_ids(current_context)
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
    assert result.newly_registered_attachment_ids[0] in list_tool_runtime_attachment_ids(current_context)
    mocked.assert_awaited_once()


def test_tool_runtime_context_none_temporarily_clears_nested_scope(tmp_path: Path) -> None:
    """tool_runtime_context(None) should clear and then restore an outer context."""
    ctx = _tool_context(tmp_path, attachment_ids=("att_upload",))
    with tool_runtime_context(ctx):
        assert get_tool_runtime_context() is ctx
        with tool_runtime_context(None):
            assert get_tool_runtime_context() is None
        assert get_tool_runtime_context() is ctx
