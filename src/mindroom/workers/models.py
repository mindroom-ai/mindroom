"""Backend-neutral worker models."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

WorkerStatus = Literal["starting", "ready", "idle", "failed"]
WorkerReadyPhase = Literal["cold_start", "waiting", "ready", "failed"]
ScriptResourceProfileName = Literal["small", "standard", "large"]


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """Stable worker request resolved from worker-routing semantics."""

    worker_key: str
    private_agent_names: frozenset[str] | None = None
    mirrored_credential_services: frozenset[str] | None = None
    state_scope_worker_key: str | None = None
    resource_profile: ScriptResourceProfileName | None = None


@dataclass(frozen=True, slots=True)
class WorkerHandle:
    """Generic worker handle used by the execution layer."""

    worker_id: str
    worker_key: str
    endpoint: str
    auth_token: str | None
    status: WorkerStatus
    backend_name: str
    last_used_at: float
    created_at: float
    last_started_at: float | None = None
    expires_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None
    debug_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerMaintenanceResult:
    """Workers changed by one backend maintenance pass."""

    cleaned: tuple[WorkerHandle, ...]
    reconciled: tuple[WorkerHandle, ...]


@dataclass(frozen=True, slots=True)
class WorkerReadyProgress:
    """Progress event emitted while a worker is warming up."""

    phase: WorkerReadyPhase
    worker_key: str
    backend_name: str
    elapsed_seconds: float
    error: str | None = None


ProgressSink = Callable[[WorkerReadyProgress], None]

_API_ROUTES = {
    "cleanup": "workers/cleanup",
    "script-run": "scripts/run",
    "script-status": "scripts",
    "script-cancel": "scripts",
    "agent-cli-install": "agent-cli/install",
    "agent-cli-shell": "agent-cli/shell",
}


def worker_api_endpoint(
    handle: WorkerHandle,
    operation: Literal[
        "execute",
        "leases",
        "workers",
        "cleanup",
        "save-attachment",
        "view-file",
        "script-run",
        "script-status",
        "script-cancel",
        "agent-cli-install",
        "agent-cli-shell",
    ],
) -> str:
    """Return the API endpoint for one worker operation."""
    api_root = handle.debug_metadata.get("api_root")
    if api_root is None:
        api_root = handle.endpoint.removesuffix("/execute").rstrip("/")

    if operation == "execute":
        return handle.endpoint
    return f"{api_root}/{_API_ROUTES.get(operation, operation)}"


# Requester encoding percent-escapes "!" and other key parts are normalized
# without it, so no requester ID can impersonate the privileged CLI segment.
_PROCESS_SEGMENT_PREFIXES = {"script": "script-", "agent-turn": "!agent-turn-"}


def process_worker_key(base_worker_key: str, *, purpose: Literal["script", "agent-turn"], process_id: UUID) -> str:
    """Pin a physical process to a user-agent key without changing its final agent."""
    parts = base_worker_key.split(":")
    if len(parts) < 5 or parts[0] != "v1" or parts[2] != "user_agent" or not parts[-1]:
        msg = "Isolated processes require a resolved user-agent worker key."
        raise ValueError(msg)
    return ":".join((*parts[:-1], f"{_PROCESS_SEGMENT_PREFIXES[purpose]}{process_id.hex}", parts[-1]))


def is_cli_worker_key(worker_key: str) -> bool:
    """Recognize the reserved physical CLI process-key namespace."""
    parts = worker_key.split(":")
    prefix = _PROCESS_SEGMENT_PREFIXES["agent-turn"]
    if len(parts) < 6 or not parts[-2].startswith(prefix):
        return False
    try:
        process_id = UUID(hex=parts[-2].removeprefix(prefix))
        return (
            process_worker_key(":".join((*parts[:-2], parts[-1])), purpose="agent-turn", process_id=process_id)
            == worker_key
        )
    except ValueError:
        return False
