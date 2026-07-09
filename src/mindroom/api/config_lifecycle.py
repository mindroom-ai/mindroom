"""Helpers for API config loading, writing, and file-watcher lifecycle."""

from __future__ import annotations

import hashlib
import threading
import weakref
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn, cast

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from mindroom import constants
from mindroom.config.main import (
    CONFIG_LOAD_USER_ERROR_TYPES,
    Config,
    ConfigRuntimeValidationError,
    RuntimeConfig,
    iter_config_validation_messages,
)
from mindroom.config.yaml_includes import (
    load_yaml_config_source,
    load_yaml_config_source_with_digests,
    partial_source_files,
    source_files_fingerprint,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.external_triggers.store import TriggerDeliverySnapshot
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.watch import KnowledgeSourceWatcher

logger = get_logger(__name__)
_UNSET = object()
_REQUEST_SNAPSHOT_SCOPE_KEY = "api_snapshot"
CONFIG_GENERATION_HEADER = "x-mindroom-config-generation"
CONFIG_USES_INCLUDES_HEADER = "x-mindroom-config-uses-includes"
_REGISTERED_API_APPS: weakref.WeakSet[FastAPI] = weakref.WeakSet()
_REGISTERED_API_APPS_LOCK = threading.Lock()


@dataclass(frozen=True)
class ConfigLoadResult:
    """Outcome of one API config-file load attempt."""

    success: bool
    error_status_code: int | None = None
    error_detail: object | None = None


@dataclass
class ApiSnapshot:
    """One published API runtime snapshot."""

    generation: int
    runtime_paths: constants.RuntimePaths
    config_data: dict[str, Any]
    runtime_config: RuntimeConfig | None = None
    config_load_result: ConfigLoadResult | None = None
    source_fingerprint: str | None = None
    source_files: frozenset[Path] | None = None
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
    conversation_cache: object
    config_generation: int
    is_trigger_snapshot_ready: Callable[[TriggerDeliverySnapshot], Awaitable[bool]]


@dataclass
class _MindroomAppState:
    """Single typed namespace for FastAPI ``app.state`` attributes used across the API."""

    api_state: ApiState | None = None
    api_auth_account_id: str | None = None
    orchestrator_knowledge_refresh_scheduler: KnowledgeRefreshScheduler | None = None
    knowledge_source_watcher: KnowledgeSourceWatcher | None = None
    knowledge_refresh_scheduler: KnowledgeRefreshScheduler | None = None
    external_trigger_runtime: ExternalTriggerRuntime | None = None


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


def _config_error_detail(
    exc: ValidationError | ConfigRuntimeValidationError | yaml.YAMLError | OSError | UnicodeError,
) -> list[dict[str, object]]:
    """Return one shared API error payload for invalid current config."""
    return [
        {
            "loc": tuple(location.split(" → ")) if " → " in location else (location,),
            "msg": message,
            "type": "value_error",
        }
        for location, message in iter_config_validation_messages(exc)
    ]


def _source_fingerprint(source: bytes | str) -> str:
    """Return the stable identity used for raw config stale-write protection."""
    source_bytes = source.encode("utf-8") if isinstance(source, str) else source
    return hashlib.sha256(source_bytes).hexdigest()


def _load_config_result(
    runtime_paths: constants.RuntimePaths,
) -> tuple[ConfigLoadResult, dict[str, Any] | None, RuntimeConfig | None, str | None, frozenset[Path] | None]:
    """Load and validate one config file without mutating shared app state."""
    source_fingerprint: str | None = None
    source_files: frozenset[Path] | None = None
    try:
        source_bytes = runtime_paths.config_path.read_bytes()
        source_fingerprint = _source_fingerprint(source_bytes)
        # Parse the bytes already read so a mid-load file edit can never publish
        # config under a fingerprint computed from different content.
        data, source_digests = load_yaml_config_source_with_digests(runtime_paths.config_path, source=source_bytes)
        source_files = frozenset(source_digests)
        source_fingerprint = source_files_fingerprint(runtime_paths.config_path, source_digests)
        runtime_config = RuntimeConfig.from_authored(
            Config.model_validate(data),
            runtime_paths,
            tolerate_plugin_load_errors=True,
        )
        validated_payload = runtime_config.authored_model_dump()
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        detail = _config_error_detail(exc)
        logger.warning(
            "Failed to load API config due to validation",
            config_path=str(runtime_paths.config_path),
            errors=detail,
        )
        if source_files is None:
            # A parse-time failure never reached the full set; the files read
            # so far keep the watcher covering a broken new include file.
            source_files = partial_source_files(exc)
        return (
            ConfigLoadResult(success=False, error_status_code=422, error_detail=detail),
            None,
            None,
            source_fingerprint,
            source_files,
        )
    except Exception:
        logger.exception("Failed to load API config", config_path=str(runtime_paths.config_path))
        return (
            ConfigLoadResult(success=False, error_status_code=500, error_detail="Failed to load configuration"),
            None,
            None,
            source_fingerprint,
            source_files,
        )
    else:
        logger.info(
            "loaded_agent_configuration",
            path=str(runtime_paths.config_path),
            source_file_count=len(source_files),
        )
        logger.info("loaded_agent_configuration_count", agent_count=len(runtime_config.agents))
        logger.info("Loaded API config", config_path=str(runtime_paths.config_path))
        return ConfigLoadResult(success=True), validated_payload, runtime_config, source_fingerprint, source_files


def _source_fingerprint_for_published_runtime_config(
    runtime_paths: constants.RuntimePaths,
    validated_payload: dict[str, Any],
) -> tuple[str, frozenset[Path] | None]:
    """Return the disk fingerprint and source set when the file still matches the runtime config.

    The source set is ``None`` when the published config cannot be tied to the
    on-disk files, so the caller keeps the snapshot's last known set.
    """
    canonical_source = yaml.dump(
        validated_payload,
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=True,
    )
    canonical_fingerprint = _source_fingerprint(canonical_source)
    result, disk_payload, _disk_config, disk_fingerprint, disk_source_files = _load_config_result(runtime_paths)
    if result.success and disk_payload == validated_payload and disk_fingerprint is not None:
        return disk_fingerprint, disk_source_files
    return canonical_fingerprint, None


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


class _ConfigComposedFromIncludesError(ConfigRuntimeValidationError):
    """Structured config write rejected because the config is split across include files."""


_CONFIG_COMPOSED_FROM_INCLUDES_MESSAGE = (
    "configuration is composed from multiple files via !include; edit the source files instead"
)
_CONFIG_COMPOSED_FROM_INCLUDES_ERROR_CODE = "config_composed_from_includes"


def _composed_from_includes_http_error(exc: _ConfigComposedFromIncludesError) -> HTTPException:
    """Return the includes-rejection 409 with a machine-readable code.

    The code lets clients tell this permanent rejection apart from the
    retryable stale-write 409, which carries a plain string detail.
    """
    return HTTPException(
        status_code=409,
        detail={"code": _CONFIG_COMPOSED_FROM_INCLUDES_ERROR_CODE, "message": str(exc)},
    )


def _raise_when_composed_from_includes(
    runtime_paths: constants.RuntimePaths,
    committed_source_files: frozenset[Path] | None,
) -> None:
    """Reject structured config writes that would silently flatten include files.

    The committed snapshot's source set is authoritative: it reflects the last
    successful load even when an included file has since become unreadable, so a
    split config cannot be flattened just because one include broke.
    """
    if committed_source_files is not None:
        if len(committed_source_files) > 1:
            raise _ConfigComposedFromIncludesError(_CONFIG_COMPOSED_FROM_INCLUDES_MESSAGE)
        return
    try:
        _, source_files = load_yaml_config_source(runtime_paths.config_path)
    except CONFIG_LOAD_USER_ERROR_TYPES:
        # With no committed source metadata, an unreadable or broken on-disk
        # config stays recoverable through structured replacement, exactly like
        # before includes existed.
        return
    if len(source_files) > 1:
        raise _ConfigComposedFromIncludesError(_CONFIG_COMPOSED_FROM_INCLUDES_MESSAGE)


def _save_config_to_file(
    config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
    *,
    committed_source_files: frozenset[Path] | None,
) -> str:
    """Save config to YAML file with deterministic ordering."""
    _raise_when_composed_from_includes(runtime_paths, committed_source_files)
    config_path = runtime_paths.config_path
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    source = yaml.dump(
        config,
        default_flow_style=False,
        sort_keys=True,
        allow_unicode=True,
    )
    tmp_path.write_text(source, encoding="utf-8")
    constants.safe_replace(tmp_path, config_path)
    return _source_fingerprint(source)


def _save_raw_config_source_to_file(
    source: str,
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Save raw config source text to the active config path."""
    config_path = runtime_paths.config_path
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp_path.write_text(source, encoding="utf-8")
    constants.safe_replace(tmp_path, config_path)


def _persist_runtime_validated_config(
    runtime_config: RuntimeConfig,
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Persist one validated config and immediately publish matching committed API snapshots."""
    validated_payload = runtime_config.authored_model_dump()
    matching_states = [state for state in _registered_api_states() if state.snapshot.runtime_paths == runtime_paths]
    if not matching_states:
        _save_config_to_file(validated_payload, runtime_paths=runtime_paths, committed_source_files=None)
        return

    with ExitStack() as stack:
        locked_snapshots: list[tuple[ApiState, ApiSnapshot]] = []
        for state in sorted(matching_states, key=id):
            stack.enter_context(state.config_lock)
            snapshot = state.snapshot
            if snapshot.runtime_paths != runtime_paths:
                continue
            locked_snapshots.append((state, snapshot))

        committed_source_files = next(
            (snapshot.source_files for _, snapshot in locked_snapshots if snapshot.source_files is not None),
            None,
        )
        source_fingerprint = _save_config_to_file(
            validated_payload,
            runtime_paths=runtime_paths,
            committed_source_files=committed_source_files,
        )
        for state, snapshot in locked_snapshots:
            state.snapshot = _published_snapshot(
                snapshot,
                config_data=deepcopy(validated_payload),
                runtime_config=runtime_config,
                config_load_result=ConfigLoadResult(success=True),
                source_fingerprint=source_fingerprint,
                source_files=frozenset({runtime_paths.config_path.resolve()}),
            )


def _validated_config_payload(
    raw_config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> tuple[RuntimeConfig, dict[str, Any]]:
    """Normalize and validate one config payload against the active runtime."""
    validated_config = RuntimeConfig.from_authored(Config.model_validate(raw_config), runtime_paths)
    return validated_config, validated_config.authored_model_dump()


def validate_and_persist_config_payload(
    raw_config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> RuntimeConfig:
    """Validate and persist one authored config payload against the active runtime."""
    validated_config, _ = _validated_config_payload(raw_config, runtime_paths)
    _persist_runtime_validated_config(validated_config, runtime_paths)
    return validated_config


def register_api_app(api_app: FastAPI) -> None:
    """Register one live API app so external config writers can advance its snapshot."""
    with _REGISTERED_API_APPS_LOCK:
        _REGISTERED_API_APPS.add(api_app)


def _registered_api_states() -> list[ApiState]:
    """Return all live API states that still expose config state."""
    with _REGISTERED_API_APPS_LOCK:
        apps = list(_REGISTERED_API_APPS)
    states: list[ApiState] = []
    for api_app in apps:
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


def _request_or_current_snapshot(request: Request) -> ApiSnapshot:
    """Return the request-bound snapshot when present, else the current app snapshot."""
    bound_snapshot = request_snapshot(request)
    if bound_snapshot is not None:
        return bound_snapshot
    return require_api_state(request.app).snapshot


def _published_snapshot(
    snapshot: ApiSnapshot,
    *,
    increment_generation: bool = True,
    runtime_paths: constants.RuntimePaths | None = None,
    config_data: dict[str, Any] | None = None,
    runtime_config: RuntimeConfig | None | object = _UNSET,
    config_load_result: ConfigLoadResult | None | object = _UNSET,
    source_fingerprint: str | None | object = _UNSET,
    source_files: frozenset[Path] | None | object = _UNSET,
    auth_state: object = _UNSET,
) -> ApiSnapshot:
    """Return one new published snapshot with an incremented generation."""
    updated_runtime_paths = snapshot.runtime_paths if runtime_paths is None else runtime_paths
    updated_config_data = snapshot.config_data if config_data is None else config_data
    updated_runtime_config = (
        snapshot.runtime_config if runtime_config is _UNSET else cast("RuntimeConfig | None", runtime_config)
    )
    updated_load_result = (
        snapshot.config_load_result
        if config_load_result is _UNSET
        else cast("ConfigLoadResult | None", config_load_result)
    )
    updated_source_fingerprint = (
        snapshot.source_fingerprint if source_fingerprint is _UNSET else cast("str | None", source_fingerprint)
    )
    updated_source_files = (
        snapshot.source_files if source_files is _UNSET else cast("frozenset[Path] | None", source_files)
    )
    updated_auth_state = snapshot.auth_state if auth_state is _UNSET else auth_state
    return replace(
        snapshot,
        generation=snapshot.generation + 1 if increment_generation else snapshot.generation,
        runtime_paths=updated_runtime_paths,
        config_data=updated_config_data,
        runtime_config=updated_runtime_config,
        config_load_result=updated_load_result,
        source_fingerprint=updated_source_fingerprint,
        source_files=updated_source_files,
        auth_state=updated_auth_state,
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


def _raise_if_generation_mismatch(snapshot: ApiSnapshot, expected_generation: int | None) -> None:
    """Reject writes authored against a stale client-side snapshot."""
    if expected_generation is None:
        return
    if snapshot.generation != expected_generation:
        raise _stale_snapshot_error()


def _build_mutated_config[T](
    snapshot: ApiSnapshot,
    mutate: Callable[[dict[str, Any]], T],
    runtime_paths: constants.RuntimePaths,
) -> tuple[T, dict[str, Any], RuntimeConfig]:
    """Build one validated config payload from a committed snapshot off-lock."""
    _raise_for_config_load_result(snapshot.config_load_result)
    if not snapshot.config_data:
        _raise_missing_loaded_config()
    candidate_config = deepcopy(snapshot.config_data)
    result = mutate(candidate_config)
    validated_config, validated_payload = _validated_config_payload(candidate_config, runtime_paths)
    return result, validated_payload, validated_config


def _commit_mutated_snapshot[T](
    api_app: FastAPI,
    initial_state: ApiState,
    *,
    expected_generation: int,
    runtime_paths: constants.RuntimePaths,
    validated_payload: dict[str, Any],
    validated_config: RuntimeConfig,
    result: T,
) -> T:
    """Commit one previously validated mutation if the targeted snapshot is still current."""
    with initial_state.config_lock:
        current_state = require_api_state(api_app)
        current = current_state.snapshot
        if current.generation != expected_generation or current.runtime_paths != runtime_paths:
            _raise_for_config_load_result(current.config_load_result)
            raise _stale_snapshot_error()
        source_fingerprint = _save_config_to_file(
            validated_payload,
            runtime_paths=runtime_paths,
            committed_source_files=current.source_files,
        )
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload,
            runtime_config=validated_config,
            config_load_result=ConfigLoadResult(success=True),
            source_fingerprint=source_fingerprint,
            source_files=frozenset({runtime_paths.config_path.resolve()}),
        )
        return result


def _validate_replacement_payload(
    new_config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> tuple[RuntimeConfig, dict[str, Any]]:
    """Validate one replacement config payload off-lock."""
    return _validated_config_payload(new_config, runtime_paths)


def _validate_raw_config_source(
    source: str,
    runtime_paths: constants.RuntimePaths,
) -> tuple[RuntimeConfig, dict[str, Any], frozenset[Path], str]:
    """Validate raw YAML source against the current runtime without mutating the live file.

    Parsing the source as the live config path keeps include semantics identical
    to the post-save reload (relative paths, containment, self-include cycles)
    and yields the same include-aware fingerprint the next disk load computes,
    so a raw save of a split config does not trigger a spurious generation bump.
    """
    data, source_digests = load_yaml_config_source_with_digests(
        runtime_paths.config_path,
        source=source.encode("utf-8"),
    )
    runtime_config = RuntimeConfig.from_authored(Config.model_validate(data), runtime_paths)
    source_fingerprint = source_files_fingerprint(runtime_paths.config_path, source_digests)
    return runtime_config, runtime_config.authored_model_dump(), frozenset(source_digests), source_fingerprint


def _commit_replaced_snapshot(
    api_app: FastAPI,
    initial_state: ApiState,
    *,
    expected_generation: int,
    runtime_paths: constants.RuntimePaths,
    validated_payload: dict[str, Any],
    validated_config: RuntimeConfig,
) -> int:
    """Commit one previously validated replacement payload if the snapshot is still current."""
    with initial_state.config_lock:
        current_state = require_api_state(api_app)
        current = current_state.snapshot
        if current.generation != expected_generation or current.runtime_paths != runtime_paths:
            raise _stale_snapshot_error()
        source_fingerprint = _save_config_to_file(
            validated_payload,
            runtime_paths=runtime_paths,
            committed_source_files=current.source_files,
        )
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload,
            runtime_config=validated_config,
            config_load_result=ConfigLoadResult(success=True),
            source_fingerprint=source_fingerprint,
            source_files=frozenset({runtime_paths.config_path.resolve()}),
        )
        return current_state.snapshot.generation


def _commit_raw_replaced_snapshot(
    api_app: FastAPI,
    initial_state: ApiState,
    *,
    expected_generation: int,
    runtime_paths: constants.RuntimePaths,
    validated_payload: dict[str, Any],
    validated_config: RuntimeConfig,
    source: str,
    source_files: frozenset[Path],
    source_fingerprint: str,
) -> int:
    """Commit one raw replacement payload if the targeted snapshot is still current."""
    with initial_state.config_lock:
        current_state = require_api_state(api_app)
        current = current_state.snapshot
        if current.generation != expected_generation or current.runtime_paths != runtime_paths:
            raise _stale_snapshot_error()
        _save_raw_config_source_to_file(source, runtime_paths=runtime_paths)
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload,
            runtime_config=validated_config,
            config_load_result=ConfigLoadResult(success=True),
            source_fingerprint=source_fingerprint,
            source_files=source_files,
        )
        return current_state.snapshot.generation


def _build_and_commit_mutation[T](
    api_app: FastAPI,
    mutate: Callable[[dict[str, Any]], T],
    *,
    error_prefix: str,
    initial_snapshot: ApiSnapshot | None = None,
) -> T:
    """Build one config mutation off-lock and commit it only if still current."""
    initial_state = require_api_state(api_app)
    if initial_snapshot is None:
        with initial_state.config_lock:
            snapshot = require_api_state(api_app).snapshot
    else:
        snapshot = initial_snapshot
    try:
        result, validated_payload, validated_config = _build_mutated_config(
            snapshot,
            mutate,
            snapshot.runtime_paths,
        )
        return _commit_mutated_snapshot(
            api_app,
            initial_state,
            expected_generation=snapshot.generation,
            runtime_paths=snapshot.runtime_paths,
            validated_payload=validated_payload,
            validated_config=validated_config,
            result=result,
        )
    except HTTPException:
        raise
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
    except _ConfigComposedFromIncludesError as e:
        raise _composed_from_includes_http_error(e) from e
    except ConfigRuntimeValidationError as e:
        raise HTTPException(status_code=422, detail=_config_error_detail(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{error_prefix}: {e!s}") from e


def _build_and_commit_replacement(
    api_app: FastAPI,
    new_config: dict[str, Any],
    *,
    error_prefix: str,
    initial_snapshot: ApiSnapshot | None = None,
    expected_generation: int | None = None,
) -> int:
    """Build one replacement payload off-lock and commit it only if still current."""
    initial_state = require_api_state(api_app)
    if initial_snapshot is None:
        with initial_state.config_lock:
            snapshot = require_api_state(api_app).snapshot
    else:
        snapshot = initial_snapshot
    try:
        _raise_if_generation_mismatch(snapshot, expected_generation)
        validated_config, validated_payload = _validate_replacement_payload(new_config, snapshot.runtime_paths)
        return _commit_replaced_snapshot(
            api_app,
            initial_state,
            expected_generation=snapshot.generation,
            runtime_paths=snapshot.runtime_paths,
            validated_payload=validated_payload,
            validated_config=validated_config,
        )
    except HTTPException:
        raise
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
    except _ConfigComposedFromIncludesError as e:
        raise _composed_from_includes_http_error(e) from e
    except ConfigRuntimeValidationError as e:
        raise HTTPException(status_code=422, detail=_config_error_detail(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{error_prefix}: {e!s}") from e


def _build_and_commit_raw_replacement(
    api_app: FastAPI,
    source: str,
    *,
    error_prefix: str,
    initial_snapshot: ApiSnapshot | None = None,
    expected_generation: int | None = None,
) -> int:
    """Build one raw replacement payload off-lock and commit it only if still current."""
    initial_state = require_api_state(api_app)
    if initial_snapshot is None:
        with initial_state.config_lock:
            snapshot = require_api_state(api_app).snapshot
    else:
        snapshot = initial_snapshot
    try:
        _raise_if_generation_mismatch(snapshot, expected_generation)
        validated_config, validated_payload, source_files, source_fingerprint = _validate_raw_config_source(
            source,
            snapshot.runtime_paths,
        )
        return _commit_raw_replaced_snapshot(
            api_app,
            initial_state,
            expected_generation=snapshot.generation,
            runtime_paths=snapshot.runtime_paths,
            validated_payload=validated_payload,
            validated_config=validated_config,
            source=source,
            source_files=source_files,
            source_fingerprint=source_fingerprint,
        )
    except HTTPException:
        raise
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        raise HTTPException(status_code=422, detail=_config_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{error_prefix}: {exc!s}") from exc


def load_config_into_app(runtime_paths: constants.RuntimePaths, api_app: FastAPI) -> bool:
    """Load config from disk into one API app's committed config cache."""
    initial_state = require_api_state(api_app)
    snapshot = initial_state.snapshot
    result, validated_payload, runtime_config, source_fingerprint, source_files = _load_config_result(runtime_paths)
    with initial_state.config_lock:
        current_state = require_api_state(api_app)
        current = current_state.snapshot
        if current.generation != snapshot.generation or current.runtime_paths != runtime_paths:
            logger.info(
                "Discarding stale API config load after runtime swap",
                load_config_path=str(runtime_paths.config_path),
                active_config_path=str(current.runtime_paths.config_path),
            )
            return False
        same_source = source_fingerprint is not None and source_fingerprint == current.source_fingerprint
        # A failed load publishes the union of the last good source set and the
        # files the failed attempt read, so the watcher never loses last-good
        # coverage while still covering newly added include files whose edit
        # broke the config; the next successful load replaces the union with
        # the real set, shrinking it back.
        published_source_files = source_files if source_files is not None else current.source_files
        if not result.success and source_files is not None and current.source_files is not None:
            published_source_files = source_files | current.source_files
        current_state.snapshot = _published_snapshot(
            current,
            increment_generation=not same_source,
            config_data=validated_payload if validated_payload is not None else current.config_data,
            runtime_config=runtime_config if runtime_config is not None else current.runtime_config,
            config_load_result=result,
            source_fingerprint=source_fingerprint,
            source_files=published_source_files,
        )
    return result.success


def _publish_runtime_config_into_app(
    runtime_config: RuntimeConfig,
    runtime_paths: constants.RuntimePaths,
    api_app: FastAPI,
) -> bool:
    """Publish one already-validated runtime config into one API app's committed cache."""
    initial_state = require_api_state(api_app)
    snapshot = initial_state.snapshot
    validated_payload = runtime_config.authored_model_dump()
    source_fingerprint, source_files = _source_fingerprint_for_published_runtime_config(
        runtime_paths,
        validated_payload,
    )
    with initial_state.config_lock:
        current_state = require_api_state(api_app)
        current = current_state.snapshot
        if current.runtime_paths != runtime_paths:
            logger.info(
                "Discarding stale API config publish after runtime swap",
                publish_config_path=str(runtime_paths.config_path),
                active_config_path=str(current.runtime_paths.config_path),
            )
            return False
        same_config = current.config_data == validated_payload
        if current.generation != snapshot.generation and not same_config:
            logger.info(
                "Discarding stale API config publish after config changed",
                publish_config_path=str(runtime_paths.config_path),
            )
            return False
        same_source = source_fingerprint == current.source_fingerprint
        current_state.snapshot = _published_snapshot(
            current,
            increment_generation=not (same_source or same_config),
            config_data=validated_payload,
            runtime_config=runtime_config,
            config_load_result=ConfigLoadResult(success=True),
            source_fingerprint=source_fingerprint,
            # A publish that cannot be tied to disk keeps the last known source
            # set so the watcher still covers the previous include files.
            source_files=source_files if source_files is not None else current.source_files,
        )
    return True


def read_app_committed_runtime_config(
    api_app: FastAPI,
) -> tuple[RuntimeConfig, constants.RuntimePaths]:
    """Read one validated runtime config and runtime from the same published snapshot."""
    initial_state = require_api_state(api_app)
    with initial_state.config_lock:
        snapshot = require_api_state(api_app).snapshot
        _raise_for_config_load_result(snapshot.config_load_result)
        if not snapshot.config_data or snapshot.runtime_config is None:
            _raise_missing_loaded_config()
        return snapshot.runtime_config, snapshot.runtime_paths


def read_committed_config[T](
    request: Request,
    reader: Callable[[dict[str, Any]], T],
) -> T:
    """Read committed API config only when the current on-disk config is valid."""
    snapshot = _request_or_current_snapshot(request)
    _raise_for_config_load_result(snapshot.config_load_result)
    if not snapshot.config_data:
        _raise_missing_loaded_config()
    return reader(snapshot.config_data)


def read_committed_config_and_runtime[T](
    request: Request,
    reader: Callable[[dict[str, Any]], T],
) -> tuple[T, constants.RuntimePaths]:
    """Read committed API config and runtime from one coherent request snapshot."""
    snapshot = _request_or_current_snapshot(request)
    _raise_for_config_load_result(snapshot.config_load_result)
    if not snapshot.config_data:
        _raise_missing_loaded_config()
    return reader(snapshot.config_data), snapshot.runtime_paths


def read_committed_runtime_config(
    request: Request,
) -> tuple[RuntimeConfig, constants.RuntimePaths]:
    """Read one validated runtime config and runtime from one coherent request snapshot."""
    snapshot = _request_or_current_snapshot(request)
    _raise_for_config_load_result(snapshot.config_load_result)
    if not snapshot.config_data or snapshot.runtime_config is None:
        _raise_missing_loaded_config()
    return snapshot.runtime_config, snapshot.runtime_paths


def write_committed_config[T](
    request: Request,
    mutate: Callable[[dict[str, Any]], T],
    *,
    error_prefix: str,
) -> T:
    """Mutate committed API config from the last valid cache snapshot."""
    return _build_and_commit_mutation(
        request.app,
        mutate,
        error_prefix=error_prefix,
        initial_snapshot=request_snapshot(request),
    )


def replace_committed_config(
    request: Request,
    new_config: dict[str, Any],
    *,
    error_prefix: str,
    expected_generation: int | None = None,
) -> int:
    """Replace the entire committed API config with one freshly validated payload."""
    return _build_and_commit_replacement(
        request.app,
        new_config,
        error_prefix=error_prefix,
        initial_snapshot=request_snapshot(request),
        expected_generation=expected_generation,
    )


def config_uses_includes(request: Request) -> bool:
    """Return whether the committed config is composed from multiple files via !include."""
    snapshot = _request_or_current_snapshot(request)
    return snapshot.source_files is not None and len(snapshot.source_files) > 1


def read_raw_config_source(request: Request) -> str:
    """Read the raw config source text for the current runtime."""
    snapshot = _request_or_current_snapshot(request)
    try:
        return snapshot.runtime_paths.config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Recovery still needs the raw source visible even when the on-disk file
        # contains unreadable bytes. Replacement characters keep the editor usable.
        return snapshot.runtime_paths.config_path.read_bytes().decode("utf-8", errors="replace")


def replace_raw_config_source(
    request: Request,
    source: str,
    *,
    error_prefix: str,
    expected_generation: int | None = None,
) -> int:
    """Replace the raw config source with one freshly validated payload."""
    return _build_and_commit_raw_replacement(
        request.app,
        source,
        error_prefix=error_prefix,
        initial_snapshot=request_snapshot(request),
        expected_generation=expected_generation,
    )
