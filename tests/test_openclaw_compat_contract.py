"""Contract tests for the OpenClaw compatibility toolkit surface."""

from __future__ import annotations

import json
import os
import sqlite3
from itertools import count
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mindroom.tools  # noqa: F401
from mindroom.attachments import register_local_attachment
from mindroom.custom_tools.openclaw_compat import OpenClawCompatTools
from mindroom.openclaw_context import OpenClawToolContext, get_openclaw_tool_context, openclaw_tool_context
from mindroom.thread_utils import create_session_id
from mindroom.tools_metadata import TOOL_METADATA, get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path

OPENCLAW_COMPAT_CORE_TOOLS = {
    "agents_list",
    "session_status",
    "sessions_list",
    "sessions_history",
    "sessions_send",
    "sessions_spawn",
    "subagents",
    "message",
}

OPENCLAW_COMPAT_ALIAS_TOOLS = {
    "browser",
    "cron",
    "web_search",
    "web_fetch",
    "exec",
    "process",
    "read_file",
    "edit_file",
    "write_file",
    "grep",
    "find_files",
    "ls",
}


def test_openclaw_compat_tool_registered() -> None:
    """Verify metadata registration for the OpenClaw compatibility toolkit."""
    assert "openclaw_compat" in TOOL_METADATA
    metadata = TOOL_METADATA["openclaw_compat"]
    assert metadata.display_name == "OpenClaw Compat"


def test_openclaw_compat_tool_instantiates() -> None:
    """Verify the compatibility toolkit can be loaded from the registry."""
    tool = get_tool_by_name("openclaw_compat")
    assert isinstance(tool, OpenClawCompatTools)


def test_openclaw_compat_core_tool_names_present() -> None:
    """Lock the core OpenClaw-compatible tool name contract."""
    tool = OpenClawCompatTools()
    exposed_names = {func.name for func in tool.functions.values()} | {
        func.name for func in tool.async_functions.values()
    }
    assert OPENCLAW_COMPAT_CORE_TOOLS.issubset(exposed_names)


def test_openclaw_compat_alias_tool_names_present() -> None:
    """Lock the alias OpenClaw-compatible tool name contract."""
    tool = OpenClawCompatTools()
    exposed_names = {func.name for func in tool.functions.values()} | {
        func.name for func in tool.async_functions.values()
    }
    assert OPENCLAW_COMPAT_ALIAS_TOOLS.issubset(exposed_names)


@pytest.mark.asyncio
async def test_openclaw_compat_placeholder_responses_are_json() -> None:
    """Verify context-bound tools return structured errors without runtime context."""
    tool = OpenClawCompatTools()
    context_required = [
        ("agents_list", await tool.agents_list()),
        ("session_status", await tool.session_status()),
        ("sessions_list", await tool.sessions_list()),
        ("sessions_history", await tool.sessions_history("main")),
        ("sessions_send", await tool.sessions_send(message="hello", session_key="main")),
        ("sessions_spawn", await tool.sessions_spawn(task="do this")),
        ("subagents", await tool.subagents()),
        ("message", await tool.message(action="send", message="hi")),
    ]

    for expected_tool_name, raw_response in context_required:
        payload = json.loads(raw_response)
        assert payload["tool"] == expected_tool_name
        assert payload["status"] == "error"
        assert "message" in payload


