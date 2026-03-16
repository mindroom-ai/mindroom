"""Generic worker-routing primitives for tool execution."""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

WorkerScope = Literal["shared", "user", "user_agent", "room_thread"]
ResolvedWorkerKeyScope = Literal["shared", "user", "user_agent", "room_thread", "unscoped"]
_ExecutionChannel = Literal["matrix", "openai_compat"]

_WORKER_DIRNAME_MAX_PREFIX_LENGTH = 80
_AGENT_WORKSPACE_DIRNAME = "workspace"
_PRIVATE_INSTANCE_ROOT_DIRNAME = "private_instances"
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
    normalized = re.sub(r"[^a-zA-Z0-9._@+-]+", "_", value.strip()).strip("_")
    return normalized or "default"


def _normalize_worker_requester_part(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._:@+-]+", "_", value.strip()).strip("_")
    return normalized or "default"


def _normalize_worker_dir_part(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._@+-]+", "_", value.strip()).strip("_")
    return normalized or "worker"


def _identity_requester_key(identity: ToolExecutionIdentity) -> str | None:
    if identity.requester_id:
        return _normalize_worker_requester_part(identity.requester_id)
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
    elif worker_scope == "room_thread":
        room_key = _normalize_worker_key_part(identity.room_id or "default")
        thread_key = _normalize_worker_key_part(identity.resolved_thread_id or identity.thread_id or "main")
        worker_key = f"v1:{tenant_key}:room_thread:{room_key}:{thread_key}:{effective_agent_name}"
    else:
        msg = f"Unknown worker scope: {worker_scope}"
        raise ValueError(msg)

    return worker_key


def resolve_unscoped_worker_key(
    *,
    agent_name: str,
    execution_identity: ToolExecutionIdentity | None = None,
    tenant_id: str | None = None,
    account_id: str | None = None,
) -> str:
    """Derive a stable backend worker key for unscoped sandbox execution."""
    identity = execution_identity or get_tool_execution_identity()
    tenant_key = _normalize_worker_key_part(
        tenant_id
        or (identity.tenant_id if identity is not None and identity.tenant_id is not None else None)
        or account_id
        or (identity.account_id if identity is not None and identity.account_id is not None else None)
        or "default",
    )
    effective_agent_name = _normalize_worker_key_part(agent_name)
    return f"v1:{tenant_key}:unscoped:{effective_agent_name}"


def is_unscoped_worker_key(worker_key: str) -> bool:
    """Return whether a worker key uses the unscoped backend worker form."""
    parts = worker_key.split(":")
    return len(parts) >= 4 and parts[0] == "v1" and parts[2] == "unscoped"


def resolved_worker_key_scope(worker_key: str) -> ResolvedWorkerKeyScope | None:
    """Return the parsed scope discriminator for one resolved worker key."""
    parts = worker_key.split(":")
    if len(parts) < 4 or parts[0] != "v1":
        return None
    scope = parts[2]
    if scope not in {"shared", "user", "user_agent", "room_thread", "unscoped"}:
        return None
    return cast("ResolvedWorkerKeyScope", scope)


def worker_key_agent_name(worker_key: str) -> str | None:
    """Return the encoded agent name for one resolved worker key, when present."""
    scope = resolved_worker_key_scope(worker_key)
    if scope is None or scope == "user":
        return None

    parts = worker_key.split(":")
    min_parts_by_scope = {
        "shared": 4,
        "unscoped": 4,
        "user_agent": 5,
        "room_thread": 6,
    }
    min_parts = min_parts_by_scope.get(scope)
    if min_parts is None or len(parts) < min_parts:
        return None
    return parts[3] if scope in {"shared", "unscoped"} else parts[-1]


