"""Whether a delegated subagent may run its worker-routed tool calls in a dedicated worker of its own."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workers.models import SCRIPT_RESOURCE_PROFILE_NAMES
from mindroom.workers.placements import DedicatedWorkerPlacement
from mindroom.workers.runtime import idle_worker_cleanup_runs

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild


def _dedicated_workers_available(runtime_paths: RuntimePaths) -> bool:
    """Return whether this runtime can give a subagent a worker of its own.

    Each such worker serves one subagent, so they are only offered where idle cleanup removes them again.
    """
    return worker_backend_kind(runtime_paths).dedicated and idle_worker_cleanup_runs(runtime_paths)


def resource_profiles_available(runtime_paths: RuntimePaths) -> bool:
    """Return whether a subagent's own worker can be given a resource profile, which only Kubernetes enforces.

    Callers check ``_dedicated_workers_available`` first, so this asks only for the backend.
    """
    return worker_backend_kind(runtime_paths) is WorkerBackendKind.KUBERNETES


def dedicated_worker_refusal(
    caller_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    resource_profile: object,
) -> str | None:
    """Return why ``caller_name`` cannot run a subagent in a dedicated worker with ``resource_profile``, or None."""
    caller = config.agents.get(caller_name)
    if caller is None or not caller.dedicated_subagent_workers:
        return f"Cannot delegate: {caller_name} may not run subagents in dedicated workers."
    if not _dedicated_workers_available(runtime_paths):
        return (
            "Cannot delegate: dedicated workers need the docker or kubernetes worker backend and idle-worker "
            "cleanup, which the API server runs when MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS is above 0."
        )
    if resource_profile is None:
        return None
    if not isinstance(resource_profile, str) or resource_profile not in SCRIPT_RESOURCE_PROFILE_NAMES:
        return "Cannot delegate: resource_profile must be small, standard, or large."
    if not resource_profiles_available(runtime_paths):
        return "Cannot delegate: resource_profile requires the kubernetes worker backend."
    return None


def child_placement(
    child: DelegationChild,
    config: Config,
    runtime_paths: RuntimePaths,
) -> DedicatedWorkerPlacement | None:
    """Return the worker placement for one attempt of ``child``, or None for its normal worker.

    Every attempt rechecks its caller's permission and the backend, so a caller that loses the setting, or a runtime
    that can no longer provide such workers, runs the child's later turns on its normal worker.
    """
    if (
        not child.dedicated_worker
        or dedicated_worker_refusal(
            child.caller_agent_name,
            config,
            runtime_paths,
            resource_profile=child.worker_resource_profile,
        )
        is not None
    ):
        return None
    return DedicatedWorkerPlacement(child.subagent_id, child.worker_resource_profile)
