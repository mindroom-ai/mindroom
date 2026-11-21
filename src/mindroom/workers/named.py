"""Workers an agent's shell calls address by name, each a worker of its own beside the agent's normal worker.

A reply owns the named workers its calls start. They are removed when the reply and the background jobs it started
have ended, and the backend's cleanup removes any that outlive their reply, as after a restart, so nothing here is
kept on disk. A worker's key holds the reply's digest of the name, never the name itself: another reply's calls,
and a later reply's calls with the same name, reach workers of their own.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom.background_tasks import create_background_task
from mindroom.config.resolution import agent_policy
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.resources import current_response_resources
from mindroom.tool_system.worker_routing import (
    named_worker_key,
    pinned_worker_state_scope_key,
    resolved_worker_key_scope,
    worker_log_id,
)
from mindroom.workers.backend import RetiringWorkerBackend, WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workers.backends.kubernetes_config import NAMED_WORKERS_MAX_ENV, configured_named_workers_max
from mindroom.workers.models import SCRIPT_RESOURCE_PROFILE_NAMES
from mindroom.workers.runtime import idle_worker_cleanup_runs, lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.workers.backend import WorkerBackend
    from mindroom.workers.models import ProgressSink, ScriptResourceProfileName, WorkerHandle, WorkerSpec

__all__ = [
    "NamedWorkerCall",
    "NamedWorkerError",
    "ensure_named_worker",
    "named_worker_call",
    "named_workers_refusal",
]

logger = get_logger(__name__)

_DISABLED_REFUSAL = f"This deployment disabled named workers with {NAMED_WORKERS_MAX_ENV}=0."
_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# Why an agent whose workers are not one per requester and agent runs no named workers: its normal worker's key
# would not name whose limit a named worker counts against.
_SCOPE_REFUSAL = (
    "Named workers need an agent whose workers are per requester and agent: "
    "worker_scope: user_agent, or private.per: user_agent."
)

# Guards the fleets and the starts below; never held across a backend call, so the event loop may take it.
_lock = threading.Lock()
# The fleet of each reply that has not ended, by its turn ID.
_fleets: dict[str, _NamedWorkerFleet] = {}
# Named workers being started, which the backend may not list yet, with how many calls start each.
_starting: Counter[str] = Counter()
# Serializes counting the workers that exist and admitting one more, across the backend call that takes.
_admission_lock = threading.Lock()


class NamedWorkerError(RuntimeError):
    """Why a call cannot run in the worker it names, in words for the agent that made the call."""


@dataclass(slots=True)
class _NamedWorker:
    """One worker a fleet started: the size it was started with and the start its last call ran in."""

    resource_profile: ScriptResourceProfileName | None
    # What the backend ran the worker as when the last call ran in it, and how often that had started.
    start: tuple[str | None, int] | None = None


@dataclass(eq=False, slots=True)
class _NamedWorkerFleet:
    """The named workers of one reply and of the background jobs it started."""

    turn_id: str
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    # How many replies with this turn ID, and the background jobs each started, have not ended.
    holders: int = 0
    # Each worker the fleet started, by its key.
    workers: dict[str, _NamedWorker] = field(default_factory=dict)
    # The runtime whose backend runs the fleet's workers, known once a call starts one.
    runtime_paths: RuntimePaths | None = None

    def worker_key(self, normal_worker_key: str, name: str) -> str:
        """Return the key of the worker ``name`` beside the normal worker ``normal_worker_key``."""
        pin_id = hashlib.sha256(f"{self.nonce}:{name}".encode()).hexdigest()[:32]
        return named_worker_key(normal_worker_key, pin_id)

    def admit(self, call: NamedWorkerCall, worker_key: str) -> ScriptResourceProfileName | None:
        """Record that ``call`` starts or reaches ``worker_key`` and return the size the worker has; ``_lock`` is held.

        The first call admitted for a worker sizes it. A later call takes that size, and one that asks for another
        is refused, because resizing a worker restarts it under the commands that run in it.
        """
        if self.holders < 1:
            msg = f"Cannot run in worker '{call.name}': its reply and that reply's background jobs have ended."
            raise NamedWorkerError(msg)
        worker = self.workers.get(worker_key)
        if worker is None:
            worker = self.workers[worker_key] = _NamedWorker(call.resource_profile)
        elif call.resource_profile not in (None, worker.resource_profile):
            size = f"resource_profile {worker.resource_profile}" if worker.resource_profile else "no resource_profile"
            msg = (
                f"Worker '{call.name}' was started with {size} in this reply; "
                "omit resource_profile, or name another worker."
            )
            raise NamedWorkerError(msg)
        self.runtime_paths = call.runtime_paths
        return worker.resource_profile

    def header(self, name: str, handle: WorkerHandle) -> str:
        """Return the line a call's result starts with: its worker, and whether this call ran in a new start of it."""
        start = (handle.incarnation, handle.startup_count)
        with _lock:
            worker = self.workers[handle.worker_key]
            started = worker.start != start
            worker.start = start
        return f"[worker: {name}, started]" if started else f"[worker: {name}]"

    async def release(self) -> None:
        """End one reply's hold, removing the fleet's workers once no reply or background job holds it."""
        with _lock:
            self.holders -= 1
            if self.holders:
                return
            del _fleets[self.turn_id]
            # A blocking start can outlive its cancelled caller; its last starter retires it instead.
            worker_keys = tuple(key for key in self.workers if key not in _starting)
        if worker_keys and self.runtime_paths is not None:
            # Removal waits for each pod to stop, which the reply that just ended does not wait for.
            create_background_task(
                asyncio.to_thread(_retire_workers, self.runtime_paths, worker_keys),
                name="named_worker_retire",
            )