@pytest.mark.asyncio
async def test_openclaw_compat_aliases_return_structured_results() -> None:
    """Verify alias tools are callable and return stable JSON payloads."""
    tool = OpenClawCompatTools()
    tool._duckduckgo.web_search = MagicMock(return_value='{"results": []}')
    tool._website.read_url = MagicMock(return_value='{"docs": []}')
    tool._shell.functions["run_shell_command"].entrypoint = MagicMock(return_value="ok")
    tool._scheduler.schedule = AsyncMock(return_value="scheduled")
    browser_entrypoint = AsyncMock(return_value='{"status":"ok","action":"status"}')
    tool._browser_tool = SimpleNamespace(
        async_functions={"browser": SimpleNamespace(entrypoint=browser_entrypoint)},
        functions={},
    )

    web_search_payload = json.loads(await tool.web_search("mindroom"))
    assert web_search_payload["status"] == "ok"
    assert web_search_payload["tool"] == "web_search"

    web_fetch_payload = json.loads(await tool.web_fetch("https://example.com"))
    assert web_fetch_payload["status"] == "ok"
    assert web_fetch_payload["tool"] == "web_fetch"

    browser_payload = json.loads(
        await tool.browser(
            action="status",
            target="node",
            node="node-1",
            profile="openclaw",
        ),
    )
    assert browser_payload["status"] == "ok"
    assert browser_payload["tool"] == "browser"
    browser_entrypoint.assert_awaited_once_with(
        action="status",
        node="node-1",
        profile="openclaw",
        target="node",
    )

    exec_payload = json.loads(await tool.exec("echo hi"))
    assert exec_payload["status"] == "ok"
    assert exec_payload["tool"] == "exec"

    process_payload = json.loads(await tool.process("echo hi"))
    assert process_payload["status"] == "ok"
    assert process_payload["tool"] == "process"

    cron_payload = json.loads(await tool.cron("in 1 minute remind me to test"))
    assert cron_payload["status"] == "ok"
    assert cron_payload["tool"] == "cron"


def test_openclaw_compat_coding_read_file(tmp_path: Path) -> None:
    """Verify read_file delegates to CodingTools and returns structured payload."""
    tool = OpenClawCompatTools()
    test_file = tmp_path / "hello.txt"
    test_file.write_text("line1\nline2\n")
    tool._coding.base_dir = tmp_path

    payload = json.loads(tool.read_file(str(test_file)))
    assert payload["status"] == "ok"
    assert payload["tool"] == "read_file"
    assert "line1" in payload["result"]


def test_openclaw_compat_coding_write_file(tmp_path: Path) -> None:
    """Verify write_file delegates to CodingTools and returns structured payload."""
    tool = OpenClawCompatTools()
    tool._coding.base_dir = tmp_path
    target = tmp_path / "out.txt"

    payload = json.loads(tool.write_file(str(target), "hello"))
    assert payload["status"] == "ok"
    assert payload["tool"] == "write_file"
    assert target.read_text() == "hello"


def test_openclaw_compat_coding_edit_file(tmp_path: Path) -> None:
    """Verify edit_file delegates to CodingTools and returns structured payload."""
    tool = OpenClawCompatTools()
    tool._coding.base_dir = tmp_path
    target = tmp_path / "edit.txt"
    target.write_text("old content here")

    payload = json.loads(tool.edit_file(str(target), "old content", "new content"))
    assert payload["status"] == "ok"
    assert payload["tool"] == "edit_file"
    assert "new content here" in target.read_text()


def test_openclaw_compat_coding_grep(tmp_path: Path) -> None:
    """Verify grep delegates to CodingTools and returns structured payload."""
    tool = OpenClawCompatTools()
    tool._coding.base_dir = tmp_path
    (tmp_path / "sample.py").write_text("def hello():\n    pass\n")

    payload = json.loads(tool.grep("hello", literal=True))
    assert payload["status"] == "ok"
    assert payload["tool"] == "grep"
    assert "hello" in payload["result"]


def test_openclaw_compat_coding_grep_error_prefixed_filename_is_ok(tmp_path: Path) -> None:
    """Verify grep does not misclassify successful results from Error* filenames."""
    tool = OpenClawCompatTools()
    tool._coding.base_dir = tmp_path
    (tmp_path / "Error.py").write_text("needle\n")

    payload = json.loads(tool.grep("needle", literal=True))
    assert payload["status"] == "ok"
    assert payload["tool"] == "grep"
    assert "Error.py" in payload["result"]


def test_openclaw_compat_coding_find_files(tmp_path: Path) -> None:
    """Verify find_files delegates to CodingTools and returns structured payload."""
    tool = OpenClawCompatTools()
    tool._coding.base_dir = tmp_path
    (tmp_path / "foo.py").write_text("")

    payload = json.loads(tool.find_files("*.py"))
    assert payload["status"] == "ok"
    assert payload["tool"] == "find_files"
    assert "foo.py" in payload["result"]


