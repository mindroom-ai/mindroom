"""The API's published config snapshots: loading, committing writes, and reading them per request."""

from __future__ import annotations

import threading
import weakref
from contextlib import ExitStack
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request

from mindroom.api.app_state import app_state
from mindroom.config.commit import ConfigCommit, ConfigComposedFromIncludesError, write_config_commit
from mindroom.config.load_attempt import (
    ConfigLoad,
    config_error_detail,
    load_config_attempt,
)
from mindroom.config.main import (
    CONFIG_LOAD_USER_ERROR_TYPES,
    Config,
    ConfigRuntimeValidationError,
)
from mindroom.event_journal.opening import pending_event_journal_restart
from mindroom.knowledge.status import reconcile_knowledge_mode_transition_states
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.settings import pending_background_tool_jobs_restart

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom import constants

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
    load_error: Exception | None = None
    source_fingerprint: str | None = None
    uses_includes: bool = False


@dataclass
class ApiState:
    """Stable holder for the current API runtime snapshot."""

    config_lock: threading.Lock
    snapshot: ApiSnapshot


def require_api_state(api_app: FastAPI) -> ApiState:
    """Return the published :class:`ApiState`, raising ``TypeError`` if not initialized."""
    api_state = app_state(api_app).api_state
    if api_state is None:
        msg = "API context is not initialized"
        raise TypeError(msg)
    return api_state


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


def _request_snapshot(request: Request) -> ApiSnapshot | None:
    """Return the request-bound API snapshot, if one was pinned earlier."""
    snapshot = request.scope.get(_REQUEST_SNAPSHOT_SCOPE_KEY)
    return snapshot if isinstance(snapshot, ApiSnapshot) else None


def _store_request_snapshot(request: Request, snapshot: ApiSnapshot) -> ApiSnapshot:
    """Pin one API snapshot to the current request."""
    request.scope[_REQUEST_SNAPSHOT_SCOPE_KEY] = snapshot
    return snapshot


def bind_current_request_snapshot(request: Request) -> ApiSnapshot:
    """Pin the app's current published snapshot to the current request."""
    existing = _request_snapshot(request)
    if existing is not None:
        return existing
    state = require_api_state(request.app)
    with state.config_lock:
        return _store_request_snapshot(request, state.snapshot)


def rebind_current_request_snapshot(request: Request) -> ApiSnapshot:
    """Move a request to the app's newest publication of the runtime it authenticated against.

    A request whose app moved to another runtime meanwhile is stale: its auth state names the old one.
    """
    state = require_api_state(request.app)
    with state.config_lock:
        current = state.snapshot
    pinned = _request_snapshot(request)
    if pinned is not None and pinned.runtime_paths != current.runtime_paths:
        raise _stale_snapshot_error()
    return _store_request_snapshot(request, current)


def _request_or_current_snapshot(request: Request) -> ApiSnapshot:
    """Return the request-bound snapshot when present, else the current app snapshot."""
    bound_snapshot = _request_snapshot(request)
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
    load_error: Exception | None | _Keep = _Keep.FIELD,
    source_fingerprint: str | None | _Keep = _Keep.FIELD,
    uses_includes: bool | _Keep = _Keep.FIELD,
) -> ApiSnapshot:
    """Return ``snapshot`` with the given fields as one new publication, always with a fresh commit revision."""
    return replace(
        snapshot,
        generation=snapshot.generation + 1 if increment_generation else snapshot.generation,
        revision=snapshot.revision + 1,
        runtime_paths=snapshot.runtime_paths if runtime_paths is _Keep.FIELD else runtime_paths,
        runtime_config=snapshot.runtime_config if runtime_config is _Keep.FIELD else runtime_config,
        load_error=snapshot.load_error if load_error is _Keep.FIELD else load_error,
        source_fingerprint=snapshot.source_fingerprint if source_fingerprint is _Keep.FIELD else source_fingerprint,
        uses_includes=snapshot.uses_includes if uses_includes is _Keep.FIELD else uses_includes,
    )


def publish_runtime_paths(state: ApiState, runtime_paths: constants.RuntimePaths) -> bool:
    """Publish the snapshot again for ``runtime_paths``, dropping what was loaded for other paths; return whether they changed."""
    with state.config_lock:
        current = state.snapshot
        changed = current.runtime_paths != runtime_paths
        state.snapshot = (
            _published_snapshot(
                current,
                runtime_paths=runtime_paths,
                runtime_config=None,
                load_error=None,
                source_fingerprint=None,
                uses_includes=False,
            )
            if changed
            else _published_snapshot(current)
        )
    return changed


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
    A save replaces the whole config, so it is exempt from the file check and can repair a broken file no load
    has read.
    """
    state = require_api_state(request.app)
    pinned = bind_current_request_snapshot(request)
    if expected_generation is not None and pinned.generation != expected_generation:
        raise _stale_snapshot_error()
    try:
        commit = build(pinned.runtime_paths)
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        raise HTTPException(status_code=422, detail=config_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {exc!s}") from exc
    try:
        published = _commit(
            pinned.runtime_paths,
            commit,
            loaded=None,
            writer=_DashboardWriter(state=state, pinned=pinned),
        )
    except HTTPException:
        raise
    except ConfigComposedFromIncludesError as exc:
        # The code tells this permanent rejection apart from the retryable stale-write 409's plain string detail.
        raise HTTPException(
            status_code=409,
            detail={"code": "config_composed_from_includes", "message": str(exc)},
        ) from exc
    except ConfigRuntimeValidationError as exc:
        raise HTTPException(status_code=422, detail=config_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {exc!s}") from exc
    assert published is not None
    # The save replaced exactly the pinned config: the writer's revision check proved nothing was published between.
    _reconcile_knowledge_mode(pinned.runtime_config, commit.config, pinned.runtime_paths)
    return published


@dataclass(frozen=True)
class _DashboardWriter:
    """A dashboard save's own API state and the snapshot its request authored against."""

    state: ApiState
    pinned: ApiSnapshot


