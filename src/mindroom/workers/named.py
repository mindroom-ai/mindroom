"""Workers an agent's shell calls address by name, each a worker of its own beside the agent's normal worker.

A reply owns the named workers its calls start. They are removed when the reply and the background jobs it started
have ended, and the backend's cleanup removes any that outlive their reply, as after a restart, so nothing here is
kept on disk. A worker's key holds the reply's digest of the name, never the name itself: another reply's calls,
and a later reply's calls with the same name, reach workers of their own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.config.resolution import agent_policy
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.resources import current_response_resources
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT, normalize_output_path_argument
from mindroom.tool_system.worker_routing import (
    named_worker_key,
    pinned_worker_state_scope_key,
    resolve_worker_key,
    resolved_worker_key_scope,
    worker_log_id,
)
from mindroom.workers.backend import RetiringWorkerBackend, WorkerBackendError
from mindroom.workers.backend_kinds import WorkerBackendKind, worker_backend_kind
from mindroom.workers.backends.kubernetes_config import (
    NAMED_WORKERS_MAX_ENV,
    configured_named_workers_max,
    configured_runtime_class_name,
)
from mindroom.workers.models import SCRIPT_RESOURCE_PROFILE_NAMES, WorkerSandboxConfig
from mindroom.workers.runtime import idle_worker_cleanup_runs, lease_configured_primary_worker_manager
from mindroom.workers.sandbox_mounts import (
    parse_sandbox_config,
    resolve_worker_sandbox_mount,
    sandbox_mount_dir_runtime_class_refusal,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workers.backend import WorkerBackend
    from mindroom.workers.models import ProgressSink, ScriptResourceProfileName, WorkerHandle, WorkerSpec

type LockedWorkerSpec = tuple[ScriptResourceProfileName | None, WorkerSandboxConfig | None]

__all__ = [
    "LockedWorkerSpec",
    "NamedWorkerCall",
    "NamedWorkerError",
    "current_named_worker_owner",
    "deserialize_sandbox_config",
    "ensure_named_worker",
    "named_worker_call",
    "named_worker_owner",
    "named_workers_refusal",
    "parse_named_worker_sandbox",
    "resolve_locked_worker_options",
    "resolve_named_worker_scope",
    "sandbox_mount_relative_path",
    "serialize_sandbox_config",
    "validate_effective_sandbox",
    "validate_named_worker_options",
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
# The fleet of each reply or workflow owner that has not ended, by its owner ID.
_fleets: dict[str, _NamedWorkerFleet] = {}
# Explicit fleet owner and optional prelocked named-worker specs bound by a workflow caller.
_FLEET_OWNER: ContextVar[tuple[str, Mapping[str, LockedWorkerSpec]] | None] = ContextVar(
    "named_worker_owner",
    default=None,
)
# Named workers being started, which the backend may not list yet, with how many calls start each.
_starting: Counter[str] = Counter()
# Serializes counting the workers that exist and admitting one more, across the backend call that takes.
_admission_lock = threading.Lock()


def current_named_worker_owner() -> str | None:
    """Return the explicit named-worker fleet owner bound in this context, if any."""
    bound = _FLEET_OWNER.get()
    return None if bound is None else bound[0]


@contextmanager
def named_worker_owner(
    owner_id: str,
    *,
    prelocked: Mapping[str, LockedWorkerSpec] | None = None,
) -> Iterator[None]:
    """Bind an explicit fleet owner (and optional persisted worker locks) for named worker calls."""
    token = _FLEET_OWNER.set((owner_id, dict(prelocked) if prelocked else {}))
    try:
        yield
    finally:
        _FLEET_OWNER.reset(token)


class NamedWorkerError(RuntimeError):
    """Why a call cannot run in the worker it names, in words for the agent that made the call."""


def _describe_sandbox(sandbox: WorkerSandboxConfig | None) -> str:
    if sandbox is None:
        return "without sandbox mode"
    if sandbox.mount_dir is None:
        return "in sandbox mode (empty scratch)"
    mode = ", read-only" if sandbox.mount_read_only else ""
    return f"in sandbox mode (mount_dir={sandbox.mount_dir!r}{mode})"


def resolve_locked_worker_options(
    locked: LockedWorkerSpec | None,
    *,
    worker_name: str,
    resource_profile: ScriptResourceProfileName | None,
    sandbox: WorkerSandboxConfig | None,
    sandbox_specified: bool,
    scope: str = "reply",
) -> LockedWorkerSpec:
    """Resolve effective ``(resource_profile, sandbox)`` against an optional locked worker spec.

    When ``locked`` is None, returns ``(resource_profile, sandbox)``. Otherwise omitted options inherit the locked
    values while conflicting options raise ``NamedWorkerError``.
    """
    if locked is None:
        return resource_profile, sandbox
    effective_profile, effective_sandbox = locked
    verb = "started" if scope == "reply" else "locked"
    if resource_profile not in (None, effective_profile):
        size = f"resource_profile {effective_profile}" if effective_profile else "no resource_profile"
        msg = (
            f"Worker '{worker_name}' was {verb} with {size} in this {scope}; "
            "omit resource_profile, or name another worker."
        )
        raise NamedWorkerError(msg)
    if sandbox_specified and sandbox != effective_sandbox:
        msg = (
            f"Worker '{worker_name}' was {verb} {_describe_sandbox(effective_sandbox)} in this {scope}; "
            "omit sandbox options to reuse it, or name another worker."
        )
        raise NamedWorkerError(msg)
    return effective_profile, effective_sandbox


@dataclass(slots=True)
class _NamedWorker:
    """One worker a fleet started: its locked size and sandbox config, and the start its last call ran in."""

    resource_profile: ScriptResourceProfileName | None
    sandbox: WorkerSandboxConfig | None = None
    # What the backend ran the worker as when the last call ran in it, and how often that had started.
    start: tuple[str | None, int] | None = None


@dataclass(eq=False, slots=True)
class _NamedWorkerFleet:
    """The named workers of one reply or workflow owner and its active child operations."""

    owner_id: str
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    # How many execution resource holders for this owner ID have not ended.
    holders: int = 0
    # Each worker the fleet started, by its key.
    workers: dict[str, _NamedWorker] = field(default_factory=dict)
    # Persisted immutable specs inherited from prior epochs, keyed by worker name (never retired unless started).
    prelocked: dict[str, LockedWorkerSpec] = field(default_factory=dict)
    # The runtime whose backend runs the fleet's workers, known once a call starts one.
    runtime_paths: RuntimePaths | None = None

    def worker_key(self, normal_worker_key: str, name: str) -> str:
        """Return the key of the worker ``name`` beside the normal worker ``normal_worker_key``."""
        pin_id = hashlib.sha256(f"{self.nonce}:{name}".encode()).hexdigest()[:32]
        return named_worker_key(normal_worker_key, pin_id)

    def admit(
        self,
        call: NamedWorkerCall,
        worker_key: str,
    ) -> LockedWorkerSpec:
        """Record that ``call`` starts or reaches ``worker_key`` and return its locked size and sandbox; ``_lock`` is held.

        The first call admitted for a worker (or its persisted prelocked spec) locks its size and sandbox settings.
        A later call omitting them reuses those locked settings; one asking for different settings is refused.
        """
        if self.holders < 1:
            msg = f"Cannot run in worker '{call.name}': its reply and that reply's background jobs have ended."
            raise NamedWorkerError(msg)
        worker = self.workers.get(worker_key)
        locked = (worker.resource_profile, worker.sandbox) if worker is not None else self.prelocked.get(call.name)
        effective_profile, effective_sandbox = resolve_locked_worker_options(
            locked,
            worker_name=call.name,
            resource_profile=call.resource_profile,
            sandbox=call.sandbox,
            sandbox_specified=call.sandbox_specified,
            scope="reply",
        )
        if effective_sandbox is not None and effective_sandbox.mount_dir is not None:
            runtime_class_name = configured_runtime_class_name(call.runtime_paths)
            if runtime_class_name is not None:
                raise NamedWorkerError(sandbox_mount_dir_runtime_class_refusal(runtime_class_name))
        if worker is None:
            worker = self.workers[worker_key] = _NamedWorker(effective_profile, effective_sandbox)
        self.runtime_paths = call.runtime_paths
        return worker.resource_profile, worker.sandbox

    def header(self, name: str, handle: WorkerHandle) -> str:
        """Return the line a call's result starts with: its worker, and whether this call ran in a new start of it."""
        start = (handle.incarnation, handle.startup_count)
        with _lock:
            worker = self.workers[handle.worker_key]
            started = worker.start != start
            worker.start = start
        return f"[worker: {name}, started]" if started else f"[worker: {name}]"

    async def release(self) -> None:
        """End one holder's lease, removing the fleet's started workers once no holder remains."""
        with _lock:
            self.holders -= 1
            if self.holders:
                return
            del _fleets[self.owner_id]
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
    sandbox: WorkerSandboxConfig | None = None
    sandbox_specified: bool = False

    def worker_spec(self, normal_spec: WorkerSpec) -> WorkerSpec:
        """Return the spec of the named worker beside the normal worker ``normal_spec`` describes, as the call asks.

        ``ensure_named_worker`` gives the worker the size and sandbox config its first admitted call asked for.
        """
        return replace(
            normal_spec,
            worker_key=self._fleet.worker_key(normal_spec.worker_key, self.name),
            state_scope_worker_key=normal_spec.worker_key,
            resource_profile=self.resource_profile,
            sandbox=self.sandbox,
            mirror_grantable_credentials=normal_spec.mirror_grantable_credentials and self.sandbox is None,
        )

    def locked_sandbox(self, normal_worker_key: str | None = None) -> WorkerSandboxConfig | None:
        """Return the fleet's already-locked sandbox config if admitted or prelocked, else this call's sandbox."""
        worker_key = self._fleet.worker_key(normal_worker_key, self.name) if normal_worker_key is not None else None
        with _lock:
            if worker_key is not None and (worker := self._fleet.workers.get(worker_key)) is not None:
                return worker.sandbox
            if (prelocked := self._fleet.prelocked.get(self.name)) is not None:
                return prelocked[1]
            return self.sandbox

    def header(self, handle: WorkerHandle) -> str:
        """Return the line the call's result starts with, which names its worker and says when it is a new start."""
        return self._fleet.header(self.name, handle)


