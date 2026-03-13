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

    from mindroom.config.main import Config

WorkerScope = Literal["shared", "user", "user_agent"]
_ExecutionChannel = Literal["matrix", "openai_compat"]

_WORKER_DIRNAME_MAX_PREFIX_LENGTH = 80
_WORKER_BACKEND_ENV = "MINDROOM_WORKER_BACKEND"
_DEDICATED_WORKER_KEY_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"
_AGENT_WORKSPACE_DIRNAME = "workspace"
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
    state_root: Path | None
    worker_relative_path: str | None


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


def _normalized_worker_backend_name() -> str:
    raw = os.getenv(_WORKER_BACKEND_ENV, "").strip().lower()
    if raw in {"k8s", "kubernetes"}:
        return "kubernetes"
    return raw


def _uses_unscoped_dedicated_worker_roots() -> bool:
    return _normalized_worker_backend_name() == "kubernetes" or bool(os.getenv(_DEDICATED_WORKER_KEY_ENV, "").strip())


def worker_owned_tool_paths_use_relative_overrides() -> bool:
    """Return whether worker-owned tool paths must be sent relative to the worker root."""
    return _normalized_worker_backend_name() == "kubernetes"


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


def resolve_agent_worker_key(
    *,
    agent_name: str,
    config: Config,
    execution_identity: ToolExecutionIdentity | None = None,
) -> str | None:
    """Resolve the current worker key for an agent when worker routing is active."""
    if agent_name not in config.agents:
        return None

    worker_scope = config.get_agent_worker_scope(agent_name)
    if worker_scope is None:
        return None

    identity = resolve_execution_identity_for_worker_scope(
        worker_scope,
        agent_name=agent_name,
        execution_identity=execution_identity,
    )
    if identity is None:
        return None

    return resolve_worker_key(worker_scope, identity, agent_name=agent_name)


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
    resolved_base_path = base_storage_path.expanduser().resolve()
    workers_dir = (
        resolved_base_path.parent if resolved_base_path.parent.name == "workers" else resolved_base_path / "workers"
    )
    return workers_dir / worker_dir_name(worker_key)


def _resolve_agent_worker_root(
    *,
    agent_name: str,
    base_storage_path: Path,
    config: Config,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Path | None:
    """Resolve the current worker root for an agent when worker routing is active."""
    worker_key = resolve_agent_worker_key(
        agent_name=agent_name,
        config=config,
        execution_identity=execution_identity,
    )
    if worker_key is None:
        return None

    return worker_root_path(base_storage_path, worker_key)


def _resolve_agent_unscoped_worker_root(
    *,
    agent_name: str,
    base_storage_path: Path,
    config: Config,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Path | None:
    """Resolve the backend-owned worker root for unscoped dedicated execution."""
    if agent_name not in config.agents or config.get_agent_worker_scope(agent_name) is not None:
        return None
    if not _uses_unscoped_dedicated_worker_roots():
        return None
    worker_key = resolve_unscoped_worker_key(
        agent_name=agent_name,
        execution_identity=execution_identity,
    )
    return worker_root_path(base_storage_path, worker_key)


def resolve_agent_owned_state_root(
    *,
    agent_name: str,
    base_storage_path: Path,
    config: Config,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Path | None:
    """Resolve the worker-owned state root for one agent when applicable."""
    scoped_root = _resolve_agent_worker_root(
        agent_name=agent_name,
        base_storage_path=base_storage_path,
        config=config,
        execution_identity=execution_identity,
    )
    if scoped_root is not None:
        return scoped_root

    return _resolve_agent_unscoped_worker_root(
        agent_name=agent_name,
        base_storage_path=base_storage_path,
        config=config,
        execution_identity=execution_identity,
    )


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
    """Copy config-side starter files into the canonical agent-owned path when missing."""
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


def _resolve_agent_workspace_target(path_text: str, *, agent_root: Path) -> Path:
    candidate = (agent_root / Path(path_text)).resolve()
    resolved_root = agent_root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        msg = f"Agent-owned paths must stay within {resolved_root}: {path_text}"
        raise ValueError(msg) from exc
    return candidate


def resolve_agent_owned_path(
    path_text: str,
    *,
    field_name: str,
    agent_name: str,
    base_storage_path: Path,
    config: Config,
    execution_identity: ToolExecutionIdentity | None = None,
    state_root: Path | None = None,
) -> AgentOwnedPath:
    """Resolve one agent-owned path to the canonical worker-backed location when active."""
    source_path = resolve_config_relative_path(path_text)
    if state_root is None:
        state_root = resolve_agent_owned_state_root(
            agent_name=agent_name,
            base_storage_path=base_storage_path,
            config=config,
            execution_identity=execution_identity,
        )
    if state_root is None:
        return AgentOwnedPath(
            resolved_path=source_path,
            state_root=None,
            worker_relative_path=None,
        )

    if Path(path_text).is_absolute():
        msg = f"Agent '{agent_name}' uses worker-owned execution, so {field_name} must be relative: {path_text}"
        raise ValueError(msg)

    agent_workspace_root = (state_root / _AGENT_WORKSPACE_DIRNAME / _normalize_worker_dir_part(agent_name)).resolve()
    target_path = _resolve_agent_workspace_target(path_text, agent_root=agent_workspace_root)
    _bootstrap_missing_path(source_path, target_path, state_root=state_root)
    return AgentOwnedPath(
        resolved_path=target_path,
        state_root=state_root,
        worker_relative_path=target_path.relative_to(state_root.resolve()).as_posix(),
    )


def resolve_agent_state_storage_path(
    *,
    agent_name: str,
    base_storage_path: Path,
    config: Config,
    execution_identity: ToolExecutionIdentity | None = None,
) -> Path:
    """Return the storage path that should back the agent's mutable state."""
    return (
        resolve_agent_owned_state_root(
            agent_name=agent_name,
            base_storage_path=base_storage_path,
            config=config,
            execution_identity=execution_identity,
        )
        or base_storage_path
    )