def test_openclaw_compat_coding_find_files_error_prefixed_filename_is_ok(tmp_path: Path) -> None:
    """Verify find_files does not misclassify successful Error* filename matches."""
    tool = OpenClawCompatTools()
    tool._coding.base_dir = tmp_path
    (tmp_path / "Error.py").write_text("")

    payload = json.loads(tool.find_files("*.py"))
    assert payload["status"] == "ok"
    assert payload["tool"] == "find_files"
    assert "Error.py" in payload["result"]


def test_openclaw_compat_coding_ls(tmp_path: Path) -> None:
    """Verify ls delegates to CodingTools and returns structured payload."""
    tool = OpenClawCompatTools()
    tool._coding.base_dir = tmp_path
    (tmp_path / "a.txt").write_text("")
    (tmp_path / "subdir").mkdir()

    payload = json.loads(tool.ls())
    assert payload["status"] == "ok"
    assert payload["tool"] == "ls"
    assert "a.txt" in payload["result"]
    assert "subdir/" in payload["result"]


def test_openclaw_compat_coding_ls_error_prefixed_filename_is_ok(tmp_path: Path) -> None:
    """Verify ls does not misclassify successful results from Error* filenames."""
    tool = OpenClawCompatTools()
    tool._coding.base_dir = tmp_path
    (tmp_path / "Error notes.txt").write_text("")

    payload = json.loads(tool.ls())
    assert payload["status"] == "ok"
    assert payload["tool"] == "ls"
    assert "Error notes.txt" in payload["result"]


def test_openclaw_compat_coding_read_file_error() -> None:
    """Verify read_file returns error payload for missing files."""
    tool = OpenClawCompatTools()
    payload = json.loads(tool.read_file("/nonexistent/path/file.txt"))
    assert payload["status"] == "error"
    assert payload["tool"] == "read_file"


def test_openclaw_compat_merge_paths_prepends_and_dedupes() -> None:
    """Verify login-shell PATH entries are prepended without duplicates."""
    merged = OpenClawCompatTools._merge_paths(
        existing_path=f"/usr/bin{os.pathsep}/bin",
        shell_path=f"/custom/bin{os.pathsep}/usr/bin",
    )
    assert merged == f"/custom/bin{os.pathsep}/usr/bin{os.pathsep}/bin"


def test_openclaw_compat_ensure_login_shell_path_applies_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify PATH bootstrap probes login shell once and caches the result."""
    monkeypatch.setenv("PATH", f"/existing/bin{os.pathsep}/usr/bin")
    monkeypatch.setattr(OpenClawCompatTools, "_login_shell_path_loaded", False)
    monkeypatch.setattr(OpenClawCompatTools, "_login_shell_path_applied", False)
    monkeypatch.setattr(OpenClawCompatTools, "_login_shell_path", None)

    call_counter = {"count": 0}

    def _fake_read_login_shell_path(_cls: type[OpenClawCompatTools]) -> str:
        call_counter["count"] += 1
        return f"/custom/bin{os.pathsep}/other/bin"

    monkeypatch.setattr(
        OpenClawCompatTools,
        "_read_login_shell_path",
        classmethod(_fake_read_login_shell_path),
    )

    OpenClawCompatTools._ensure_login_shell_path()
    first_path = os.environ["PATH"]
    OpenClawCompatTools._ensure_login_shell_path()

    assert call_counter["count"] == 1
    assert first_path == f"/custom/bin{os.pathsep}/other/bin{os.pathsep}/existing/bin{os.pathsep}/usr/bin"
    assert os.environ["PATH"] == first_path


def test_openclaw_compat_ensure_login_shell_path_retries_after_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify transient probe failures do not permanently disable future PATH bootstrap."""
    original_path = f"/existing/bin{os.pathsep}/usr/bin"
    monkeypatch.setenv("PATH", original_path)
    monkeypatch.setattr(OpenClawCompatTools, "_login_shell_path_loaded", False)
    monkeypatch.setattr(OpenClawCompatTools, "_login_shell_path_applied", False)
    monkeypatch.setattr(OpenClawCompatTools, "_login_shell_path", None)

    call_counter = {"count": 0}
    responses = [None, f"/custom/bin{os.pathsep}/other/bin"]

    def _fake_read_login_shell_path(_cls: type[OpenClawCompatTools]) -> str | None:
        call_counter["count"] += 1
        return responses.pop(0)

    monkeypatch.setattr(
        OpenClawCompatTools,
        "_read_login_shell_path",
        classmethod(_fake_read_login_shell_path),
    )

    OpenClawCompatTools._ensure_login_shell_path()
    assert call_counter["count"] == 1
    assert os.environ["PATH"] == original_path
    assert OpenClawCompatTools._login_shell_path_loaded is False
    assert OpenClawCompatTools._login_shell_path_applied is False

    OpenClawCompatTools._ensure_login_shell_path()
    assert call_counter["count"] == 2
    assert os.environ["PATH"] == f"/custom/bin{os.pathsep}/other/bin{os.pathsep}/existing/bin{os.pathsep}/usr/bin"
    assert OpenClawCompatTools._login_shell_path_loaded is True
    assert OpenClawCompatTools._login_shell_path_applied is True


