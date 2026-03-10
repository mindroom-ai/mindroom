"""Generic worker-routing primitives for tool execution."""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.config.main import Config

WorkerScope = Literal["shared", "user", "user_agent", "room_thread"]
ExecutionChannel = Literal["matrix", "openai_compat"]

_WORKER_DIRNAME_MAX_PREFIX_LENGTH = 80


@dataclass(frozen=True)
class ToolExecutionIdentity:
    """Serializable execution identity used for worker resolution."""

    channel: ExecutionChannel
    agent_name: str
    requester_id: str | None
    room_id: str | None
    thread_id: str | None
    resolved_thread_id: str | None
    session_id: str | None
    tenant_id: str | None = None
    account_id: str | None = None


_TOOL_EXECUTION_IDENTITY: ContextVar[ToolExecutionIdentity | None] = ContextVar(
    "tool_execution_identity",
    default=None,
)


def get_tool_execution_identity() -> ToolExecutionIdentity | None:
    """Return the current tool execution identity."""
    return _TOOL_EXECUTION_IDENTITY.get()


@contextmanager
def tool_execution_identity(identity: ToolExecutionIdentity | None) -> Iterator[None]:
    """Set the current tool execution identity for the active execution scope."""
    token = _TOOL_EXECUTION_IDENTITY.set(identity)
    try:
        yield
    finally:
        _TOOL_EXECUTION_IDENTITY.reset(token)


def _normalize_worker_key_part(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._:@+-]+", "_", value.strip()).strip("_")
    return normalized or "default"


def _identity_requester_key(identity: ToolExecutionIdentity) -> str | None:
    if identity.requester_id:
        return _normalize_worker_key_part(identity.requester_id)
    if identity.channel == "openai_compat" and identity.session_id:
        # Best-effort fallback for OpenAI-compatible clients that do not supply
        # a trusted user identifier. This keeps scoped workers usable for the
        # prototype while preserving stable routing across repeated session IDs.
        return _normalize_worker_key_part(f"session:{identity.session_id}")
    return None


def resolve_worker_key(
    worker_scope: WorkerScope,
    identity: ToolExecutionIdentity,
    *,
    agent_name: str | None = None,
) -> str | None:
    """Derive a stable worker key from scope and execution identity."""
    tenant_key = _normalize_worker_key_part(identity.tenant_id or identity.account_id or "default")
    effective_agent_name = _normalize_worker_key_part(agent_name or identity.agent_name)
    worker_key: str | None

    if worker_scope == "shared":
        worker_key = f"v1:{tenant_key}:shared:{effective_agent_name}"
    elif worker_scope == "user":
        requester_key = _identity_requester_key(identity)
        if requester_key is None:
            return None
        worker_key = f"v1:{tenant_key}:user:{requester_key}"
    elif worker_scope == "user_agent":
        requester_key = _identity_requester_key(identity)
        if requester_key is None:
            return None
        worker_key = f"v1:{tenant_key}:user_agent:{requester_key}:{effective_agent_name}"
    else:
        room_key = identity.room_id
        if room_key is None:
            if identity.channel == "openai_compat" and identity.session_id:
                room_key = f"session:{identity.session_id}"
            else:
                return None

        thread_key = identity.resolved_thread_id or identity.thread_id or room_key
        worker_key = (
            f"v1:{tenant_key}:room_thread:"
            f"{_normalize_worker_key_part(room_key)}:"
            f"{_normalize_worker_key_part(thread_key)}"
        )

    return worker_key


def worker_dir_name(worker_key: str) -> str:
    """Return a stable filesystem-safe dirname for a worker key."""
    prefix = _normalize_worker_key_part(worker_key)
    prefix = prefix[:_WORKER_DIRNAME_MAX_PREFIX_LENGTH].rstrip("._-")
    if not prefix:
        prefix = "worker"
    digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def worker_root_path(base_storage_path: Path, worker_key: str) -> Path:
    """Return the persistent state root path for a worker key."""
    return base_storage_path.expanduser().resolve() / "workers" / worker_dir_name(worker_key)


def resolve_agent_worker_root(
    *,
    agent_name: str,
    base_storage_path: Path,
    config: Config,
) -> Path | None:
    """Resolve the current worker root for an agent when worker routing is active."""
    if agent_name not in config.agents:
        return None

    worker_scope = config.get_agent_worker_scope(agent_name)
    if worker_scope is None:
        return None

    identity = get_tool_execution_identity()
    if identity is None:
        return None

    worker_key = resolve_worker_key(worker_scope, identity, agent_name=agent_name)
    if worker_key is None:
        return None

    return worker_root_path(base_storage_path, worker_key)


def resolve_agent_memory_storage_path(
    *,
    agent_name: str,
    base_storage_path: Path,
    config: Config,
) -> Path:
    """Return the storage path that should back the agent's mutable memory state."""
    return (
        resolve_agent_worker_root(
            agent_name=agent_name,
            base_storage_path=base_storage_path,
            config=config,
        )
        or base_storage_path
    )
