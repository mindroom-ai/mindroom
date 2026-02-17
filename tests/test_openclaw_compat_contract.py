"""Contract tests for the OpenClaw compatibility toolkit surface."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import mindroom.tools  # noqa: F401
from mindroom.custom_tools.openclaw_compat import OpenClawCompatTools
from mindroom.openclaw_context import OpenClawToolContext, get_openclaw_tool_context, openclaw_tool_context
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


@pytest.mark.xfail(reason="Alias tools not yet implemented (Phase 6)")
def test_openclaw_compat_alias_tool_names_present() -> None:
    """Lock the alias OpenClaw-compatible tool name contract."""
    tool = OpenClawCompatTools()
    exposed_names = {func.name for func in tool.functions.values()} | {
        func.name for func in tool.async_functions.values()
    }
    assert OPENCLAW_COMPAT_ALIAS_TOOLS.issubset(exposed_names)


@pytest.mark.asyncio
async def test_openclaw_compat_placeholder_responses_are_json() -> None:
    """Verify placeholder responses follow a stable JSON structure."""
    tool = OpenClawCompatTools()
    responses = [
        ("agents_list", {}, await tool.agents_list()),
        ("session_status", {"session_key", "model"}, await tool.session_status()),
        ("sessions_list", {"kinds", "limit", "active_minutes", "message_limit"}, await tool.sessions_list()),
        ("sessions_history", {"session_key", "limit", "include_tools"}, await tool.sessions_history("main")),
        (
            "sessions_send",
            {"message", "session_key", "label", "agent_id", "timeout_seconds"},
            await tool.sessions_send(message="hello", session_key="main"),
        ),
        (
            "sessions_spawn",
            {"task", "label", "agent_id", "model", "run_timeout_seconds", "timeout_seconds", "cleanup"},
            await tool.sessions_spawn(task="do this"),
        ),
        ("subagents", {"action", "target", "message", "recent_minutes"}, await tool.subagents()),
        (
            "message",
            {"action", "message", "channel", "target", "thread_id"},
            await tool.message(action="send", message="hi"),
        ),
        ("gateway", {"action", "raw", "base_hash", "note"}, await tool.gateway(action="config.get")),
        ("nodes", {"action", "node"}, await tool.nodes(action="status")),
        ("canvas", {"action", "node", "target", "url", "java_script"}, await tool.canvas(action="snapshot")),
    ]

    for expected_tool_name, expected_arg_keys, raw_response in responses:
        payload = json.loads(raw_response)
        assert payload["tool"] == expected_tool_name
        assert payload["status"] == "not_implemented"
        if expected_arg_keys:
            assert "args" in payload, f"{expected_tool_name} should include args"
            assert set(payload["args"].keys()) == expected_arg_keys, (
                f"{expected_tool_name} args mismatch: {set(payload['args'].keys())} != {expected_arg_keys}"
            )


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