@pytest.mark.asyncio
async def test_openclaw_compat_exec_uses_shell_tool_entrypoint() -> None:
    """Verify exec dispatches via shell toolkit function entrypoint."""
    tool = OpenClawCompatTools()
    tool._shell.run_shell_command = MagicMock(side_effect=AssertionError("direct method should not be used"))
    entrypoint = MagicMock(return_value="ok")
    tool._shell.functions["run_shell_command"].entrypoint = entrypoint

    payload = json.loads(await tool.exec("echo hi"))

    assert payload["status"] == "ok"
    assert payload["tool"] == "exec"
    entrypoint.assert_called_once_with(["echo", "hi"])
    tool._shell.run_shell_command.assert_not_called()


@pytest.mark.asyncio
async def test_openclaw_compat_exec_bootstraps_login_shell_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify exec calls login-shell PATH bootstrap before running shell command."""
    tool = OpenClawCompatTools()
    call_counter = {"count": 0}

    def _fake_ensure(_cls: type[OpenClawCompatTools]) -> None:
        call_counter["count"] += 1

    monkeypatch.setattr(OpenClawCompatTools, "_ensure_login_shell_path", classmethod(_fake_ensure))
    entrypoint = MagicMock(return_value="ok")
    tool._shell.functions["run_shell_command"].entrypoint = entrypoint

    payload = json.loads(await tool.exec("echo hi"))

    assert payload["status"] == "ok"
    assert payload["tool"] == "exec"
    assert call_counter["count"] == 1
    entrypoint.assert_called_once_with(["echo", "hi"])


@pytest.mark.asyncio
async def test_openclaw_compat_agents_list_with_runtime_context(tmp_path: Path) -> None:
    """Verify context-bound tools can read runtime context when provided."""
    tool = OpenClawCompatTools()
    config = MagicMock()
    config.agents = {"code": MagicMock(), "research": MagicMock()}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.agents_list())

    assert payload["status"] == "ok"
    assert payload["tool"] == "agents_list"
    assert payload["agents"] == ["code", "research"]


@pytest.mark.asyncio
async def test_openclaw_compat_message_send_does_not_force_current_thread(tmp_path: Path) -> None:
    """Verify message send stays room-level unless thread_id is explicitly provided."""
    tool = OpenClawCompatTools()
    tool._send_matrix_text = AsyncMock(return_value="$evt")
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.message(action="send", message="hello"))

    assert payload["status"] == "ok"
    assert payload["tool"] == "message"
    assert payload["thread_id"] is None
    tool._send_matrix_text.assert_awaited_once_with(
        ctx,
        room_id="!room:localhost",
        text="hello",
        thread_id=None,
    )


@pytest.mark.asyncio
async def test_openclaw_compat_message_reply_uses_context_thread(tmp_path: Path) -> None:
    """Verify replies default to the active context thread when none is passed."""
    tool = OpenClawCompatTools()
    tool._send_matrix_text = AsyncMock(return_value="$evt")
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.message(action="reply", message="hello"))

    assert payload["status"] == "ok"
    assert payload["tool"] == "message"
    assert payload["thread_id"] == "$ctx-thread:localhost"
    tool._send_matrix_text.assert_awaited_once_with(
        ctx,
        room_id="!room:localhost",
        text="hello",
        thread_id="$ctx-thread:localhost",
    )


@pytest.mark.asyncio
async def test_openclaw_compat_message_attachments_lists_context_ids(tmp_path: Path) -> None:
    """Verify message attachments action returns current-context attachment metadata."""
    tool = OpenClawCompatTools()
    sample_file = tmp_path / "sample.txt"
    sample_file.write_text("hello", encoding="utf-8")
    attachment = register_local_attachment(
        tmp_path,
        sample_file,
        kind="file",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        source_event_id="$root",
        sender="@user:localhost",
    )
    assert attachment is not None

    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
        attachment_ids=(attachment.attachment_id,),
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.message(action="attachments"))

    assert payload["status"] == "ok"
    assert payload["action"] == "attachments"
    assert payload["attachment_ids"] == [attachment.attachment_id]
    assert payload["attachments"][0]["attachment_id"] == attachment.attachment_id
    assert payload["attachments"][0]["available"] is True
    assert payload["attachments"][0]["local_path"] == str(sample_file.resolve())


@pytest.mark.asyncio
async def test_openclaw_compat_message_send_supports_attachment_only(tmp_path: Path) -> None:
    """Verify message send can upload files without a text body."""
    tool = OpenClawCompatTools()
    tool._send_matrix_text = AsyncMock()
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    sample_file = tmp_path / "upload.txt"
    sample_file.write_text("payload", encoding="utf-8")

    with (
        openclaw_tool_context(ctx),
        patch("mindroom.custom_tools.attachments.send_file_message", new=AsyncMock(return_value="$file_evt")),
    ):
        payload = json.loads(await tool.message(action="send", attachments=[str(sample_file)]))

    assert payload["status"] == "ok"
    assert payload["event_id"] == "$file_evt"
    assert payload["attachment_event_ids"] == ["$file_evt"]
    tool._send_matrix_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_send_returns_error_when_matrix_send_fails(tmp_path: Path) -> None:
    """Verify sessions_send returns an error payload when Matrix send fails."""
    tool = OpenClawCompatTools()
    tool._send_matrix_text = AsyncMock(return_value=None)
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.sessions_send(message="hello"))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_send"
    assert "Failed to send message" in payload["message"]


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_list_scopes_registry_entries_by_context(tmp_path: Path) -> None:
    """Verify sessions_list only returns registry sessions from the active context scope."""
    tool = OpenClawCompatTools()
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx_a = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room-a:localhost",
        thread_id="$thread-a:localhost",
        requester_id="@alice:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    ctx_b = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room-b:localhost",
        thread_id="$thread-b:localhost",
        requester_id="@bob:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    session_a = create_session_id(ctx_a.room_id, "$thread-a:localhost")
    session_b = create_session_id(ctx_b.room_id, "$thread-b:localhost")
    tool._touch_session(ctx_a, session_key=session_a, kind="thread", label="alpha", status="active")
    tool._touch_session(ctx_b, session_key=session_b, kind="thread", label="beta", status="active")

    with openclaw_tool_context(ctx_a):
        payload_a = json.loads(await tool.sessions_list())
    with openclaw_tool_context(ctx_b):
        payload_b = json.loads(await tool.sessions_list())

    assert [session["session_key"] for session in payload_a["sessions"]] == [session_a]
    assert [session["session_key"] for session in payload_b["sessions"]] == [session_b]


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_send_label_prefers_latest_in_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify label lookup is scoped and chooses the most recently updated matching session."""
    tool = OpenClawCompatTools()
    tool._send_matrix_text = AsyncMock(return_value="$evt")
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@alice:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    other_ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$other-thread:localhost",
        requester_id="@bob:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    epoch_counter = count(1)
    iso_counter = count(1)
    monkeypatch.setattr(
        OpenClawCompatTools,
        "_now_epoch",
        staticmethod(lambda: next(epoch_counter)),
    )
    monkeypatch.setattr(
        OpenClawCompatTools,
        "_now_iso",
        staticmethod(lambda: f"2026-01-01T00:00:{next(iso_counter):02d}+00:00"),
    )

    outsider_session = create_session_id(ctx.room_id, "$outside:localhost")
    older_session = create_session_id(ctx.room_id, "$older:localhost")
    newer_session = create_session_id(ctx.room_id, "$newer:localhost")
    tool._touch_session(other_ctx, session_key=outsider_session, kind="thread", label="work", status="active")
    tool._touch_session(ctx, session_key=older_session, kind="thread", label="work", status="active")
    tool._touch_session(ctx, session_key=newer_session, kind="thread", label="work", status="active")

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.sessions_send(message="hello", label="work"))

    assert payload["status"] == "ok"
    assert payload["session_key"] == newer_session
    tool._send_matrix_text.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        text="hello",
        thread_id="$newer:localhost",
    )


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_spawn_returns_error_when_matrix_send_fails(tmp_path: Path) -> None:
    """Verify sessions_spawn returns an error payload when Matrix send fails."""
    tool = OpenClawCompatTools()
    tool._send_matrix_text = AsyncMock(return_value=None)
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.sessions_spawn(task="do thing"))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "Failed to send spawn message" in payload["message"]