def _commit(
    runtime_paths: constants.RuntimePaths,
    commit: ConfigCommit,
    *,
    loaded: Config | None,
    writer: _DashboardWriter | None,
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
            states[id(writer.state)] = writer.state
        for state in sorted(states.values(), key=id):
            stack.enter_context(state.config_lock)
        if writer is not None:
            current = writer.state.snapshot
            if current.revision != writer.pinned.revision or current.runtime_paths != writer.pinned.runtime_paths:
                raise _stale_snapshot_error()
        targets = [state for state in states.values() if state.snapshot.runtime_paths == runtime_paths]
        write_config_commit(
            runtime_paths,
            commit,
            loaded=loaded,
            committed_uses_includes=any(state.snapshot.uses_includes for state in targets),
        )
        for state in targets:
            state.snapshot = _published_snapshot(
                state.snapshot,
                runtime_config=commit.config,
                load_error=None,
                source_fingerprint=commit.config.source_fingerprint,
                uses_includes=commit.config.uses_includes,
            )
        return writer.state.snapshot if writer is not None else None


def _reconcile_knowledge_mode(
    previous: Config | None,
    current: Config,
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Mark semantic indexes stale where `current` moved a knowledge base across modes from the config it replaced.

    The API reconciles only what it publishes on its own, its dashboard saves; the runtime reconciles each config it
    applies, which covers its own load and every tool commit.
    """
    if previous is not None:
        reconcile_knowledge_mode_transition_states(previous, current, runtime_paths)


def load_config_into_app(runtime_paths: constants.RuntimePaths, api_app: FastAPI) -> ConfigLoad:
    """Load config from disk into one API app's committed config cache unless a newer commit wins."""
    state = require_api_state(api_app)
    snapshot = state.snapshot
    attempt = load_config_attempt(runtime_paths)
    with state.config_lock:
        current = state.snapshot
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
        state.snapshot = _published_snapshot(
            current,
            increment_generation=not same_source,
            runtime_config=attempt.config if attempt.config is not None else current.runtime_config,
            load_error=attempt.error,
            source_fingerprint=attempt.source_fingerprint,
            uses_includes=attempt.uses_includes is True or (attempt.error is not None and current.uses_includes),
        )
    return attempt


def _loaded_runtime_config(snapshot: ApiSnapshot) -> tuple[Config, constants.RuntimePaths]:
    """Return the snapshot's validated runtime config and runtime, or raise its load failure or the missing-config error."""
    error = snapshot.load_error
    if isinstance(error, CONFIG_LOAD_USER_ERROR_TYPES):
        raise HTTPException(status_code=422, detail=config_error_detail(error))
    if error is not None or snapshot.runtime_config is None:
        raise HTTPException(status_code=500, detail="Failed to load configuration")
    return snapshot.runtime_config, snapshot.runtime_paths


def read_app_committed_runtime_config(
    api_app: FastAPI,
) -> tuple[Config, constants.RuntimePaths]:
    """Read one validated runtime config and runtime from the same published snapshot."""
    state = require_api_state(api_app)
    with state.config_lock:
        return _loaded_runtime_config(state.snapshot)


def read_committed_runtime_config(
    request: Request,
) -> tuple[Config, constants.RuntimePaths]:
    """Read the request's config only while the current config file is valid, raising the last load's failure."""
    return _loaded_runtime_config(_request_or_current_snapshot(request))


def last_good_runtime_config(request: Request) -> Config | None:
    """Return the last config that loaded for the request's runtime, even while the current file is broken.

    None only before the first load. Routes that must keep working while an operator repairs the file read this.
    """
    return _request_or_current_snapshot(request).runtime_config


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
    return _request_or_current_snapshot(request).uses_includes


def read_raw_config_source(request: Request) -> str:
    """Read the raw config source text for the current runtime."""
    snapshot = _request_or_current_snapshot(request)
    try:
        return snapshot.runtime_paths.config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Recovery still needs the raw source visible even when the on-disk file
        # contains unreadable bytes. Replacement characters keep the editor usable.
        return snapshot.runtime_paths.config_path.read_bytes().decode("utf-8", errors="replace")
