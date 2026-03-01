"""Contract tests for the OpenClaw compatibility toolkit surface."""

from __future__ import annotations

import inspect
import json
import os
from itertools import count
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.custom_tools import subagents as subagents_module
from mindroom.custom_tools.openclaw_compat import OpenClawCompatTools
from mindroom.custom_tools.subagents import SubAgentsTools
from mindroom.openclaw_context import OpenClawToolContext, get_openclaw_tool_context, openclaw_tool_context
from mindroom.thread_utils import create_session_id
from mindroom.tools_metadata import TOOL_METADATA, get_tool_by_name

if TYPE_CHECKING:
    from pathlib import Path

OPENCLAW_COMPAT_CORE_TOOLS = {
    "agents_list",
    "sessions_send",
    "sessions_spawn",
    "list_sessions",
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

SUBAGENT_TOOLSET_TOOLS = {
    "agents_list",
    "sessions_send",
    "sessions_spawn",
    "list_sessions",
}


async def call_openclaw_subagent_tool(tool: OpenClawCompatTools, name: str, **kwargs: object) -> str:
    """Invoke the sub-agent runtime entrypoint registered on OpenClaw compat."""
    entrypoint = tool.async_functions[name].entrypoint
    assert entrypoint is not None
    result = entrypoint(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    assert isinstance(result, str)
    return result


def test_openclaw_compat_tool_registered() -> None:
    """Verify metadata registration for the OpenClaw compatibility toolkit."""
    assert "openclaw_compat" in TOOL_METADATA
    metadata = TOOL_METADATA["openclaw_compat"]
    assert metadata.display_name == "OpenClaw Compat"


def test_openclaw_compat_tool_instantiates() -> None:
    """Verify the compatibility toolkit can be loaded from the registry."""
    tool = get_tool_by_name("openclaw_compat")
    assert isinstance(tool, OpenClawCompatTools)


def test_subagents_tool_registered() -> None:
    """Verify metadata registration for the reusable sub-agents toolkit."""
    assert "subagents" in TOOL_METADATA
    metadata = TOOL_METADATA["subagents"]
    assert metadata.display_name == "Sub-Agents"


def test_subagents_tool_instantiates() -> None:
    """Verify the reusable sub-agents toolkit can be loaded from the registry."""
    tool = get_tool_by_name("subagents")
    assert isinstance(tool, SubAgentsTools)


def test_subagents_tool_is_standalone() -> None:
    """Sub-agents toolkit should not inherit from OpenClaw compatibility toolkit."""
    assert not issubclass(SubAgentsTools, OpenClawCompatTools)


def test_openclaw_compat_core_tool_names_present() -> None:
    """Lock the core OpenClaw-compatible tool name contract."""
    tool = OpenClawCompatTools()
    exposed_names = {func.name for func in tool.functions.values()} | {
        func.name for func in tool.async_functions.values()
    }
    assert OPENCLAW_COMPAT_CORE_TOOLS.issubset(exposed_names)


def test_subagents_tool_names_present() -> None:
    """Lock the reusable sub-agents tool name contract."""
    tool = SubAgentsTools()
    exposed_names = {func.name for func in tool.functions.values()} | {
        func.name for func in tool.async_functions.values()
    }
    assert exposed_names == SUBAGENT_TOOLSET_TOOLS


def test_openclaw_compat_alias_tool_names_present() -> None:
    """Lock the alias OpenClaw-compatible tool name contract."""
    tool = OpenClawCompatTools()
    exposed_names = {func.name for func in tool.functions.values()} | {
        func.name for func in tool.async_functions.values()
    }
    assert OPENCLAW_COMPAT_ALIAS_TOOLS.issubset(exposed_names)


def test_openclaw_compat_uses_subagents_entrypoints() -> None:
    """OpenClaw compatibility should reuse the general sub-agents toolkit."""
    tool = OpenClawCompatTools()
    for entrypoint_name in SUBAGENT_TOOLSET_TOOLS:
        entrypoint = tool.async_functions[entrypoint_name].entrypoint
        assert entrypoint is not None
        assert getattr(entrypoint, "__self__", None) is tool._subagents_tools


@pytest.mark.asyncio
async def test_openclaw_compat_placeholder_responses_are_json() -> None:
    """Verify context-bound tools return structured errors without runtime context."""
    tool = OpenClawCompatTools()
    context_required = [
        ("agents_list", await call_openclaw_subagent_tool(tool, "agents_list")),
        (
            "sessions_send",
            await call_openclaw_subagent_tool(tool, "sessions_send", message="hello", session_key="main"),
        ),
        ("sessions_spawn", await call_openclaw_subagent_tool(tool, "sessions_spawn", task="do this")),
        ("list_sessions", await call_openclaw_subagent_tool(tool, "list_sessions")),
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
        payload = json.loads(await call_openclaw_subagent_tool(tool, "agents_list"))

    assert payload["status"] == "ok"
    assert payload["tool"] == "agents_list"
    assert payload["agents"] == ["code", "research"]


@pytest.mark.asyncio
async def test_openclaw_compat_message_send_does_not_force_current_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify message send stays room-level unless thread_id is explicitly provided."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
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
    send_mock.assert_awaited_once_with(
        ctx,
        room_id="!room:localhost",
        text="hello",
        thread_id=None,
    )


@pytest.mark.asyncio
async def test_openclaw_compat_message_reply_uses_context_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify replies default to the active context thread when none is passed."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
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
    send_mock.assert_awaited_once_with(
        ctx,
        room_id="!room:localhost",
        text="hello",
        thread_id="$ctx-thread:localhost",
    )


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_send_returns_error_when_matrix_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sessions_send returns an error payload when Matrix send fails."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
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
        payload = json.loads(await call_openclaw_subagent_tool(tool, "sessions_send", message="hello"))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_send"
    assert "Failed to send message" in payload["message"]


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_send_relays_original_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sessions_send relays requester identity for bot-authored dispatch events."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
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

    with openclaw_tool_context(ctx):
        payload = json.loads(await call_openclaw_subagent_tool(tool, "sessions_send", message="hello"))

    assert payload["status"] == "ok"
    send_mock.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        text="hello",
        thread_id="$ctx-thread:localhost",
        original_sender=ctx.requester_id,
    )


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_send_rejects_room_mode_threaded_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify threaded dispatch is rejected when target agent uses thread_mode=room."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    config.get_entity_thread_mode = MagicMock(return_value="room")
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@alice:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    target_session = create_session_id(ctx.room_id, "$worker-thread:localhost")

    with openclaw_tool_context(ctx):
        payload = json.loads(
            await call_openclaw_subagent_tool(
                tool,
                "sessions_send",
                message="hello",
                session_key=target_session,
                agent_id="openclaw",
            ),
        )

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_send"
    assert "thread_mode=room" in payload["message"]
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_send_label_resolves_to_tracked_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify label lookup resolves to a tracked session."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
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

    session_key = create_session_id(ctx.room_id, "$target:localhost")
    subagents_module._record_session(ctx, session_key=session_key, label="work", target_agent="openclaw")

    with openclaw_tool_context(ctx):
        payload = json.loads(await call_openclaw_subagent_tool(tool, "sessions_send", message="hello", label="work"))

    assert payload["status"] == "ok"
    assert payload["session_key"] == session_key
    send_mock.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        text="hello",
        thread_id="$target:localhost",
        original_sender=ctx.requester_id,
    )


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_send_label_prefers_most_recent_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify label lookup prefers the most recently touched in-scope session."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)

    epoch_counter = count(1)
    iso_counter = count(1)
    monkeypatch.setattr(subagents_module, "_now_epoch", lambda: float(next(epoch_counter)))
    monkeypatch.setattr(
        subagents_module,
        "_now_iso",
        lambda: f"2026-01-01T00:00:{next(iso_counter):02d}+00:00",
    )

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

    older_session = create_session_id(ctx.room_id, "$older:localhost")
    newer_session = create_session_id(ctx.room_id, "$newer:localhost")
    subagents_module._record_session(ctx, session_key=older_session, label="work", target_agent="openclaw")
    subagents_module._record_session(ctx, session_key=newer_session, label="work", target_agent="openclaw")

    with openclaw_tool_context(ctx):
        payload = json.loads(await call_openclaw_subagent_tool(tool, "sessions_send", message="hello", label="work"))

    assert payload["status"] == "ok"
    assert payload["session_key"] == newer_session
    send_mock.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        text="hello",
        thread_id="$newer:localhost",
        original_sender=ctx.requester_id,
    )


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_spawn_returns_error_when_matrix_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sessions_spawn returns an error payload when Matrix send fails."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
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
        payload = json.loads(await call_openclaw_subagent_tool(tool, "sessions_spawn", task="do thing"))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "Failed to send spawn message" in payload["message"]


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_spawn_relays_original_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify sessions_spawn relays requester identity for bot-authored dispatch events."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
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

    with openclaw_tool_context(ctx):
        payload = json.loads(await call_openclaw_subagent_tool(tool, "sessions_spawn", task="do thing"))

    assert payload["status"] == "ok"
    assert "session_key" in payload
    assert payload["target_agent"] == "openclaw"
    assert payload["event_id"] == "$event"
    assert "run_id" not in payload
    assert "parent_session_key" not in payload
    assert "run" not in payload
    send_mock.assert_awaited_once_with(
        ctx,
        room_id=ctx.room_id,
        text="@mindroom_openclaw do thing",
        thread_id=None,
        original_sender=ctx.requester_id,
    )


