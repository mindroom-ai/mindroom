"""Pinned native Playwright tool surface; safe to load on the primary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_EXCLUDED_TOOLS = frozenset({"browser_run_code_unsafe", "browser_install", "browser_route", "browser_unroute"})


def browser_mcp_catalog() -> dict[str, dict[str, Any]]:
    """Load independent native schemas without launching any runtime resources."""
    tools = json.loads(Path(__file__).with_suffix(".json").read_text())
    return {tool["name"]: tool for tool in tools}


def verify_browser_mcp_catalog(tools: list[dict[str, Any]]) -> None:
    """Refuse discovery drift, including unexpected capabilities and duplicate names."""
    filtered = [tool for tool in tools if tool["name"] not in _EXCLUDED_TOOLS]
    actual = {tool["name"]: {key: tool[key] for key in ("name", "description", "inputSchema")} for tool in filtered}
    if len(actual) != len(filtered) or actual != browser_mcp_catalog():
        msg = "Pinned browser MCP catalog does not match the installed server."
        raise RuntimeError(msg)
