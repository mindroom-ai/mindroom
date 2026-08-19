"""Typed durable records for background Python script runs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class ScriptRunState(StrEnum):
    """Durable lifecycle states for one background script run."""

    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ScriptCallState(StrEnum):
    """Durable lifecycle states for one logical governed tool call."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ScriptToolGrant:
    """One permitted toolkit/function pair captured at script launch."""

    toolkit_name: str
    function_name: str


@dataclass(frozen=True, slots=True)
class ScriptRunRecord:
    """Durable primary-owned state for one background script run."""

    run_id: str
    agent_name: str
    owner_user_id: str
    room_id: str
    source_digest: str
    grants: tuple[ScriptToolGrant, ...]
    token_hash: str
    preapprove_launch_grants: bool = False
    thread_root_event_id: str | None = None
    execution_identity: dict[str, object] = field(default_factory=dict)
    worker_key: str | None = None
    worker_id: str | None = None
    snapshot_locator: str | None = None
    name: str | None = None
    local_unsafe: bool = False
    max_tool_calls_per_minute: int = 30
    max_runtime_seconds: int = 24 * 60 * 60
    state: ScriptRunState = ScriptRunState.STARTING
    created_at: str = field(default_factory=lambda: _utc_now())
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    cancel_requested_at: str | None = None
    cancellation_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScriptCallRecord:
    """Durable receipt for exactly one logical script tool call."""

    run_id: str
    call_id: str
    grant: ScriptToolGrant
    arguments_digest: str
    state: ScriptCallState
    created_at: str
    result: object | None = None
    error: object | None = None


@dataclass(frozen=True, slots=True)
class ScriptCallClaim:
    """Result of atomically claiming a logical script call."""

    call: ScriptCallRecord
    created: bool


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


_SCRIPT_RUN_ID_RE = re.compile(r"script-([0-9a-f]{32})")


def supervisor_handle_for_run(run_id: str) -> str:
    """Derive the sole valid shell-supervisor handle for a generated script run."""
    match = _SCRIPT_RUN_ID_RE.fullmatch(run_id)
    if match is None:
        msg = "Script run ID must be script- followed by 32 lowercase hexadecimal characters."
        raise ValueError(msg)
    return f"shell:{match.group(1)}"


def script_worker_key_for_run(base_worker_key: str, run_id: str) -> str:
    """Derive one run-pinned user-agent worker key while keeping the agent final."""
    if _SCRIPT_RUN_ID_RE.fullmatch(run_id) is None:
        msg = "Script run ID must be script- followed by 32 lowercase hexadecimal characters."
        raise ValueError(msg)
    parts = base_worker_key.split(":")
    if len(parts) < 5 or parts[0] != "v1" or parts[2] != "user_agent" or not parts[-1]:
        msg = "Background scripts require a resolved user-agent worker key."
        raise ValueError(msg)
    return ":".join((*parts[:-1], run_id, parts[-1]))
