"""Contract tests for the OpenClaw compatibility toolkit surface."""

from __future__ import annotations

import json
import sqlite3
from itertools import count
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call

import pytest

import mindroom.tools  # noqa: F401
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
    "gateway",
    "nodes",
    "canvas",
}

OPENCLAW_COMPAT_ALIAS_TOOLS = {
    "cron",
    "web_search",
    "web_fetch",
    "read",
    "write",
    "edit",
    "exec",
    "process",
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
    not_configured = [
        ("gateway", await tool.gateway(action="config.get")),
        ("nodes", await tool.nodes(action="status")),
        ("canvas", await tool.canvas(action="snapshot")),
    ]

    for expected_tool_name, raw_response in context_required:
        payload = json.loads(raw_response)
        assert payload["tool"] == expected_tool_name
        assert payload["status"] == "error"
        assert "message" in payload

    for expected_tool_name, raw_response in not_configured:
        payload = json.loads(raw_response)
        assert payload["tool"] == expected_tool_name
        assert payload["status"] == "not_configured"
        assert "message" in payload


@pytest.mark.asyncio
async def test_openclaw_compat_aliases_return_structured_results() -> None:
    """Verify alias tools are callable and return stable JSON payloads."""
    tool = OpenClawCompatTools()
    tool._duckduckgo.web_search = MagicMock(return_value='{"results": []}')
    tool._website.read_url = MagicMock(return_value='{"docs": []}')
    tool._file.functions["read_file"].entrypoint = MagicMock(return_value="hello world")
    tool._file.functions["read_file_chunk"].entrypoint = MagicMock(return_value="hello")
    tool._file.functions["save_file"].entrypoint = MagicMock(return_value="saved")
    tool._shell.functions["run_shell_command"].entrypoint = MagicMock(return_value="ok")
    tool._scheduler.schedule = AsyncMock(return_value="scheduled")

    web_search_payload = json.loads(await tool.web_search("mindroom"))
    assert web_search_payload["status"] == "ok"
    assert web_search_payload["tool"] == "web_search"

    web_fetch_payload = json.loads(await tool.web_fetch("https://example.com"))
    assert web_fetch_payload["status"] == "ok"
    assert web_fetch_payload["tool"] == "web_fetch"

    read_payload = json.loads(await tool.read(file_path="README.md", offset=2, limit=3))
    assert read_payload["status"] == "ok"
    assert read_payload["tool"] == "read"

    write_payload = json.loads(
        await tool.write(
            file_path="notes.txt",
            content=[{"type": "text", "text": "hello"}, {"kind": "text", "value": " world"}],
        ),
    )
    assert write_payload["status"] == "ok"
    assert write_payload["tool"] == "write"

    edit_payload = json.loads(
        await tool.edit(
            file_path="notes.txt",
            old_string=[{"type": "text", "text": "hello"}],
            new_string=[{"kind": "text", "value": "hi"}],
        ),
    )
    assert edit_payload["status"] == "ok"
    assert edit_payload["tool"] == "edit"

    exec_payload = json.loads(await tool.exec("echo hi"))
    assert exec_payload["status"] == "ok"
    assert exec_payload["tool"] == "exec"

    process_payload = json.loads(await tool.process("echo hi"))
    assert process_payload["status"] == "ok"
    assert process_payload["tool"] == "process"

    cron_payload = json.loads(await tool.cron("in 1 minute remind me to test"))
    assert cron_payload["status"] == "ok"
    assert cron_payload["tool"] == "cron"

    tool._file.functions["read_file_chunk"].entrypoint.assert_called_once_with("README.md", 1, 3)
    tool._file.functions["save_file"].entrypoint.assert_any_call("hello world", "notes.txt", True)
    tool._file.functions["save_file"].entrypoint.assert_any_call("hi world", "notes.txt", True)


@pytest.mark.asyncio
async def test_openclaw_compat_read_adaptive_paging_emits_continuation_hint() -> None:
    """Verify read auto-pages chunks and returns continuation hints when capped."""
    tool = OpenClawCompatTools()
    tool.READ_PAGE_LINE_LIMIT = 2
    tool.READ_MAX_PAGES = 2
    tool.READ_MAX_OUTPUT_BYTES = 1024

    read_chunk = MagicMock(side_effect=["a1\na2", "b1\nb2", "c1\nc2"])
    tool._file.functions["read_file_chunk"].entrypoint = read_chunk

    payload = json.loads(await tool.read(file_path="README.md"))

    assert payload["status"] == "ok"
    assert payload["tool"] == "read"
    assert payload["output_capped"] is True
    assert payload["continuation_offset"] == 5
    assert "Use offset=5 to continue." in payload["result"]
    read_chunk.assert_has_calls(
        [
            call("README.md", 0, 1),
            call("README.md", 2, 3),
        ],
    )
    assert read_chunk.call_count == 2


@pytest.mark.asyncio
async def test_openclaw_compat_read_validation_includes_retry_guidance() -> None:
    """Verify read validation errors include retry guidance to avoid call loops."""
    tool = OpenClawCompatTools()

    payload = json.loads(await tool.read())

    assert payload["status"] == "error"
    assert payload["tool"] == "read"
    assert payload["message"].endswith("Supply correct parameters before retrying.")


@pytest.mark.asyncio
async def test_openclaw_compat_edit_handles_bom_crlf_and_fuzzy_match() -> None:
    """Verify edit handles BOM/CRLF and fuzzy unicode punctuation matching."""
    tool = OpenClawCompatTools()
    tool._file.functions["read_file"].entrypoint = MagicMock(return_value="\ufeffalpha\r\nbeta\u2019s\r\ngamma\r\n")
    save_file = MagicMock(return_value="saved")
    tool._file.functions["save_file"].entrypoint = save_file

    payload = json.loads(
        await tool.edit(
            file_path="notes.txt",
            old_string="beta's",
            new_string="delta",
        ),
    )

    assert payload["status"] == "ok"
    assert payload["tool"] == "edit"
    assert payload["used_fuzzy_match"] is True
    assert payload["first_changed_line"] == 2
    assert isinstance(payload["diff"], str)
    assert payload["diff"]
    save_file.assert_called_once_with("\ufeffalpha\r\ndelta\r\ngamma\r\n", "notes.txt", True)


@pytest.mark.asyncio
async def test_openclaw_compat_edit_rejects_ambiguous_match_without_replace_all() -> None:
    """Verify edit requires unique oldText unless replace_all is explicitly set."""
    tool = OpenClawCompatTools()
    tool._file.functions["read_file"].entrypoint = MagicMock(return_value="x\nx\n")
    tool._file.functions["save_file"].entrypoint = MagicMock(return_value="saved")

    payload = json.loads(await tool.edit(path="dup.txt", old_text="x", new_text="z"))

    assert payload["status"] == "error"
    assert payload["tool"] == "edit"
    assert "must be unique" in payload["message"]


@pytest.mark.asyncio
async def test_openclaw_compat_edit_replace_all_applies_all_matches() -> None:
    """Verify edit replace_all updates every matching occurrence."""
    tool = OpenClawCompatTools()
    tool._file.functions["read_file"].entrypoint = MagicMock(return_value="x\nx\n")
    save_file = MagicMock(return_value="saved")
    tool._file.functions["save_file"].entrypoint = save_file

    payload = json.loads(await tool.edit(path="dup.txt", old_text="x", new_text="z", replace_all=True))

    assert payload["status"] == "ok"
    assert payload["tool"] == "edit"
    assert payload["replace_all"] is True
    assert payload["replacements"] == 2
    save_file.assert_called_once_with("z\nz\n", "dup.txt", True)


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
async def test_openclaw_compat_write_requires_file_tool_in_context(tmp_path: Path) -> None:
    """Verify write is blocked when the active agent does not enable file tool."""
    tool = OpenClawCompatTools()
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
        payload = json.loads(await tool.write(path="notes.txt", content="hello"))

    assert payload["status"] == "error"
    assert payload["tool"] == "write"
    assert "file tool is not enabled" in payload["message"]


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