@dataclass(frozen=True, slots=True)
class NamedWorkerCall:
    """One call's request to run in a named worker of its reply."""

    name: str
    resource_profile: ScriptResourceProfileName | None
    # How many named workers the calling agent may run at once for its requester.
    agent_limit: int
    runtime_paths: RuntimePaths
    _fleet: _NamedWorkerFleet

    def worker_spec(self, normal_spec: WorkerSpec) -> WorkerSpec:
        """Return the spec of the named worker beside the normal worker ``normal_spec`` describes, as the call asks.

        ``ensure_named_worker`` gives the worker the size its first admitted call asked for.
        """
        return replace(
            normal_spec,
            worker_key=self._fleet.worker_key(normal_spec.worker_key, self.name),
            state_scope_worker_key=normal_spec.worker_key,
            resource_profile=self.resource_profile,
        )

    def header(self, handle: WorkerHandle) -> str:
        """Return the line the call's result starts with, which names its worker and says when it is a new start."""
        return self._fleet.header(self.name, handle)


def _owned_fleet(turn_id: str | None) -> _NamedWorkerFleet | None:
    """Keep each calling turn's fleet until the shared response and its background jobs settle."""
    owner = current_response_resources()
    if turn_id is None or owner is None:
        return None
    with _lock:
        fleet = _fleets.get(turn_id) or _NamedWorkerFleet(turn_id)
        retained = owner.retain(fleet, fleet.release)
        if retained is None:
            return None
        if retained:
            _fleets[turn_id] = fleet
            fleet.holders += 1
        return fleet


def _agent_limit(agent_name: str | None, config: Config | None) -> int:
    """Return how many named workers ``agent_name`` may run at once for one requester, 0 for none."""
    agent = config.agents.get(agent_name) if config is not None and agent_name is not None else None
    return 0 if agent is None else agent.named_workers


def named_workers_refusal(agent_name: str | None, config: Config | None, runtime_paths: RuntimePaths) -> str | None:
    """Return why ``agent_name``'s calls cannot run in named workers, or None when they can."""
    if config is None or agent_name is None or _agent_limit(agent_name, config) < 1:
        return "Named workers are off for this agent; named_workers in its config allows them."
    if agent_policy(config, agent_name).effective_execution_scope != "user_agent":
        return _SCOPE_REFUSAL
    if worker_backend_kind(runtime_paths) is not WorkerBackendKind.KUBERNETES or not idle_worker_cleanup_runs(
        runtime_paths,
    ):
        return (
            "Named workers need the kubernetes worker backend and idle-worker cleanup, which the API server runs "
            "when MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS is above 0."
        )
    if configured_named_workers_max(runtime_paths) == 0:
        return _DISABLED_REFUSAL
    return None


def named_worker_call(
    name: object,
    resource_profile: object,
    *,
    agent_name: str | None,
    config: Config | None,
    runtime_paths: RuntimePaths,
    turn_id: str | None,
) -> NamedWorkerCall | None:
    """Return the named worker a call's ``worker`` and ``resource_profile`` arguments ask for, None for neither.

    The arguments are as the model sent them, so their types are checked here. ``turn_id`` is the reply the call
    belongs to, whose fleet owns the worker.
    """
    if name is None and resource_profile is None:
        return None
    if name is None:
        msg = "resource_profile sizes a named worker; set worker too."
        raise NamedWorkerError(msg)
    refusal = named_workers_refusal(agent_name, config, runtime_paths)
    if refusal is not None:
        raise NamedWorkerError(refusal)
    if not isinstance(name, str) or _NAME_PATTERN.fullmatch(name) is None:
        msg = "worker must be 1 to 32 lowercase letters, digits, or hyphens, and start with a letter or digit."
        raise NamedWorkerError(msg)
    if resource_profile is not None and resource_profile not in SCRIPT_RESOURCE_PROFILE_NAMES:
        msg = "resource_profile must be small, standard, or large."
        raise NamedWorkerError(msg)
    fleet = _owned_fleet(turn_id)
    if fleet is None:
        msg = (
            "A named worker belongs to a reply in a conversation, and this call runs outside one, "
            "or after its reply and that reply's background jobs ended."
        )
        raise NamedWorkerError(msg)
    return NamedWorkerCall(
        name=name,
        resource_profile=cast("ScriptResourceProfileName | None", resource_profile),
        agent_limit=_agent_limit(agent_name, config),
        runtime_paths=runtime_paths,
        _fleet=fleet,
    )