def _owned_fleet(owner_id: str | None) -> _NamedWorkerFleet | None:
    """Keep each calling owner's fleet until its execution resources and child operations settle."""
    owner = current_response_resources()
    if owner_id is None or owner is None:
        return None
    bound = _FLEET_OWNER.get()
    bound_prelocked = bound[1] if bound is not None and bound[0] == owner_id else None
    with _lock:
        fleet = _fleets.get(owner_id) or _NamedWorkerFleet(owner_id)
        if bound_prelocked:
            for name, spec in bound_prelocked.items():
                existing = fleet.prelocked.get(name)
                if existing is not None and existing != spec:
                    msg = f"Worker '{name}' has conflicting persisted worker specifications."
                    raise NamedWorkerError(msg)
        retained = owner.retain(fleet, fleet.release)
        if retained is None:
            return None
        if retained:
            _fleets[owner_id] = fleet
            fleet.holders += 1
        if bound_prelocked:
            fleet.prelocked.update(bound_prelocked)
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


def parse_named_worker_sandbox(
    sandbox: object,
    mount_dir: object,
    mount_read_only: object,
    *,
    runtime_paths: RuntimePaths | None,
) -> tuple[WorkerSandboxConfig | None, bool]:
    """Parse and validate sandbox options, raising ``NamedWorkerError`` on invalid combinations."""
    try:
        return parse_sandbox_config(
            sandbox,
            mount_dir,
            mount_read_only,
            runtime_class_name=configured_runtime_class_name(runtime_paths) if runtime_paths is not None else None,
        )
    except ValueError as exc:
        raise NamedWorkerError(str(exc)) from exc


