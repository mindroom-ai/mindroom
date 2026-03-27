"""Live test plugin for tool interception hooks."""

from __future__ import annotations

import json
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)

if TYPE_CHECKING:
    from pathlib import Path

_BLOCKED_PATTERN = "*secret*"
_EVENTS_FILE = "tool-events.jsonl"


def _events_path(state_root: Path) -> Path:
    return state_root / _EVENTS_FILE


def _append_event(state_root: Path, payload: dict[str, Any]) -> None:
    events_path = _events_path(state_root)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _candidate_path(arguments: dict[str, Any]) -> str:
    for key in ("path", "file_name", "filename"):
        value = arguments.get(key)
        if value is not None:
            return str(value)
    return ""


@hook(EVENT_TOOL_BEFORE_CALL, priority=10)
async def decline_secret_read_file(ctx: ToolBeforeCallContext) -> None:
    """Block read_file for paths containing the word secret."""
    if ctx.tool_name != "read_file":
        return

    path = _candidate_path(ctx.arguments)
    if not fnmatch(path, _BLOCKED_PATTERN):
        return

    reason = f"read_file is blocked for sensitive paths matching {_BLOCKED_PATTERN!r}"
    ctx.logger.info("Declining tool call", tool_name=ctx.tool_name, path=path, reason=reason)
    ctx.decline(reason)


@hook(EVENT_TOOL_AFTER_CALL, priority=10)
async def record_tool_outcome(ctx: ToolAfterCallContext) -> None:
    """Persist one line of evidence for every observed tool call outcome."""
    payload = {
        "agent_name": ctx.agent_name,
        "arguments": dict(ctx.arguments),
        "blocked": ctx.blocked,
        "duration_ms": round(ctx.duration_ms, 2),
        "error": None if ctx.error is None else type(ctx.error).__name__,
        "requester_id": ctx.requester_id,
        "room_id": ctx.room_id,
        "session_id": ctx.session_id,
        "thread_id": ctx.thread_id,
        "tool_name": ctx.tool_name,
    }
    _append_event(ctx.state_root, payload)
    ctx.logger.info("Observed tool outcome", **payload)