def ensure_named_worker(
    backend: WorkerBackend,
    spec: WorkerSpec,
    call: NamedWorkerCall,
    *,
    progress_sink: ProgressSink | None = None,
) -> WorkerHandle:
    """Start or reach the named worker ``spec``, from ``call.worker_spec``, describes, refusing one past the limits.

    A worker that exists, or that another call is starting, is reached without counting. Otherwise every named
    worker that exists, running or stopped, and every one being started counts against the agent's limit for this
    requester and the backend's limit for everyone. The backend starts the worker with the size its first admitted
    call asked for, whatever size this call names, and replaces a stopped worker of this name with an empty one.
    """
    if backend.named_workers_max < 1:
        if backend.backend_name == "kubernetes":
            raise NamedWorkerError(_DISABLED_REFUSAL)
        msg = "Named workers need the kubernetes worker backend."
        raise NamedWorkerError(msg)
    worker_key = spec.worker_key
    normal_worker_key = spec.state_scope_worker_key
    # The key of an agent's normal worker names its tenant, requester, and agent only on this scope, so only there
    # does it say whose limit the worker counts against.
    if normal_worker_key is None or resolved_worker_key_scope(normal_worker_key) != "user_agent":
        raise NamedWorkerError(_SCOPE_REFUSAL)
    with _admission_lock:
        # Read before the workers that exist, so a start that ends in between is counted there, never nowhere.
        with _lock:
            starting = frozenset(_starting)
            already_admitted = worker_key in call._fleet.workers
        existing = worker_key in starting or (already_admitted and backend.has_named_worker(worker_key))
        counted = None if existing else backend.named_worker_keys() | starting
        if counted is not None and worker_key not in counted:
            mine = sum(pinned_worker_state_scope_key(key) == normal_worker_key for key in counted)
            if mine >= call.agent_limit:
                msg = (
                    f"Cannot start worker '{call.name}': this agent already has {mine} of the {call.agent_limit} "
                    "named workers it may have for you. Run the command in a worker this reply started, or wait: "
                    "a named worker no command runs in is removed after a few minutes."
                )
                raise NamedWorkerError(msg)
            if len(counted) >= backend.named_workers_max:
                msg = (
                    f"Cannot start worker '{call.name}': this deployment already has its limit of "
                    f"{backend.named_workers_max} named workers. Run the command in a worker this reply started, "
                    "or try again later."
                )
                raise NamedWorkerError(msg)
        with _lock:
            spec = replace(spec, resource_profile=call._fleet.admit(call, worker_key))
            _starting[worker_key] += 1
    try:
        return backend.ensure_worker(spec, progress_sink=progress_sink)
    finally:
        with _lock:
            _starting[worker_key] -= 1
            last_start = _starting[worker_key] == 0
            if last_start:
                del _starting[worker_key]
            retire = last_start and not call._fleet.holders
        if retire:
            _retire_named_worker(backend, worker_key)


def _retire_named_worker(backend: WorkerBackend, worker_key: str) -> None:
    try:
        if not isinstance(backend, RetiringWorkerBackend):
            msg = "Named worker backend does not support exact worker retirement."
            raise WorkerBackendError(msg)  # noqa: TRY301 - Use the same warning path as a failed retirement.
        backend.retire_worker(worker_key)
    except WorkerBackendError:
        logger.warning("named_worker_retire_failed", worker=worker_log_id(worker_key), exc_info=True)


def _retire_workers(runtime_paths: RuntimePaths, worker_keys: tuple[str, ...]) -> None:
    """Remove a fleet's workers together; one that fails is logged and left to the backend's cleanup."""
    lease = lease_configured_primary_worker_manager(runtime_paths)
    if lease is None:
        return
    with lease as backend:
        if backend.named_workers_max < 1:
            return
        with ThreadPoolExecutor(max_workers=len(worker_keys), thread_name_prefix="named-worker-retire") as pool:
            list(pool.map(partial(_retire_named_worker, backend), worker_keys))
