"""The API's published config snapshots: loading, committing writes, and reading them per request."""

from __future__ import annotations

import threading
import weakref
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, NoReturn

from fastapi import FastAPI, HTTPException, Request

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.commit import ConfigCommit, ConfigComposedFromIncludesError, write_config_commit
from mindroom.config.load_attempt import (
    ConfigLoad,
    ConfigLoadResult,
    config_error_detail,
    load_config_attempt,
)
from mindroom.config.main import CONFIG_LOAD_USER_ERROR_TYPES, Config
from mindroom.event_journal.opening import pending_event_journal_restart
from mindroom.knowledge.status import reconcile_knowledge_mode_transition_states
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.settings import pending_background_tool_jobs_restart

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom import constants
    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.api.computers import ComputerRuntime
    from mindroom.api.mcp_gateway import GatewayRuntime
    from mindroom.api.usage_export import UsageExportRunner
    from mindroom.budgets.monitor import BudgetMonitor
    from mindroom.config.reload_status import ConfigReloadStatus
    from mindroom.external_triggers.store import TriggerDeliverySnapshot
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.watch import KnowledgeSourceWatcher
    from mindroom.response_activity import ActiveResponseIdentity, ActiveScriptRunInfo
    from mindroom.response_admission import ResponseAdmissionGate
    from mindroom.scheduling.runtime import ScheduledTaskRuntime
    from mindroom.thread_export.workspace_sync import WorkspaceThreadExportRunner
    from mindroom.worker_computer.sessions import ComputerSessionStore
    from mindroom.workers.backend import WorkerBackend

logger = get_logger(__name__)
_REQUEST_SNAPSHOT_SCOPE_KEY = "api_snapshot"
CONFIG_GENERATION_HEADER = "x-mindroom-config-generation"
CONFIG_USES_INCLUDES_HEADER = "x-mindroom-config-uses-includes"
CONFIG_PENDING_RESTART_HEADER = "x-mindroom-config-pending-restart"
_REGISTERED_API_APPS: weakref.WeakSet[FastAPI] = weakref.WeakSet()
_REGISTERED_API_APPS_LOCK = threading.Lock()


@dataclass
class ApiSnapshot:
    """One published API runtime snapshot.

    ``generation`` is what consumers bind to, so it only advances when the
    config they see actually changes. ``revision`` advances on every
    publication without exception, including ones that deliberately leave the
    generation alone -- a refused or failed load. Writers compare-and-swap on
    ``revision``: a request that pinned a snapshot before such a publication
    would otherwise still pass a generation check and commit its stale copy of
    the payload over whatever the operator had just edited on disk.
    """

    generation: int
    runtime_paths: constants.RuntimePaths
    revision: int = 0
    runtime_config: Config | None = None
    config_load_result: ConfigLoadResult | None = None
    source_fingerprint: str | None = None
    source_files: frozenset[Path] | None = None
    uses_includes: bool | None = None
    auth_state: Any | None = None


@dataclass
class ApiState:
    """Stable holder for the current API runtime snapshot."""

    config_lock: threading.Lock
    snapshot: ApiSnapshot


@dataclass(frozen=True)
class ExternalTriggerRuntime:
    """Runtime objects needed to deliver accepted external triggers."""

    client: object
    conversation_reader: object
    config_generation: int
    is_trigger_snapshot_ready: Callable[[TriggerDeliverySnapshot], Awaitable[bool]]
    agent_reply_memberships: AgentReplyMembershipIndex
    response_admission_gate: ResponseAdmissionGate
    wait_for_admission_or_shutdown: Callable[[], Awaitable[bool]]


