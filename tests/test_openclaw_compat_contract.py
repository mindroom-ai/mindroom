"""Contract tests for the OpenClaw compatibility toolkit surface."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

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
    tool._shell.functions["run_shell_command"].entrypoint = MagicMock(return_value="ok")
    tool._scheduler.schedule = AsyncMock(return_value="scheduled")

    web_search_payload = json.loads(await tool.web_search("mindroom"))
    assert web_search_payload["status"] == "ok"
    assert web_search_payload["tool"] == "web_search"

    web_fetch_payload = json.loads(await tool.web_fetch("https://example.com"))
    assert web_fetch_payload["status"] == "ok"
    assert web_fetch_payload["tool"] == "web_fetch"

    exec_payload = json.loads(await tool.exec("echo hi"))
    assert exec_payload["status"] == "ok"
    assert exec_payload["tool"] == "exec"

    process_payload = json.loads(await tool.process("echo hi"))
    assert process_payload["status"] == "ok"
    assert process_payload["tool"] == "process"

    cron_payload = json.loads(await tool.cron("in 1 minute remind me to test"))
    assert cron_payload["status"] == "ok"
    assert cron_payload["tool"] == "cron"


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


def test_openclaw_context_readable_inside_context_manager() -> None:
    """Verify the runtime context is accessible inside the context manager."""
    ctx = OpenClawToolContext(
        agent_name="test",
        room_id="!room:localhost",
        thread_id=None,
        requester_id="@user:localhost",
        client=MagicMock(),
        config=MagicMock(),
        storage_path=MagicMock(),
    )
    assert get_openclaw_tool_context() is None
    with openclaw_tool_context(ctx):
        got = get_openclaw_tool_context()
        assert got is ctx
        assert got.agent_name == "test"
        assert got.thread_id is None
    assert get_openclaw_tool_context() is None