@pytest.mark.asyncio
async def test_openclaw_compat_subagents_kill_all_scopes_to_context(tmp_path: Path) -> None:
    """Verify kill-all only updates runs belonging to the active context scope."""
    tool = OpenClawCompatTools()
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx_a = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$thread-a:localhost",
        requester_id="@alice:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    ctx_b = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$thread-b:localhost",
        requester_id="@bob:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    tool._track_run(
        ctx_a,
        run_id="run-a",
        session_key=create_session_id(ctx_a.room_id, "$thread-a:localhost"),
        task="task a",
        target_agent="openclaw",
        status="accepted",
        event_id="$event-a",
    )
    tool._track_run(
        ctx_b,
        run_id="run-b",
        session_key=create_session_id(ctx_b.room_id, "$thread-b:localhost"),
        task="task b",
        target_agent="openclaw",
        status="accepted",
        event_id="$event-b",
    )

    with openclaw_tool_context(ctx_a):
        kill_payload = json.loads(await tool.subagents(action="kill", target="all"))
        list_a_payload = json.loads(await tool.subagents(action="list"))
    with openclaw_tool_context(ctx_b):
        list_b_payload = json.loads(await tool.subagents(action="list"))

    assert kill_payload["status"] == "ok"
    assert kill_payload["updated"] == ["run-a"]
    assert [run["run_id"] for run in list_a_payload["runs"]] == ["run-a"]
    assert [run["status"] for run in list_a_payload["runs"]] == ["killed"]
    assert [run["run_id"] for run in list_b_payload["runs"]] == ["run-b"]
    assert [run["status"] for run in list_b_payload["runs"]] == ["accepted"]


