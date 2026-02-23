"""Backward-compatible aliases for OpenClaw tool context helpers."""

from __future__ import annotations

from mindroom.session_tools_context import (
    SessionToolsContext,
    get_session_tools_context,
    session_tools_context,
)

OpenClawToolContext = SessionToolsContext
get_openclaw_tool_context = get_session_tools_context
openclaw_tool_context = session_tools_context
