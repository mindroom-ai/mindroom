"""Contract tests for the OpenClaw compatibility toolkit surface."""

from __future__ import annotations

import json

import pytest

import mindroom.tools  # noqa: F401
from mindroom.custom_tools.openclaw_compat import OpenClawCompatTools
from mindroom.tools_metadata import TOOL_METADATA, get_tool_by_name

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


@pytest.mark.asyncio
async def test_openclaw_compat_placeholder_responses_are_json() -> None:
    """Verify placeholder responses follow a stable JSON structure."""
    tool = OpenClawCompatTools()
    responses = [
        ("agents_list", await tool.agents_list()),
        ("session_status", await tool.session_status()),
        ("sessions_list", await tool.sessions_list()),
        ("sessions_history", await tool.sessions_history("main")),
        ("sessions_send", await tool.sessions_send(message="hello", session_key="main")),
        ("sessions_spawn", await tool.sessions_spawn(task="do this")),
        ("subagents", await tool.subagents()),
        ("message", await tool.message(action="send", message="hi")),
        ("gateway", await tool.gateway(action="config.get")),
        ("nodes", await tool.nodes(action="status")),
        ("canvas", await tool.canvas(action="snapshot")),
    ]

    for expected_tool_name, raw_response in responses:
        payload = json.loads(raw_response)
        assert payload["tool"] == expected_tool_name
        assert payload["status"] == "not_implemented"