@dataclass
class _MindroomAppState:
    """Single typed namespace for FastAPI ``app.state`` attributes used across the API."""

    api_state: ApiState | None = None
    api_auth_account_id: str | None = None
    orchestrator_knowledge_refresh_scheduler: KnowledgeRefreshScheduler | None = None
    knowledge_source_watcher: KnowledgeSourceWatcher | None = None
    knowledge_refresh_scheduler: KnowledgeRefreshScheduler | None = None
    thread_export_runner: WorkspaceThreadExportRunner | None = None
    leave_matrix_room: Callable[[str, str], Awaitable[bool]] | None = None
    external_trigger_runtime: ExternalTriggerRuntime | None = None
    # Each bound integration's runtime by integration name; the integration's own routes read its entry.
    integration_runtimes: dict[str, object] = field(default_factory=dict)
    agent_reply_memberships: AgentReplyMembershipIndex = field(default_factory=AgentReplyMembershipIndex)
    response_admission_gate: ResponseAdmissionGate | None = None
    budget_monitor: BudgetMonitor | None = None
    # The embedded runtime's scheduled-task owner, whose bots back schedule edits the router may not write.
    scheduled_tasks: ScheduledTaskRuntime | None = None
    openai_responses: set[ActiveResponseIdentity] = field(default_factory=set)
    active_calls: Callable[[], list[ActiveResponseIdentity]] | None = None
    active_script_runs: Callable[[], Awaitable[list[ActiveScriptRunInfo]]] | None = None
    config_reload_status: Callable[[], ConfigReloadStatus] | None = None
    script_worker_keepalive: Callable[[WorkerBackend], None] | None = None
    mcp_gateway_runtime: GatewayRuntime | None = None
    computer_runtime: ComputerRuntime | None = None
    computer_sessions: ComputerSessionStore | None = None
    agent_cli_registry: TurnToolRegistry | None = None
    usage_export_runner: UsageExportRunner | None = None


def ensure_app_state(api_app: FastAPI) -> _MindroomAppState:
    """Bind (or return) the :class:`MindroomAppState` for ``api_app``."""
    existing = getattr(api_app.state, "mindroom_app_state", None)
    if isinstance(existing, _MindroomAppState):
        return existing
    state = _MindroomAppState()
    api_app.state.mindroom_app_state = state
    return state


def app_state(api_app: FastAPI) -> _MindroomAppState:
    """Return the :class:`MindroomAppState` bound to ``api_app``."""
    state = getattr(api_app.state, "mindroom_app_state", None)
    if not isinstance(state, _MindroomAppState):
        msg = "MindRoom app state is not initialized"
        raise TypeError(msg)
    return state


def require_api_state(api_app: FastAPI) -> ApiState:
    """Return the published :class:`ApiState`, raising ``TypeError`` if not initialized."""
    api_state = app_state(api_app).api_state
    if api_state is None:
        msg = "API context is not initialized"
        raise TypeError(msg)
    return api_state


def _raise_for_config_load_result(result: ConfigLoadResult | None) -> None:
    """Raise HTTPException when the cached config state reflects a failed load."""
    if result is None or result.success:
        return
    raise HTTPException(
        status_code=result.error_status_code or 500,
        detail=result.error_detail or "Failed to load configuration",
    )


def _raise_missing_loaded_config() -> NoReturn:
    """Raise the shared missing-config HTTP error used by cached API reads and writes."""
    raise HTTPException(status_code=500, detail="Failed to load configuration")


_CONFIG_COMPOSED_FROM_INCLUDES_ERROR_CODE = "config_composed_from_includes"


def _composed_from_includes_http_error(exc: ConfigComposedFromIncludesError) -> HTTPException:
    """Return the includes-rejection 409 with a machine-readable code.

    The code lets clients tell this permanent rejection apart from the
    retryable stale-write 409, which carries a plain string detail.
    """
    return HTTPException(
        status_code=409,
        detail={"code": _CONFIG_COMPOSED_FROM_INCLUDES_ERROR_CODE, "message": str(exc)},
    )


def register_api_app(api_app: FastAPI) -> None:
    """Register one live API app so external config writers can advance its snapshot."""
    with _REGISTERED_API_APPS_LOCK:
        _REGISTERED_API_APPS.add(api_app)


def _registered_api_states() -> list[ApiState]:
    """Return all live API states that still expose config state; the caller holds the registry lock."""
    states: list[ApiState] = []
    for api_app in list(_REGISTERED_API_APPS):
        try:
            states.append(require_api_state(api_app))
        except TypeError:
            continue
    return states


