"""Typed durable records for background Python script runs."""

from __future__ import annotations

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
    DECLINED = "declined"
    INDETERMINATE = "indeterminate"
    CANCELLED = "cancelled"


class ScriptRunEntityKind(StrEnum):
    """Kinds of runtime entity that may request a background script run."""

    AGENT = "agent"
    TEAM = "team"
    ROUTER = "router"  # noqa: Vulture


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
    entity_kind: ScriptRunEntityKind = ScriptRunEntityKind.AGENT
    thread_root_event_id: str | None = None
    execution_identity: dict[str, object] = field(default_factory=dict)
    worker_id: str | None = None
    supervisor_handle: str | None = None
    local_unsafe: bool = False
    state: ScriptRunState = ScriptRunState.STARTING
    created_at: str = field(default_factory=lambda: _utc_now())
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    cancel_requested_at: str | None = None
    cancellation_reason: str | None = None
    call_count: int = 0


@dataclass(frozen=True, slots=True)
class ScriptCallRecord:
    """Durable receipt for exactly one logical script tool call."""

    run_id: str
    call_id: str
    grant: ScriptToolGrant
    arguments_digest: str
    state: ScriptCallState
    created_at: str
    updated_at: str
    result: object | None = None
    error: object | None = None


@dataclass(frozen=True, slots=True)
class ScriptCallClaim:
    """Result of atomically claiming a logical script call."""

    call: ScriptCallRecord
    created: bool


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