def resolve_execution_identity_for_worker_scope(
    worker_scope: WorkerScope | None,
    *,
    agent_name: str | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    tenant_id: str | None = None,
    account_id: str | None = None,
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
        tenant_id=tenant_id,
        account_id=account_id,
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
    """Return the persistent runtime root path for a worker key."""
    resolved_base_path = shared_storage_root(base_storage_path)
    if _is_resolved_worker_root(resolved_base_path, worker_key):
        return resolved_base_path
    return resolved_base_path / "workers" / worker_dir_name(worker_key)


def shared_storage_root(base_storage_path: Path) -> Path:
    """Return the canonical shared storage root.

    Callers must pass the actual shared storage root, not an `agents/<name>` or
    `workers/<name>` child path. Security-sensitive path checks should fail closed
    rather than guess by peeling path segments based only on directory names.
    """
    return base_storage_path.expanduser().resolve()


def agent_state_root_path(base_storage_path: Path, agent_name: str) -> Path:
    """Return the canonical shared state root for one agent.

    Agent-state resolution accepts the shared storage root or a pre-resolved
    canonical agent root.
    """
    resolved_base_path = shared_storage_root(base_storage_path)
    if _is_resolved_agent_state_root(resolved_base_path, agent_name):
        return resolved_base_path
    return resolved_base_path / "agents" / _normalize_worker_dir_part(agent_name)


def private_instance_scope_root_path(base_storage_path: Path, worker_key: str) -> Path:
    """Return the canonical shared root for one worker-scoped private-instance namespace."""
    resolved_base_path = shared_storage_root(base_storage_path)
    if _is_resolved_private_instance_scope_root(resolved_base_path, worker_key):
        return resolved_base_path
    return resolved_base_path / _PRIVATE_INSTANCE_ROOT_DIRNAME / worker_dir_name(worker_key)


def private_instance_state_root_path(
    base_storage_path: Path,
    *,
    worker_key: str,
    agent_name: str,
) -> Path:
    """Return the canonical durable state root for one private agent instance."""
    return private_instance_scope_root_path(base_storage_path, worker_key) / _normalize_worker_dir_part(agent_name)


def _is_resolved_agent_state_root(path: Path, agent_name: str) -> bool:
    resolved_path = path.expanduser().resolve()
    return resolved_path.parent.name == "agents" and resolved_path.name == _normalize_worker_dir_part(agent_name)


def _is_resolved_private_instance_scope_root(path: Path, worker_key: str) -> bool:
    resolved_path = path.expanduser().resolve()
    return resolved_path.parent.name == _PRIVATE_INSTANCE_ROOT_DIRNAME and resolved_path.name == worker_dir_name(
        worker_key,
    )


def _is_resolved_worker_root(path: Path, worker_key: str) -> bool:
    resolved_path = path.expanduser().resolve()
    return resolved_path.parent.name == "workers" and resolved_path.name == worker_dir_name(worker_key)


def visible_state_roots_for_worker_key(base_storage_path: Path, worker_key: str) -> tuple[Path, ...]:
    """Return the canonical durable state roots a worker key is allowed to see by default.

    Shared agent roots remain canonical for normal agents.
    Private-instance roots live under a separate shared-storage namespace keyed by
    worker scope so they are durable without becoming worker-owned state.
    `user` intentionally sees the shared `agents/` tree plus its own
    private-instance namespace because it acts as a per-requester multi-agent
    workstation.
    """
    scope = resolved_worker_key_scope(worker_key)
    if scope is None:
        return ()
    if scope == "user":
        return (
            shared_storage_root(base_storage_path) / "agents",
            private_instance_scope_root_path(base_storage_path, worker_key),
        )

    agent_name = worker_key_agent_name(worker_key)
    if agent_name is None:
        return ()
    visible_roots = [agent_state_root_path(base_storage_path, agent_name)]
    if scope in {"user_agent", "room_thread"}:
        visible_roots.append(private_instance_scope_root_path(base_storage_path, worker_key))
    return tuple(visible_roots)


def agent_workspace_root_path(base_storage_path: Path, agent_name: str) -> Path:
    """Return the canonical workspace root for one agent."""
    return agent_state_root_path(base_storage_path, agent_name) / _AGENT_WORKSPACE_DIRNAME


def agent_workspace_relative_path(path_text: str) -> Path:
    """Validate and normalize a path that must live inside an agent workspace."""
    normalized_text = path_text.strip()
    if not normalized_text:
        msg = "Agent-owned paths must not be empty."
        raise ValueError(msg)
    if "$" in normalized_text:
        msg = f"Agent-owned paths must be workspace-relative literals, not env-variable references: {path_text}"
        raise ValueError(msg)

    candidate = Path(normalized_text).expanduser()
    if candidate.is_absolute():
        msg = f"Agent-owned paths must be workspace-relative, not absolute: {path_text}"
        raise ValueError(msg)

    if ".." in candidate.parts:
        msg = f"Agent-owned paths must stay within the agent workspace: {path_text}"
        raise ValueError(msg)

    return candidate


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
) -> Path:
    """Resolve one agent-owned path into the canonical shared agent workspace.

    Durable agent files are shared per agent across all requesters and worker scopes.
    ``worker_scope`` only changes which runtime executes the tool call, not which
    files are authoritative.
    """
    relative_target = agent_workspace_relative_path(path_text)
    agent_workspace_root = agent_workspace_root_path(base_storage_path, agent_name).resolve()
    return _resolve_agent_workspace_target(relative_target, agent_root=agent_workspace_root)


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