def request_snapshot(request: Request) -> ApiSnapshot | None:
    """Return the request-bound API snapshot, if one was pinned earlier."""
    snapshot = request.scope.get(_REQUEST_SNAPSHOT_SCOPE_KEY)
    return snapshot if isinstance(snapshot, ApiSnapshot) else None


def store_request_snapshot(request: Request, snapshot: ApiSnapshot) -> ApiSnapshot:
    """Pin one API snapshot to the current request."""
    request.scope[_REQUEST_SNAPSHOT_SCOPE_KEY] = snapshot
    return snapshot


def bind_current_request_snapshot(request: Request) -> ApiSnapshot:
    """Pin the app's current published snapshot to the current request."""
    existing = request_snapshot(request)
    if existing is not None:
        return existing
    app_state = require_api_state(request.app)
    with app_state.config_lock:
        return store_request_snapshot(request, app_state.snapshot)


def rebind_current_request_snapshot(request: Request) -> ApiSnapshot:
    """Replace a request's pinned snapshot with the app's current publication."""
    app_state = require_api_state(request.app)
    with app_state.config_lock:
        return store_request_snapshot(request, app_state.snapshot)


def _request_or_current_snapshot(request: Request) -> ApiSnapshot:
    """Return the request-bound snapshot when present, else the current app snapshot."""
    bound_snapshot = request_snapshot(request)
    if bound_snapshot is not None:
        return bound_snapshot
    return require_api_state(request.app).snapshot


class _Keep(Enum):
    """Default for a ``_published_snapshot`` field the caller leaves unchanged."""

    FIELD = auto()


def _published_snapshot(
    snapshot: ApiSnapshot,
    *,
    increment_generation: bool = True,
    runtime_paths: constants.RuntimePaths | _Keep = _Keep.FIELD,
    runtime_config: Config | None | _Keep = _Keep.FIELD,
    config_load_result: ConfigLoadResult | None | _Keep = _Keep.FIELD,
    source_fingerprint: str | None | _Keep = _Keep.FIELD,
    source_files: frozenset[Path] | None | _Keep = _Keep.FIELD,
    uses_includes: bool | None | _Keep = _Keep.FIELD,
    auth_state: object = _Keep.FIELD,
) -> ApiSnapshot:
    """Return ``snapshot`` with the given fields as one new publication, always with a fresh commit revision."""
    return replace(
        snapshot,
        generation=snapshot.generation + 1 if increment_generation else snapshot.generation,
        revision=snapshot.revision + 1,
        runtime_paths=snapshot.runtime_paths if runtime_paths is _Keep.FIELD else runtime_paths,
        runtime_config=snapshot.runtime_config if runtime_config is _Keep.FIELD else runtime_config,
        config_load_result=snapshot.config_load_result if config_load_result is _Keep.FIELD else config_load_result,
        source_fingerprint=snapshot.source_fingerprint if source_fingerprint is _Keep.FIELD else source_fingerprint,
        source_files=snapshot.source_files if source_files is _Keep.FIELD else source_files,
        uses_includes=snapshot.uses_includes if uses_includes is _Keep.FIELD else uses_includes,
        auth_state=snapshot.auth_state if auth_state is _Keep.FIELD else auth_state,
    )


def _stale_snapshot_error() -> HTTPException:
    """Return the shared stale-write error used when state changed mid-request."""
    return HTTPException(
        status_code=409,
        detail="Configuration changed while request was in progress. Retry the operation.",
    )


def api_runtime_paths(request: Request) -> constants.RuntimePaths:
    """Return the API request's committed runtime paths."""
    return _request_or_current_snapshot(request).runtime_paths


def committed_generation(request: Request) -> int:
    """Return the committed snapshot generation visible to one request."""
    return _request_or_current_snapshot(request).generation


def commit_config(runtime_paths: constants.RuntimePaths, commit: ConfigCommit, *, loaded: Config) -> None:
    """Commit a change derived from `loaded`, refusing it when the file changed since, and publish it."""
    _commit(runtime_paths, commit, loaded=loaded, writer=None)