def serialize_sandbox_config(sandbox: WorkerSandboxConfig | None) -> str | None:
    """Serialize one optional ``WorkerSandboxConfig`` to canonical JSON for durable lock storage."""
    if sandbox is None:
        return None
    return json.dumps(
        {"mount_dir": sandbox.mount_dir, "mount_read_only": sandbox.mount_read_only},
        sort_keys=True,
        separators=(",", ":"),
    )


def deserialize_sandbox_config(raw: str | None) -> WorkerSandboxConfig | None:
    """Deserialize and strictly validate one persisted ``sandbox_json`` lock value."""
    if raw is None:
        return None
    try:
        stored = WorkerSandboxConfig.from_stored_dict(json.loads(raw))
    except (TypeError, ValueError) as exc:
        msg = f"Persisted worker sandbox specification is invalid: {exc}"
        raise NamedWorkerError(msg) from exc
    if stored is None:
        msg = "Persisted worker sandbox specification is corrupted."
        raise NamedWorkerError(msg)
    parsed, _ = parse_named_worker_sandbox(True, stored.mount_dir, stored.mount_read_only, runtime_paths=None)
    if parsed is None or parsed.mount_dir != stored.mount_dir:
        msg = "Persisted worker sandbox specification is invalid."
        raise NamedWorkerError(msg)
    return parsed


