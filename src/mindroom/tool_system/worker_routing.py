"""Generic worker-routing primitives for tool execution."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.constants import resolve_config_relative_path

if TYPE_CHECKING:
    from collections.abc import Iterator

WorkerScope = Literal["shared", "user", "user_agent"]
_ExecutionChannel = Literal["matrix", "openai_compat"]

_WORKER_DIRNAME_MAX_PREFIX_LENGTH = 80
_AGENT_WORKSPACE_DIRNAME = "workspace"
_ABSOLUTE_AGENT_PATH_DIRNAME = "_absolute"
SHARED_ONLY_INTEGRATION_NAMES = frozenset(
    {
        "google",
        "spotify",
        "homeassistant",
        "gmail",
        "google_calendar",
        "google_sheets",
    },
)


@dataclass(frozen=True)
class ToolExecutionIdentity:
    """Serializable execution identity used for worker resolution."""

    channel: _ExecutionChannel
    agent_name: str
    requester_id: str | None
    room_id: str | None
    thread_id: str | None
    resolved_thread_id: str | None
    session_id: str | None
    tenant_id: str | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class AgentOwnedPath:
    """Resolved canonical agent-owned path information."""

    resolved_path: Path
    state_root: Path


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


def _normalize_worker_dir_part(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._@+-]+", "_", value.strip()).strip("_")
    return normalized or "worker"


def _identity_requester_key(identity: ToolExecutionIdentity) -> str | None:
    if identity.requester_id:
        return _normalize_worker_key_part(identity.requester_id)
    return None


def worker_scope_allows_shared_only_integrations(worker_scope: WorkerScope | None) -> bool:
    """Return whether a worker scope can use shared-only dashboard integrations."""
    return worker_scope in (None, "shared")


def requires_shared_only_integration_scope(name: str) -> bool:
    """Return whether a tool or dashboard integration is restricted to shared scope."""
    return name in SHARED_ONLY_INTEGRATION_NAMES


def unsupported_shared_only_integration_message(
    name: str,
    worker_scope: WorkerScope | None,
    *,
    agent_name: str | None = None,
    subject: str = "Integration",
) -> str:
    """Return the user-facing error for shared-only integrations on isolating scopes."""
    scope_label = worker_scope or "unscoped"
    agent_detail = f"Agent '{agent_name}' uses " if agent_name else "This request uses "
    return (
        f"{subject} '{name}' is only supported for shared deployment credentials or agents with "
        f"worker_scope=shared. {agent_detail}worker_scope={scope_label}."
    )


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
        msg = f"Unknown worker scope: {worker_scope}"
        raise ValueError(msg)

    return worker_key


def resolve_unscoped_worker_key(
    *,
    agent_name: str,
    execution_identity: ToolExecutionIdentity | None = None,
) -> str:
    """Derive a stable backend worker key for unscoped sandbox execution."""
    identity = execution_identity or get_tool_execution_identity()
    tenant_key = _normalize_worker_key_part(
        identity.tenant_id
        if identity is not None and identity.tenant_id is not None
        else (
            identity.account_id
            if identity is not None and identity.account_id is not None
            else os.getenv("CUSTOMER_ID") or os.getenv("ACCOUNT_ID") or "default"
        ),
    )
    effective_agent_name = _normalize_worker_key_part(agent_name)
    return f"v1:{tenant_key}:unscoped:{effective_agent_name}"


def is_unscoped_worker_key(worker_key: str) -> bool:
    """Return whether a worker key uses the unscoped backend worker form."""
    parts = worker_key.split(":")
    return len(parts) >= 4 and parts[0] == "v1" and parts[2] == "unscoped"


def resolve_execution_identity_for_worker_scope(
    worker_scope: WorkerScope | None,
    *,
    agent_name: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
) -> ToolExecutionIdentity | None:
    """Resolve the execution identity used for worker scope decisions.

    Shared-scope state can be resolved from agent identity plus tenant/account
    even when no live request context exists yet. Isolating scopes still
    require an active execution identity.
    """
    if execution_identity is not None:
        return execution_identity

    current_identity = get_tool_execution_identity()
    if current_identity is not None:
        return current_identity

    if worker_scope != "shared" or agent_name is None:
        return None

    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id=os.getenv("CUSTOMER_ID"),
        account_id=os.getenv("ACCOUNT_ID"),
    )


def worker_dir_name(worker_key: str) -> str:
    """Return a stable filesystem-safe dirname for a worker key."""
    prefix = _normalize_worker_dir_part(worker_key)
    prefix = prefix[:_WORKER_DIRNAME_MAX_PREFIX_LENGTH].rstrip("._-")
    if not prefix:
        prefix = "worker"
    digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def worker_root_path(base_storage_path: Path, worker_key: str) -> Path:
    """Return the persistent state root path for a worker key."""
    resolved_base_path = shared_storage_root(base_storage_path)
    workers_dir = (
        resolved_base_path.parent if resolved_base_path.parent.name == "workers" else resolved_base_path / "workers"
    )
    return workers_dir / worker_dir_name(worker_key)


def shared_storage_root(base_storage_path: Path) -> Path:
    """Return the shared storage root even when passed a nested worker/agent path."""
    resolved_base_path = base_storage_path.expanduser().resolve()
    if resolved_base_path.parent.name in {"workers", "agents"}:
        return resolved_base_path.parent.parent
    return resolved_base_path


def agent_state_root_path(base_storage_path: Path, agent_name: str) -> Path:
    """Return the canonical shared state root for one agent."""
    return shared_storage_root(base_storage_path) / "agents" / _normalize_worker_dir_part(agent_name)


def agent_workspace_root_path(base_storage_path: Path, agent_name: str) -> Path:
    """Return the canonical workspace root for one agent."""
    return agent_state_root_path(base_storage_path, agent_name) / _AGENT_WORKSPACE_DIRNAME


def _bootstrap_marker_path(state_root: Path, target_path: Path) -> Path:
    relative_target = target_path.relative_to(state_root.resolve())
    marker_root = state_root / ".mindroom_bootstrap"
    if target_path.suffix:
        return marker_root / relative_target.parent / f"{relative_target.name}.seeded"
    return marker_root / relative_target / ".seeded"


def _copy_bootstrap_source(source_path: Path, target_path: Path) -> None:
    if source_path.is_dir():
        target_path.mkdir(parents=True, exist_ok=True)
        for child in source_path.iterdir():
            _copy_bootstrap_source(child, target_path / child.name)
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)


def _bootstrap_missing_path(source_path: Path, target_path: Path, *, state_root: Path) -> None:
    """Seed config-side starter files into a missing canonical agent-owned path once.

    The copy under ``agents/<agent>/workspace`` is authoritative after bootstrap.
    Later config edits do not overwrite canonical runtime state automatically.
    """
    marker_path = _bootstrap_marker_path(state_root, target_path)
    if marker_path.exists():
        return

    if target_path.exists():
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.touch()
        return

    if not source_path.exists():
        return

    _copy_bootstrap_source(source_path, target_path)
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.touch()


def _canonical_agent_workspace_relative_path(path_text: str, *, source_path: Path) -> Path:
    raw_path = Path(path_text).expanduser()
    if not raw_path.is_absolute():
        return raw_path

    target_parts = [_ABSOLUTE_AGENT_PATH_DIRNAME]
    if source_path.anchor not in {"", "/", "\\"}:
        target_parts.append(_normalize_worker_dir_part(source_path.anchor.replace(":", "")))

    relative_parts = list(source_path.parts)
    if source_path.anchor and relative_parts and relative_parts[0] == source_path.anchor:
        relative_parts = relative_parts[1:]
    return Path(*target_parts, *relative_parts)


def _resolve_agent_workspace_target(relative_path: Path, *, agent_root: Path) -> Path:
    candidate = (agent_root / relative_path).resolve()
    resolved_root = agent_root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        msg = f"Agent-owned paths must stay within {resolved_root}: {relative_path}"
        raise ValueError(msg) from exc
    return candidate


def resolve_agent_owned_path(
    path_text: str,
    *,
    agent_name: str,
    base_storage_path: Path,
    state_root: Path | None = None,
) -> AgentOwnedPath:
    """Resolve one agent-owned path into the canonical shared agent workspace.

    Durable agent files are shared per agent across all requesters and worker scopes.
    ``worker_scope`` only changes which runtime executes the tool call, not which
    workspace copy is authoritative.
    """
    source_path = resolve_config_relative_path(path_text)
    if state_root is None:
        state_root = agent_state_root_path(base_storage_path, agent_name)

    relative_target = _canonical_agent_workspace_relative_path(path_text, source_path=source_path)
    agent_workspace_root = agent_workspace_root_path(base_storage_path, agent_name).resolve()
    target_path = _resolve_agent_workspace_target(relative_target, agent_root=agent_workspace_root)
    _bootstrap_missing_path(source_path, target_path, state_root=state_root)
    return AgentOwnedPath(
        resolved_path=target_path,
        state_root=state_root,
    )


def resolve_agent_state_storage_path(
    *,
    agent_name: str,
    base_storage_path: Path,
) -> Path:
    """Return the canonical durable state root for one agent.

    Requester-scoped worker runtimes do not partition file-backed memory, mem0 state,
    sessions, or learning. All durable agent state lives under one root per agent.
    """
    return agent_state_root_path(base_storage_path, agent_name)