@pytest.mark.asyncio
async def test_openclaw_compat_message_send_returns_error_when_matrix_send_fails(tmp_path: Path) -> None:
    """Verify message send returns an error payload when Matrix send fails."""
    tool = OpenClawCompatTools()
    tool._send_matrix_text = AsyncMock(return_value=None)
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.message(action="send", message="hello"))

    assert payload["status"] == "error"
    assert payload["tool"] == "message"
    assert payload["action"] == "send"
    assert "Failed to send message" in payload["message"]


@pytest.mark.asyncio
async def test_openclaw_compat_subagents_steer_does_not_mark_run_steered_on_dispatch_error(
    tmp_path: Path,
) -> None:
    """Verify steer keeps run state unchanged when dispatch fails."""
    tool = OpenClawCompatTools()
    tool._send_matrix_text = AsyncMock(return_value=None)
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@alice:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    tool._track_run(
        ctx,
        run_id="run-1",
        session_key=create_session_id(ctx.room_id, "$thread-1:localhost"),
        task="task",
        target_agent="openclaw",
        status="accepted",
        event_id="$event-1",
    )

    with openclaw_tool_context(ctx):
        steer_payload = json.loads(await tool.subagents(action="steer", target="run-1", message="continue"))
        list_payload = json.loads(await tool.subagents(action="list"))

    assert steer_payload["status"] == "error"
    assert steer_payload["dispatch"]["status"] == "error"
    assert [run["run_id"] for run in list_payload["runs"]] == ["run-1"]
    assert [run["status"] for run in list_payload["runs"]] == ["accepted"]