def commit_dashboard_config(
    request: Request,
    build: Callable[[constants.RuntimePaths], ConfigCommit],
    *,
    expected_generation: int | None,
) -> ApiSnapshot:
    """Commit one dashboard save built against the request's snapshot, and return the published snapshot.

    Blocking: `build` validates the new config, which loads plugins, so callers run this in a worker thread.
    A full replacement is exempt from the file check, so a save can repair a broken file no load has read.
    """
    state = require_api_state(request.app)
    pinned = bind_current_request_snapshot(request)
    if expected_generation is not None and pinned.generation != expected_generation:
        raise _stale_snapshot_error()
    try:
        published = _commit(pinned.runtime_paths, build(pinned.runtime_paths), loaded=None, writer=(state, pinned))
    except HTTPException:
        raise
    except ConfigComposedFromIncludesError as exc:
        raise _composed_from_includes_http_error(exc) from exc
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        raise HTTPException(status_code=422, detail=config_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {exc!s}") from exc
    assert published is not None
    return published


def _commit(
    runtime_paths: constants.RuntimePaths,
    commit: ConfigCommit,
    *,
    loaded: Config | None,
    writer: tuple[ApiState, ApiSnapshot] | None,
) -> ApiSnapshot | None:
    """Write `commit` and publish it to every API state on `runtime_paths` atomically with the write.

    A dashboard `writer` names its state and the snapshot it authored against, which must still be current;
    its new snapshot is returned. Every registered state is locked before filtering by runtime, and no app
    registers meanwhile, so no state can move onto `runtime_paths` and miss the publication. API locks always
    come before the config file lock.
    """
    with _REGISTERED_API_APPS_LOCK, ExitStack() as stack:
        states = {id(state): state for state in _registered_api_states()}
        if writer is not None:
            states[id(writer[0])] = writer[0]
        for state in sorted(states.values(), key=id):
            stack.enter_context(state.config_lock)
        if writer is not None:
            current = writer[0].snapshot
            if current.revision != writer[1].revision or current.runtime_paths != writer[1].runtime_paths:
                raise _stale_snapshot_error()
        targets = [state for state in states.values() if state.snapshot.runtime_paths == runtime_paths]
        write_config_commit(
            runtime_paths,
            commit,
            loaded=loaded,
            committed_uses_includes=any(state.snapshot.uses_includes for state in targets),
        )
        transitions = [(state.snapshot.runtime_config, commit.config) for state in targets]
        for state in targets:
            state.snapshot = _published_snapshot(
                state.snapshot,
                runtime_config=commit.config,
                config_load_result=ConfigLoadResult(success=True, uses_includes=commit.config.uses_includes),
                source_fingerprint=commit.config.source_fingerprint,
                source_files=commit.config.source_files,
                uses_includes=commit.config.uses_includes,
            )
        published = writer[0].snapshot if writer is not None else None
    _reconcile_knowledge_modes(transitions, runtime_paths)
    return published


def _reconcile_knowledge_modes(
    transitions: list[tuple[Config | None, Config]],
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Mark semantic indexes stale for each published config that moved a knowledge base across modes."""
    seen: set[int] = set()
    for previous, current in transitions:
        if previous is None or previous is current or id(previous) in seen:
            continue
        seen.add(id(previous))
        reconcile_knowledge_mode_transition_states(previous, current, runtime_paths)


def load_config_into_app(runtime_paths: constants.RuntimePaths, api_app: FastAPI) -> ConfigLoad:
    """Load config from disk into one API app's committed config cache unless a newer commit wins."""
    initial_state = require_api_state(api_app)
    snapshot = initial_state.snapshot
    attempt = load_config_attempt(runtime_paths)
    result = attempt.result
    with initial_state.config_lock:
        current_state = require_api_state(api_app)
        current = current_state.snapshot
        if current.revision != snapshot.revision or current.runtime_paths != runtime_paths:
            logger.info(
                "Discarding API config load superseded by a newer commit",
                load_config_path=str(runtime_paths.config_path),
                active_config_path=str(current.runtime_paths.config_path),
            )
            return replace(attempt, superseded=True)
        same_source = (
            attempt.source_fingerprint is not None and attempt.source_fingerprint == current.source_fingerprint
        )
        # A failed load publishes the union of the last good source set and the
        # files the failed attempt read, so the watcher never loses last-good
        # coverage while still covering newly added include files whose edit
        # broke the config; the next successful load replaces the union with
        # the real set, shrinking it back.
        source_files = attempt.source_files
        published_source_files = source_files if source_files is not None else current.source_files
        if not result.success and source_files is not None and current.source_files is not None:
            published_source_files = source_files | current.source_files
        published_uses_includes = result.uses_includes if result.uses_includes is not None else current.uses_includes
        if not result.success and current.uses_includes:
            published_uses_includes = True
        current_state.snapshot = _published_snapshot(
            current,
            increment_generation=not same_source,
            runtime_config=attempt.config if attempt.config is not None else current.runtime_config,
            config_load_result=result,
            source_fingerprint=attempt.source_fingerprint,
            source_files=published_source_files,
            uses_includes=published_uses_includes,
        )
    if attempt.config is not None:
        _reconcile_knowledge_modes([(current.runtime_config, attempt.config)], runtime_paths)
    return attempt


def _loaded_runtime_config(snapshot: ApiSnapshot) -> tuple[Config, constants.RuntimePaths]:
    """Return the snapshot's validated runtime config and runtime, or raise its load failure or the missing-config error."""
    _raise_for_config_load_result(snapshot.config_load_result)
    if snapshot.runtime_config is None:
        _raise_missing_loaded_config()
    return snapshot.runtime_config, snapshot.runtime_paths


def read_app_committed_runtime_config(
    api_app: FastAPI,
) -> tuple[Config, constants.RuntimePaths]:
    """Read one validated runtime config and runtime from the same published snapshot."""
    initial_state = require_api_state(api_app)
    with initial_state.config_lock:
        return _loaded_runtime_config(require_api_state(api_app).snapshot)


def read_committed_runtime_config(
    request: Request,
) -> tuple[Config, constants.RuntimePaths]:
    """Read the request's config only while the current config file is valid, raising the last load's failure."""
    return _loaded_runtime_config(_request_or_current_snapshot(request))


def write_committed_config[T](
    request: Request,
    mutate: Callable[[dict[str, Any]], T],
) -> T:
    """Apply one mutation to the request's valid committed config and commit the result. Blocking."""
    results: list[T] = []

    def build(runtime_paths: constants.RuntimePaths) -> ConfigCommit:
        config, _ = read_committed_runtime_config(request)
        payload = config.authored_model_dump()
        results.append(mutate(payload))
        return ConfigCommit.from_payload(payload, runtime_paths)

    commit_dashboard_config(request, build, expected_generation=None)
    return results[0]


def config_pending_restart(request: Request) -> bool:
    """Report authored startup-only settings that differ from the running instance."""
    snapshot = _request_or_current_snapshot(request)
    if snapshot.runtime_config is None:
        return False
    return pending_event_journal_restart(
        snapshot.runtime_config,
        snapshot.runtime_paths,
    ) or pending_background_tool_jobs_restart(snapshot.runtime_config, snapshot.runtime_paths)


def config_uses_includes(request: Request) -> bool:
    """Return whether the committed config uses include or expansion tags."""
    snapshot = _request_or_current_snapshot(request)
    return snapshot.uses_includes is True


def read_raw_config_source(request: Request) -> str:
    """Read the raw config source text for the current runtime."""
    snapshot = _request_or_current_snapshot(request)
    try:
        return snapshot.runtime_paths.config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Recovery still needs the raw source visible even when the on-disk file
        # contains unreadable bytes. Replacement characters keep the editor usable.
        return snapshot.runtime_paths.config_path.read_bytes().decode("utf-8", errors="replace")
