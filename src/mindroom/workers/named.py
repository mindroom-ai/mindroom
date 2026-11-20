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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.resources import defer_execution_cleanup
from mindroom.tool_system.worker_routing import (
    named_worker_key,
    pinned_worker_kind,
    pinned_worker_state_scope_key,
    worker_log_id,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workers.models import SCRIPT_RESOURCE_PROFILE_NAMES
from mindroom.workers.runtime import idle_worker_cleanup_runs, lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
    "reply_named_workers",
]

logger = get_logger(__name__)

_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
# The statuses of a named worker that runs a pod or is starting one.
_LIVE_STATUSES = frozenset({"starting", "ready"})

# Guards the fleets and the starts below; never held across a backend call, so the event loop may take it.
_lock = threading.Lock()
# The fleet of each reply that has not ended, by its turn ID.
_fleets: dict[str, _NamedWorkerFleet] = {}
# Named workers being started, which the backend does not list yet, with how many calls start each.
_starting: Counter[str] = Counter()
# Serializes counting the running workers and admitting one more, across the backend calls that takes.
_admission_lock = threading.Lock()


class NamedWorkerError(RuntimeError):
    """Why a call cannot run in the worker it names, in words for the agent that made the call."""


@runtime_checkable
class _NamedWorkerBackend(Protocol):
    """What a backend adds to run named workers: a limit on how many run, and their removal."""

    @property
    def named_workers_max(self) -> int:
        """How many named workers may run at once for every requester and agent together."""

    def retire_worker(self, worker_key: str) -> None:
        """Remove one exact worker and all backend-owned state."""


@dataclass(slots=True)
class _NamedWorker:
    """One worker a fleet started: the size it was started with and the start its last call ran in."""

    resource_profile: ScriptResourceProfileName | None
    # When the worker the last call ran in was created, and how often it had started.
    start: tuple[float, int] | None = None


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

    def worker_spec(
        self,
        normal_spec: WorkerSpec,
        name: str,
        resource_profile: ScriptResourceProfileName | None,
    ) -> WorkerSpec:
        """Return the spec of the worker ``name`` beside the normal worker ``normal_spec`` describes.

        The first call that names a worker sizes it. A later call takes that size, and one that asks for another is
        refused, because resizing a worker restarts it under the commands that run in it.
        """
        pin_id = hashlib.sha256(f"{self.nonce}:{name}".encode()).hexdigest()[:32]
        worker_key = named_worker_key(normal_spec.worker_key, pin_id)
        with _lock:
            started = self.workers.get(worker_key)
        if started is not None:
            if resource_profile not in (None, started.resource_profile):
                size = (
                    f"resource_profile {started.resource_profile}"
                    if started.resource_profile
                    else "no resource_profile"
                )
                msg = f"Worker '{name}' was started with {size} in this reply; omit resource_profile, or name another worker."
                raise NamedWorkerError(msg)
            resource_profile = started.resource_profile
        return replace(
            normal_spec,
            worker_key=worker_key,
            state_scope_worker_key=normal_spec.worker_key,
            resource_profile=resource_profile,
        )

    def header(self, name: str, handle: WorkerHandle) -> str:
        """Return the line a call's result starts with: its worker, and whether this call ran in a new start of it."""
        start = (handle.created_at, handle.startup_count)
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
            worker_keys = tuple(self.workers)
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
        """Return the spec of the named worker beside the normal worker ``normal_spec`` describes."""
        return self._fleet.worker_spec(normal_spec, self.name, self.resource_profile)

    def header(self, handle: WorkerHandle) -> str:
        """Return the line the call's result starts with, which names its worker and says when it is a new start."""
        return self._fleet.header(self.name, handle)


@asynccontextmanager
async def reply_named_workers(turn_id: str | None) -> AsyncIterator[None]:
    """Own the named workers the calls of the reply ``turn_id`` start, until it and its background jobs end.

    A reply that resumes under the same turn ID while its background jobs still run keeps their workers. None is a
    turn outside a conversation, whose calls can name no worker.
    """
    if turn_id is None:
        yield
        return
    with _lock:
        fleet = _fleets.setdefault(turn_id, _NamedWorkerFleet(turn_id))
        fleet.holders += 1
    try:
        yield
    finally:
        # Background jobs the reply started may still name workers, so the last of them releases the hold.
        if not defer_execution_cleanup(fleet.release):
            await fleet.release()