@pytest.mark.asyncio
async def test_openclaw_compat_exec_returns_error_for_invalid_shell_syntax() -> None:
    """Verify malformed shell commands return a structured error payload."""
    tool = OpenClawCompatTools()
    payload = json.loads(await tool.exec('echo "unterminated'))
    assert payload["status"] == "error"
    assert payload["tool"] == "exec"
    assert "invalid shell command" in payload["message"]


@pytest.mark.asyncio
async def test_openclaw_compat_exec_requires_shell_tool_in_context(tmp_path: Path) -> None:
    """Verify exec is blocked when the active agent does not enable shell tool."""
    tool = OpenClawCompatTools()
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["file"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.exec("echo hi"))

    assert payload["status"] == "error"
    assert payload["tool"] == "exec"
    assert "shell tool is not enabled" in payload["message"]


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_history_mixed_timestamp_types_sorted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify mixed matrix/db timestamp formats are sorted without type errors."""
    tool = OpenClawCompatTools()
    room_id = "!room:localhost"
    thread_id = "$thread:localhost"
    session_key = create_session_id(room_id, thread_id)

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    db_path = sessions_dir / "openclaw.db"
    runs = json.dumps(
        [
            {
                "run_id": "run-1",
                "status": "completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "content_type": "text",
                "content": "db entry",
                "input": "hello",
            },
        ],
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            'CREATE TABLE "openclaw_sessions" (session_id TEXT, session_type TEXT, user_id TEXT, created_at INTEGER, updated_at INTEGER, runs TEXT)',
        )
        conn.execute(
            'INSERT INTO "openclaw_sessions" VALUES (?, ?, ?, ?, ?, ?)',
            (session_key, "thread", "@user:localhost", 1735689600, 1735689600, runs),
        )
        conn.commit()

    async def _fake_thread_history(
        _client: object,
        passed_room_id: str,
        passed_thread_id: str,
    ) -> list[dict[str, object]]:
        assert passed_room_id == room_id
        assert passed_thread_id == thread_id
        return [
            {
                "event_id": "$evt",
                "sender": "@user:localhost",
                "timestamp": 1735689600000,
                "body": "matrix entry",
            },
        ]

    monkeypatch.setattr("mindroom.custom_tools.openclaw_compat.fetch_thread_history", _fake_thread_history)

    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id=room_id,
        thread_id=thread_id,
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await tool.sessions_history(session_key=session_key, limit=10))

    assert payload["status"] == "ok"
    assert [entry["source"] for entry in payload["history"]] == ["matrix_thread", "agent_db"]


def test_openclaw_compat_read_agent_sessions_handles_missing_table(tmp_path: Path) -> None:
    """Verify session reads tolerate sqlite files without the expected session table."""
    tool = OpenClawCompatTools()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    with sqlite3.connect(sessions_dir / "openclaw.db") as conn:
        conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY)")
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    assert tool._read_agent_sessions(ctx) == []


def test_openclaw_context_readable_inside_context_manager(tmp_path: Path) -> None:
    """Verify the runtime context is accessible inside the context manager."""
    ctx = OpenClawToolContext(
        agent_name="test",
        room_id="!room:localhost",
        thread_id=None,
        requester_id="@user:localhost",
        client=MagicMock(),
        config=MagicMock(),
        storage_path=tmp_path,
    )
    assert get_openclaw_tool_context() is None
    with openclaw_tool_context(ctx):
        got = get_openclaw_tool_context()
        assert got is ctx
        assert got.agent_name == "test"
        assert got.thread_id is None
    assert get_openclaw_tool_context() is None
