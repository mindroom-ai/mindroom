"""Backend-neutral worker models."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from mindroom.agent_policy import ResolvedAgentPolicy
    from mindroom.config.main import Config
    from mindroom.workers.backend import WorkerBackend

WorkerStatus = Literal["starting", "ready", "idle", "failed"]
WorkerReadyPhase = Literal["cold_start", "waiting", "ready", "failed"]
ScriptResourceProfileName = Literal["small", "standard", "large"]


@dataclass(frozen=True, slots=True)
class WorkerLaunchConfig:
    """What a dedicated worker launches with, derived once from the applied config.

    Only ``digest`` takes part in comparison, so two launches compare cheaply.
    The mappings are shared: a consumer that rewrites them copies them first.
    """

    config: Config = field(compare=False, repr=False)
    config_data: Mapping[str, object] = field(compare=False, repr=False)
    agent_policies: Mapping[str, ResolvedAgentPolicy] = field(compare=False, repr=False)
    tool_validation_snapshot: Mapping[str, Mapping[str, object]] = field(compare=False, repr=False)
    grantable_credentials: frozenset[str] = field(compare=False)
    digest: str


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """Stable worker request resolved from worker-routing semantics.

    A dedicated backend provisions it from the applied launch it reads inside the worker's lock.
    ``mirror_grantable_credentials`` mirrors the launch's grantable credentials into the worker; scripts mirror none.
    """

    worker_key: str
    private_agent_names: frozenset[str] | None = None
    mirror_grantable_credentials: bool = True
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


@dataclass(frozen=True, slots=True)
class ConfiguredWorkerBackend:
    """One reading of the selected worker backend's settings.

    ``signature`` changes whenever these settings would build a different manager.
    ``cleanup_signature`` names the workers that manager owns, which outlive routine changes of its settings.
    ``build`` constructs the manager from this same reading; its dedicated workers launch from ``current_launch``.
    """

    signature: tuple[str, ...]
    cleanup_signature: tuple[str, ...]
    build: Callable[[Callable[[], WorkerLaunchConfig]], WorkerBackend]


SANDBOX_RUNNER_API_ROOT = "/api/sandbox-runner"
_API_ROUTES = {
    "script-run": "scripts/run",
    "script-status": "scripts",
    "script-cancel": "scripts",
    "execute-cancel": "execute/cancel",
}


def normalize_sandbox_runner_api_root(url: str) -> str:
    """Normalize a sandbox-runner base URL, API root, or execute endpoint into its API root; blank stays blank."""
    normalized = url.strip().rstrip("/")
    if not normalized or normalized.endswith(SANDBOX_RUNNER_API_ROOT):
        return normalized
    if normalized.endswith("/execute"):
        return normalized.removesuffix("/execute")
    return f"{normalized}{SANDBOX_RUNNER_API_ROOT}"


def worker_api_endpoint(
    handle: WorkerHandle,
    operation: Literal[
        "execute",
        "leases",
        "save-attachment",
        "view-file",
        "script-run",
        "script-status",
        "script-cancel",
        "execute-cancel",
    ],
) -> str:
    """Return the API endpoint for one worker operation."""
    api_root = handle.debug_metadata.get("api_root")
    if api_root is None:
        api_root = handle.endpoint.removesuffix("/execute").rstrip("/")

    if operation == "execute":
        return handle.endpoint
    return f"{api_root}/{_API_ROUTES.get(operation, operation)}"


def process_worker_key(base_worker_key: str, *, process_id: UUID) -> str:
    """Pin a background script process to a user-agent key without changing its final agent."""
    parts = base_worker_key.split(":")
    if len(parts) < 5 or parts[0] != "v1" or parts[2] != "user_agent" or not parts[-1]:
        msg = "Isolated processes require a resolved user-agent worker key."
        raise ValueError(msg)
    return ":".join((*parts[:-1], f"script-{process_id.hex}", parts[-1]))
