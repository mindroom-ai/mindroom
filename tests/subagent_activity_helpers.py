"""Shared builders for the sub-agent activity tests."""

from __future__ import annotations

from typing import Any

from mindroom.plugin_api import SubagentSnapshot


def make_snapshot(**overrides: Any) -> SubagentSnapshot:  # noqa: ANN401
    """Return a running sub-agent snapshot, with any field overridden."""
    fields: dict[str, Any] = {
        "delegation_id": "d1",
        "subagent_id": "s1",
        "parent_delegation_id": None,
        "caller_agent_name": "helper",
        "agent_name": "researcher",
        "source": "delegate",
        "task": "Find the answer",
        "room_id": "!room:example.org",
        "thread_id": None,
        "requester_id": "@alice:example.org",
        "model_name": "default",
        "status": "running",
        "started_at": 1000.0,
        "updated_at": 1000.0,
        "ended_at": None,
        "items": (),
        "items_dropped": 0,
        "result_preview": None,
    }
    fields.update(overrides)
    return SubagentSnapshot(**fields)
