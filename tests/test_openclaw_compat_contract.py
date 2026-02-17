"""Contract tests for the OpenClaw compatibility toolkit surface."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.custom_tools.openclaw_compat import OpenClawCompatTools
from mindroom.openclaw_context import OpenClawToolContext, get_openclaw_tool_context, openclaw_tool_context
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
    tool._shell.run_shell_command = MagicMock(return_value="ok")
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