@pytest.mark.asyncio
async def test_openclaw_compat_sessions_spawn_rejects_room_mode_target_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify isolated spawn is rejected when target agent uses thread_mode=room."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value="$event")
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    config.get_entity_thread_mode = MagicMock(return_value="room")
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$ctx-thread:localhost",
        requester_id="@alice:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await call_openclaw_subagent_tool(tool, "sessions_spawn", task="do thing"))

    assert payload["status"] == "error"
    assert payload["tool"] == "sessions_spawn"
    assert "thread_mode=room" in payload["message"]
    send_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_openclaw_compat_message_send_returns_error_when_matrix_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify message send returns an error payload when Matrix send fails."""
    tool = OpenClawCompatTools()
    monkeypatch.setattr(subagents_module, "send_matrix_text", AsyncMock(return_value=None))
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
async def test_openclaw_compat_exec_returns_error_for_invalid_shell_syntax() -> None:
    """Verify malformed shell commands return a structured error payload."""
    tool = OpenClawCompatTools()
    payload = json.loads(await tool.exec('echo "unterminated'))
    assert payload["status"] == "error"
    assert payload["tool"] == "exec"
    assert "invalid shell command" in payload["message"]


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


@pytest.mark.asyncio
async def test_list_sessions_returns_tracked_sessions(tmp_path: Path) -> None:
    """Verify list_sessions returns sessions recorded by _record_session."""
    tool = OpenClawCompatTools()
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        requester_id="@alice:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    session_key = create_session_id(ctx.room_id, "$child:localhost")
    subagents_module._record_session(ctx, session_key=session_key, label="my-task", target_agent="code")

    with openclaw_tool_context(ctx):
        payload = json.loads(await call_openclaw_subagent_tool(tool, "list_sessions"))

    assert payload["status"] == "ok"
    assert payload["tool"] == "list_sessions"
    assert payload["total"] == 1
    session = payload["sessions"][0]
    assert session["session_key"] == session_key
    assert session["label"] == "my-task"
    assert session["target_agent"] == "code"


@pytest.mark.asyncio
async def test_list_sessions_empty_when_no_sessions(tmp_path: Path) -> None:
    """Verify list_sessions returns empty list when no sessions exist."""
    tool = OpenClawCompatTools()
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        requester_id="@alice:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )

    with openclaw_tool_context(ctx):
        payload = json.loads(await call_openclaw_subagent_tool(tool, "list_sessions"))

    assert payload["status"] == "ok"
    assert payload["sessions"] == []
    assert payload["total"] == 0


def test_load_registry_falls_back_to_legacy_openclaw_path(tmp_path: Path) -> None:
    """Verify _load_registry reads legacy openclaw/session_registry.json when needed."""
    config = MagicMock()
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id=None,
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    legacy_dir = tmp_path / "openclaw"
    legacy_dir.mkdir(parents=True)
    legacy_data = {
        "sessions": {
            "!room:localhost:$thread:localhost": {
                "label": "legacy-session",
                "target_agent": "code",
            },
        },
    }
    (legacy_dir / "session_registry.json").write_text(json.dumps(legacy_data))

    registry = subagents_module._load_registry(ctx)
    assert "!room:localhost:$thread:localhost" in registry
    assert registry["!room:localhost:$thread:localhost"]["label"] == "legacy-session"


def test_load_registry_migrates_old_format(tmp_path: Path) -> None:
    """Verify _load_registry extracts sessions from old {sessions: {}, runs: {}} format."""
    config = MagicMock()
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id=None,
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    registry_dir = tmp_path / "subagents"
    registry_dir.mkdir(parents=True)
    old_data = {
        "sessions": {
            "!room:localhost:$thread:localhost": {
                "label": "old-session",
                "target_agent": "code",
            },
        },
        "runs": {"run-1": {"status": "accepted"}},
    }
    (registry_dir / "session_registry.json").write_text(json.dumps(old_data))

    registry = subagents_module._load_registry(ctx)
    assert "!room:localhost:$thread:localhost" in registry
    assert registry["!room:localhost:$thread:localhost"]["label"] == "old-session"
    # Runs are discarded during migration
    assert "runs" not in registry


def test_record_session_updates_existing(tmp_path: Path) -> None:
    """Verify _record_session updates label on existing entry without overwriting other fields."""
    config = MagicMock()
    ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id=None,
        requester_id="@user:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    session_key = "!room:localhost:$thread:localhost"
    subagents_module._record_session(ctx, session_key=session_key, label="first", target_agent="code")
    subagents_module._record_session(ctx, session_key=session_key, label="second")

    registry = subagents_module._load_registry(ctx)
    assert registry[session_key]["label"] == "second"
    assert registry[session_key]["target_agent"] == "code"


def test_record_session_does_not_mutate_foreign_scope_entry(tmp_path: Path) -> None:
    """Verify out-of-scope updates do not mutate an existing tracked session entry."""
    config = MagicMock()
    owner_ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id=None,
        requester_id="@owner:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    foreign_ctx = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room:localhost",
        thread_id=None,
        requester_id="@foreign:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    session_key = "!room:localhost:$thread:localhost"

    subagents_module._record_session(owner_ctx, session_key=session_key, label="owner", target_agent="code")
    subagents_module._record_session(foreign_ctx, session_key=session_key, label="foreign", target_agent="research")

    registry = subagents_module._load_registry(owner_ctx)
    assert registry[session_key]["label"] == "owner"
    assert registry[session_key]["target_agent"] == "code"
    assert registry[session_key]["requester_id"] == "@owner:localhost"


@pytest.mark.asyncio
async def test_list_sessions_scoped_by_context(tmp_path: Path) -> None:
    """Verify list_sessions only returns sessions belonging to the active context scope."""
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
    session_a = create_session_id(ctx_a.room_id, "$child-a:localhost")
    session_b = create_session_id(ctx_b.room_id, "$child-b:localhost")
    subagents_module._record_session(ctx_a, session_key=session_a, label="alpha", target_agent="code")
    subagents_module._record_session(ctx_b, session_key=session_b, label="beta", target_agent="code")

    with openclaw_tool_context(ctx_a):
        payload_a = json.loads(await call_openclaw_subagent_tool(tool, "list_sessions"))
    with openclaw_tool_context(ctx_b):
        payload_b = json.loads(await call_openclaw_subagent_tool(tool, "list_sessions"))

    assert [s["session_key"] for s in payload_a["sessions"]] == [session_a]
    assert [s["session_key"] for s in payload_b["sessions"]] == [session_b]


@pytest.mark.asyncio
async def test_resolve_by_label_scoped_by_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify label lookup only resolves sessions from the active context scope."""
    tool = OpenClawCompatTools()
    send_mock = AsyncMock(return_value="$evt")
    monkeypatch.setattr(subagents_module, "send_matrix_text", send_mock)
    config = MagicMock()
    config.agents = {"openclaw": SimpleNamespace(tools=["shell"])}
    ctx_owner = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room-owner:localhost",
        thread_id="$thread-owner:localhost",
        requester_id="@alice:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    ctx_other = OpenClawToolContext(
        agent_name="openclaw",
        room_id="!room-other:localhost",
        thread_id="$thread-other:localhost",
        requester_id="@bob:localhost",
        client=MagicMock(),
        config=config,
        storage_path=tmp_path,
    )
    # Record session under the other context
    foreign_session = create_session_id(ctx_other.room_id, "$foreign:localhost")
    subagents_module._record_session(ctx_other, session_key=foreign_session, label="work", target_agent="code")

    # Try to resolve label "work" from the owner context — should NOT find the foreign session
    with openclaw_tool_context(ctx_owner):
        payload = json.loads(
            await call_openclaw_subagent_tool(tool, "sessions_send", message="hello", label="work"),
        )

    # Should fall back to current session, not the foreign one
    assert payload["status"] == "ok"
    assert payload["session_key"] != foreign_session
