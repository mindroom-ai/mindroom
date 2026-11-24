"""The names Claude Code offers and calls MindRoom's tools by."""

from __future__ import annotations

import hashlib
import re

__all__ = ["SERVER_NAME", "TOOL_PREFIX", "called_name", "offered_name"]

SERVER_NAME = "mindroom"
# Claude Code names an MCP tool ``mcp__<server>__<tool>`` and takes at most 64 characters of letters, digits, ``_``
# and ``-``.
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
_NAME_LIMIT = 64 - len(TOOL_PREFIX)
_HASH_LENGTH = 10


def offered_name(name: str) -> str:
    """Return the name Claude Code is offered ``name`` under: itself, or a cut and hashed one when it cannot take it."""
    if len(name) <= _NAME_LIMIT and re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:_HASH_LENGTH]
    return f"{re.sub(r'[^a-zA-Z0-9_-]', '_', name)[: _NAME_LIMIT - _HASH_LENGTH - 1]}_{digest}"


def called_name(name: str) -> str:
    """Return the full name Claude calls the tool ``name`` by."""
    return f"{TOOL_PREFIX}{offered_name(name)}"
