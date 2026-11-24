"""Cycle-free memory scope identifier helpers."""

from __future__ import annotations

_AGENT_SCOPE_PREFIX = "agent_"


def agent_scope_user_id(agent_name: str) -> str:
    """Return the scoped memory user ID for one agent."""
    return f"{_AGENT_SCOPE_PREFIX}{agent_name}"