def _agent_limit(agent_name: str | None, config: Config | None) -> int:
    """Return how many named workers ``agent_name`` may run at once for one requester, 0 for none."""
    agent = config.agents.get(agent_name) if config is not None and agent_name is not None else None
    return 0 if agent is None else agent.extra_workers


def named_workers_refusal(agent_name: str | None, config: Config | None, runtime_paths: RuntimePaths) -> str | None:
    """Return why ``agent_name``'s calls cannot run in named workers, or None when they can."""
    if _agent_limit(agent_name, config) < 1:
        return "Named workers are off for this agent; extra_workers in its config allows them."
    if worker_backend_kind(runtime_paths) is not WorkerBackendKind.KUBERNETES or not idle_worker_cleanup_runs(
        runtime_paths,
    ):
        return (
            "Named workers need the kubernetes worker backend and idle-worker cleanup, which the API server runs "
            "when MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS is above 0."
        )
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
    with _lock:
        fleet = _fleets.get(turn_id) if turn_id is not None else None
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
    """Start or reach the named worker ``spec`` describes, refusing to start one past the limits.

    A worker that already runs is reached without counting. Otherwise the workers the backend runs and those being
    started count against the agent's limit for this requester and the backend's limit for everyone. The backend
    replaces a stopped worker of this name with an empty one.
    """
    if not isinstance(backend, _NamedWorkerBackend):
        msg = "Named workers need the kubernetes worker backend."
        raise NamedWorkerError(msg)
    worker_key = spec.worker_key
    fleet = call._fleet
    with _admission_lock:
        named = {
            handle.worker_key: handle
            for handle in backend.list_workers()
            if pinned_worker_kind(handle.worker_key) == "named"
        }
        existing = named.get(worker_key)
        if existing is None or existing.status not in _LIVE_STATUSES:
            with _lock:
                starting = set(_starting)
            live = {key for key, handle in named.items() if handle.status in _LIVE_STATUSES} | starting
            live.discard(worker_key)
            mine = sum(pinned_worker_state_scope_key(key) == spec.state_scope_worker_key for key in live)
            if mine >= call.agent_limit:
                msg = (
                    f"Cannot start worker '{call.name}': {mine} of the {call.agent_limit} named workers this agent "
                    "may run for you are running. Name one of them, or wait until an idle one is removed."
                )
                raise NamedWorkerError(msg)
            if len(live) >= backend.named_workers_max:
                msg = (
                    f"Cannot start worker '{call.name}': this deployment runs its limit of "
                    f"{backend.named_workers_max} named workers. Name one you started, or try again later."
                )
                raise NamedWorkerError(msg)
        with _lock:
            if fleet.holders < 1:
                msg = f"Cannot run in worker '{call.name}': its reply and that reply's background jobs have ended."
                raise NamedWorkerError(msg)
            fleet.workers.setdefault(worker_key, _NamedWorker(spec.resource_profile))
            fleet.runtime_paths = call.runtime_paths
            _starting[worker_key] += 1
    try:
        return backend.ensure_worker(spec, progress_sink=progress_sink)
    finally:
        with _lock:
            _starting[worker_key] -= 1
            if not _starting[worker_key]:
                del _starting[worker_key]


def _retire_workers(runtime_paths: RuntimePaths, worker_keys: tuple[str, ...]) -> None:
    """Remove a fleet's workers together; one that fails is logged and left to the backend's cleanup."""
    lease = lease_configured_primary_worker_manager(runtime_paths)
    if lease is None:
        return
    with lease as backend:
        if not isinstance(backend, _NamedWorkerBackend):
            return

        def retire(worker_key: str) -> None:
            try:
                backend.retire_worker(worker_key)
            except WorkerBackendError:
                logger.warning("named_worker_retire_failed", worker=worker_log_id(worker_key), exc_info=True)

        with ThreadPoolExecutor(max_workers=len(worker_keys), thread_name_prefix="named-worker-retire") as pool:
            list(pool.map(retire, worker_keys))
