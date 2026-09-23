"""Cycle-free memory scope identifier helpers."""

from __future__ import annotations

_AGENT_SCOPE_PREFIX = "agent_"


def agent_scope_user_id(agent_name: str) -> str:
    """Return the scoped memory user ID for one agent."""
    return f"{_AGENT_SCOPE_PREFIX}{agent_name}"


def agent_name_from_scope_user_id(scope_user_id: str) -> str | None:
    """Extract the agent name from an agent scope user ID."""
    if scope_user_id.startswith(_AGENT_SCOPE_PREFIX):
        return scope_user_id[len(_AGENT_SCOPE_PREFIX) :]
    return None