def validate_named_worker_options(
    name: object,
    resource_profile: object,
    *,
    require_name: bool = True,
) -> tuple[str | None, ScriptResourceProfileName | None]:
    """Validate named worker ``name`` and ``resource_profile``, raising ``NamedWorkerError`` on bad values."""
    validated_name: str | None = None
    if require_name or name is not None:
        if not isinstance(name, str) or _NAME_PATTERN.fullmatch(name) is None:
            msg = "worker must be 1 to 32 lowercase letters, digits, or hyphens, and start with a letter or digit."
            raise NamedWorkerError(msg)
        validated_name = name
    if resource_profile is not None and (
        not isinstance(resource_profile, str) or resource_profile not in SCRIPT_RESOURCE_PROFILE_NAMES
    ):
        msg = "resource_profile must be small, standard, or large."
        raise NamedWorkerError(msg)
    return validated_name, resource_profile


def resolve_named_worker_scope(
    agent_name: str | None,
    config: Config | None,
    execution_identity: ToolExecutionIdentity | None,
    runtime_paths: RuntimePaths,
) -> tuple[str, frozenset[str] | None]:
    """Verify named-worker eligibility and return ``(owner_scope_worker_key, private_agent_names)``."""
    if (refusal := named_workers_refusal(agent_name, config, runtime_paths)) is not None:
        raise NamedWorkerError(refusal)
    assert config is not None
    assert agent_name is not None
    owner_scope = (
        resolve_worker_key("user_agent", execution_identity, agent_name=agent_name) if execution_identity else None
    )
    if owner_scope is None:
        raise NamedWorkerError(_SCOPE_REFUSAL)
    return owner_scope, frozenset({agent_name}) if agent_policy(config, agent_name).is_private else frozenset()


def validate_effective_sandbox(
    runtime_paths: RuntimePaths,
    config: Config | None,
    owner_scope: str,
    private_agent_names: frozenset[str] | None,
    sandbox: WorkerSandboxConfig | None,
    arguments: Mapping[str, object],
) -> None:
    """Verify runtimeClassName, ``mindroom_output_path``, and on-disk ``mount_dir`` for one effective locked sandbox."""
    if sandbox is None:
        return
    if normalize_output_path_argument(arguments.get(OUTPUT_PATH_ARGUMENT)) is not None:
        msg = (
            "mindroom_output_path is not supported in a sandboxed worker; "
            "write to a file inside mount_dir or capture stdout directly."
        )
        raise NamedWorkerError(msg)
    if sandbox.mount_dir is not None:
        if (runtime_class := configured_runtime_class_name(runtime_paths)) is not None:
            raise NamedWorkerError(sandbox_mount_dir_runtime_class_refusal(runtime_class))
        sandbox_mount_relative_path(
            storage_root=runtime_paths.storage_root,
            worker_key=owner_scope,
            private_agent_names=private_agent_names,
            config=config,
            sandbox=sandbox,
            validate=True,
        )


def sandbox_mount_relative_path(
    *,
    storage_root: Path,
    worker_key: str,
    private_agent_names: frozenset[str] | None,
    config: Config | None,
    sandbox: WorkerSandboxConfig,
    validate: bool = True,
) -> Path | None:
    """Return the storage-relative mount path for ``sandbox`` (None for empty scratch), validating on disk."""
    try:
        return resolve_worker_sandbox_mount(
            storage_root=storage_root,
            worker_key=worker_key,
            private_agent_names=private_agent_names,
            config=config,
            sandbox=sandbox,
            validate=validate,
        )
    except ValueError as exc:
        raise NamedWorkerError(str(exc)) from exc


def named_worker_call(
    name: object,
    resource_profile: object,
    *,
    sandbox: object = None,
    mount_dir: object = None,
    mount_read_only: object = None,
    agent_name: str | None,
    config: Config | None,
    runtime_paths: RuntimePaths,
    turn_id: str | None = None,
) -> NamedWorkerCall | None:
    """Return the named worker a call's arguments ask for, or None for an ordinary shell call.

    The arguments are as the model sent them, so their types are checked here. An explicit
    ``named_worker_owner(...)`` binding takes precedence over ``turn_id`` as the owning fleet.
    """
    if sandbox is not None and not isinstance(sandbox, bool):
        msg = "sandbox must be a boolean."
        raise NamedWorkerError(msg)
    if mount_dir is not None and not isinstance(mount_dir, str):
        msg = "mount_dir must be a workspace-relative directory path string."
        raise NamedWorkerError(msg)
    if mount_read_only is not None and not isinstance(mount_read_only, bool):
        msg = "mount_read_only must be a boolean."
        raise NamedWorkerError(msg)
    sandbox_requested = sandbox is True or mount_dir is not None or mount_read_only is True
    if name is None and resource_profile is None and not sandbox_requested:
        return None
    if name is None:
        msg = (
            "resource_profile sizes a named worker; set worker too."
            if resource_profile is not None and not sandbox_requested
            else "sandbox, mount_dir, and mount_read_only configure a named worker; set worker too."
        )
        raise NamedWorkerError(msg)
    refusal = named_workers_refusal(agent_name, config, runtime_paths)
    if refusal is not None:
        raise NamedWorkerError(refusal)
    validated_name, validated_profile = validate_named_worker_options(name, resource_profile, require_name=True)
    assert validated_name is not None
    sandbox_config, sandbox_specified = parse_named_worker_sandbox(
        sandbox,
        mount_dir,
        mount_read_only,
        runtime_paths=runtime_paths,
    )
    fleet = _owned_fleet(current_named_worker_owner() or turn_id)
    if fleet is None:
        msg = (
            "A named worker belongs to a reply in a conversation, and this call runs outside one, "
            "or after its reply and that reply's background jobs ended."
        )
        raise NamedWorkerError(msg)
    return NamedWorkerCall(
        name=validated_name,
        resource_profile=validated_profile,
        agent_limit=_agent_limit(agent_name, config),
        runtime_paths=runtime_paths,
        _fleet=fleet,
        sandbox=sandbox_config,
        sandbox_specified=sandbox_specified,
    )


def ensure_named_worker(
    backend: WorkerBackend,
    spec: WorkerSpec,
    call: NamedWorkerCall,
    *,
    progress_sink: ProgressSink | None = None,
) -> tuple[WorkerHandle, WorkerSpec]:
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
            resource_profile, sandbox = call._fleet.admit(call, worker_key)
            spec = replace(
                spec,
                resource_profile=resource_profile,
                sandbox=sandbox,
                mirror_grantable_credentials=spec.mirror_grantable_credentials and sandbox is None,
            )
            _starting[worker_key] += 1
    try:
        return backend.ensure_worker(spec, progress_sink=progress_sink), spec
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
