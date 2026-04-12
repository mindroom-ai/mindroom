"""Helpers for API config loading, writing, and file-watcher lifecycle."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import weakref
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from mindroom import constants
from mindroom.connections import canonical_connection_provider
from mindroom.config.main import (
    CONFIG_LOAD_USER_ERROR_TYPES,
    Config,
    ConfigRuntimeValidationError,
    iter_config_validation_messages,
)
from mindroom.config.main import load_config as load_runtime_config_model
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.file_watcher import watch_file
from mindroom.logging_config import get_logger

logger = get_logger(__name__)
_UNSET = object()
_REQUEST_SNAPSHOT_SCOPE_KEY = "api_snapshot"
CONFIG_GENERATION_HEADER = "x-mindroom-config-generation"
_REGISTERED_API_APPS: weakref.WeakSet[FastAPI] = weakref.WeakSet()
_REGISTERED_API_APPS_LOCK = threading.Lock()
_BACKEND_MANAGED_GOOGLE_SERVICES = frozenset({"google", "google_oauth_client"})

type WatchFileFn = Callable[
    [Path | str, Callable[[], Awaitable[None]], asyncio.Event | None],
    Awaitable[None],
]


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
    runtime_config: Config | None = None
    config_load_result: ConfigLoadResult | None = None
    auth_state: Any | None = None
    backend_managed_services: frozenset[str] = frozenset()


@dataclass
class ApiState:
    """Stable holder for the current API runtime snapshot."""

    config_lock: threading.Lock
    snapshot: ApiSnapshot


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


def _load_config_result(
    runtime_paths: constants.RuntimePaths,
) -> tuple[ConfigLoadResult, dict[str, Any] | None, Config | None]:
    """Load and validate one config file without mutating shared app state."""
    try:
        runtime_config = load_runtime_config_model(
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
        return ConfigLoadResult(success=False, error_status_code=422, error_detail=detail), None, None
    except Exception:
        logger.exception("Failed to load API config", config_path=str(runtime_paths.config_path))
        return (
            ConfigLoadResult(success=False, error_status_code=500, error_detail="Failed to load configuration"),
            None,
            None,
        )
    else:
        logger.info("Loaded API config", config_path=str(runtime_paths.config_path))
        return ConfigLoadResult(success=True), validated_payload, runtime_config


def load_runtime_config(runtime_paths: constants.RuntimePaths) -> tuple[Config, constants.RuntimePaths]:
    """Load the current runtime config and raise HTTPException on user-facing failures."""
    try:
        return (
            load_runtime_config_model(
                runtime_paths,
                tolerate_plugin_load_errors=True,
            ),
            runtime_paths,
        )
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        raise HTTPException(status_code=422, detail=_config_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to load configuration: {exc!s}") from exc


def raise_for_config_load_result(result: ConfigLoadResult | None) -> None:
    """Raise HTTPException when the cached config state reflects a failed load."""
    if result is None or result.success:
        return
    raise HTTPException(
        status_code=result.error_status_code or 500,
        detail=result.error_detail or "Failed to load configuration",
    )


def _raise_missing_loaded_config() -> None:
    """Raise the shared missing-config HTTP error used by cached API reads and writes."""
    raise HTTPException(status_code=500, detail="Failed to load configuration")


def _save_config_to_file(
    config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Save config to YAML file with deterministic ordering."""
    config_path = runtime_paths.config_path
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        yaml.dump(
            config,
            f,
            default_flow_style=False,
            sort_keys=True,
            allow_unicode=True,
        )
    constants.safe_replace(tmp_path, config_path)


def _save_raw_config_source_to_file(
    source: str,
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Save raw config source text to the active config path."""
    config_path = runtime_paths.config_path
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp_path.write_text(source, encoding="utf-8")
    constants.safe_replace(tmp_path, config_path)


def persist_runtime_validated_config(
    runtime_config: Config,
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Persist one validated config and immediately publish matching committed API snapshots."""
    validated_payload = runtime_config.authored_model_dump()
    matching_states = [state for state in _registered_api_states() if state.snapshot.runtime_paths == runtime_paths]
    backend_managed_services = _backend_managed_services_for_config(runtime_config)
    if not matching_states:
        previous_config = _load_existing_runtime_config_if_available(runtime_paths)
        _save_config_to_file(validated_payload, runtime_paths=runtime_paths)
        _cleanup_removed_google_oauth_client_services(previous_config, runtime_config, runtime_paths)
        return

    with ExitStack() as stack:
        locked_snapshots: list[tuple[ApiState, ApiSnapshot]] = []
        for state in sorted(matching_states, key=id):
            stack.enter_context(state.config_lock)
            snapshot = state.snapshot
            if snapshot.runtime_paths != runtime_paths:
                continue
            locked_snapshots.append((state, snapshot))

        _save_config_to_file(validated_payload, runtime_paths=runtime_paths)
        for state, snapshot in locked_snapshots:
            _cleanup_removed_google_oauth_client_services(snapshot.runtime_config, runtime_config, runtime_paths)
            state.snapshot = _published_snapshot(
                snapshot,
                config_data=deepcopy(validated_payload),
                runtime_config=runtime_config,
                config_load_result=ConfigLoadResult(success=True),
                backend_managed_services=backend_managed_services,
            )


def _validated_config_payload(
    raw_config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> tuple[Config, dict[str, Any]]:
    """Normalize and validate one config payload against the active runtime."""
    validated_config = Config.validate_with_runtime(
        raw_config,
        runtime_paths,
        tolerate_plugin_load_errors=True,
        strict_connection_validation=True,
    )
    return validated_config, validated_config.authored_model_dump()


def _load_existing_runtime_config_if_available(runtime_paths: constants.RuntimePaths) -> Config | None:
    """Return the current on-disk config when it can be loaded before an overwrite."""
    try:
        return load_runtime_config_model(
            runtime_paths,
            tolerate_plugin_load_errors=True,
        )
    except Exception:
        return None


def _google_oauth_client_services(config: Config | None) -> set[str]:
    """Return configured Google OAuth client backing services from one config snapshot."""
    if config is None:
        return set()
    return {
        connection.service
        for connection in config.connections.values()
        if connection.service is not None
        and connection.auth_kind == "oauth_client"
        and canonical_connection_provider(connection.provider) == "google"
    }


def _backend_managed_services_for_config(config: Config | None) -> frozenset[str]:
    """Return the cached generic-API denylist for one validated config snapshot."""
    if config is None:
        return frozenset()
    return _BACKEND_MANAGED_GOOGLE_SERVICES | frozenset(_google_oauth_client_services(config))


def _cleanup_removed_google_oauth_client_services(
    previous_config: Config | None,
    next_config: Config,
    runtime_paths: constants.RuntimePaths,
) -> None:
    """Delete shared Google OAuth client credentials for services removed from config."""
    removed_services = _google_oauth_client_services(previous_config) - _google_oauth_client_services(next_config)
    if not removed_services:
        return
    shared_manager = get_runtime_credentials_manager(runtime_paths).shared_manager()
    for service in sorted(removed_services):
        shared_manager.delete_credentials(service)


def _app_config_state(api_app: FastAPI) -> ApiState:
    """Return the app-bound API config state."""
    try:
        state = api_app.state.api_state
    except AttributeError:
        state = None
    if not isinstance(state, ApiState):
        msg = "API context is not initialized"
        raise TypeError(msg)
    return state


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
            states.append(_app_config_state(api_app))
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
    app_state = _app_config_state(request.app)
    with app_state.config_lock:
        return store_request_snapshot(request, app_state.snapshot)


def _request_or_current_snapshot(request: Request) -> ApiSnapshot:
    """Return the request-bound snapshot when present, else the current app snapshot."""
    bound_snapshot = request_snapshot(request)
    if bound_snapshot is not None:
        return bound_snapshot
    return _app_config_state(request.app).snapshot


def _published_snapshot(
    snapshot: ApiSnapshot,
    *,
    config_data: dict[str, Any] | None = None,
    runtime_config: Config | None | object = _UNSET,
    config_load_result: ConfigLoadResult | None | object = _UNSET,
    backend_managed_services: frozenset[str] | object = _UNSET,
) -> ApiSnapshot:
    """Return one new published snapshot with an incremented generation."""
    updated_config_data = snapshot.config_data if config_data is None else config_data
    updated_runtime_config = (
        snapshot.runtime_config if runtime_config is _UNSET else cast("Config | None", runtime_config)
    )
    updated_load_result = (
        snapshot.config_load_result
        if config_load_result is _UNSET
        else cast("ConfigLoadResult | None", config_load_result)
    )
    updated_backend_managed_services = (
        snapshot.backend_managed_services
        if backend_managed_services is _UNSET
        else cast("frozenset[str]", backend_managed_services)
    )
    return replace(
        snapshot,
        generation=snapshot.generation + 1,
        config_data=updated_config_data,
        runtime_config=updated_runtime_config,
        config_load_result=updated_load_result,
        backend_managed_services=updated_backend_managed_services,
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
) -> tuple[T, dict[str, Any], Config]:
    """Build one validated config payload from a committed snapshot off-lock."""
    raise_for_config_load_result(snapshot.config_load_result)
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
    validated_config: Config,
    result: T,
) -> T:
    """Commit one previously validated mutation if the targeted snapshot is still current."""
    with initial_state.config_lock:
        current_state = _app_config_state(api_app)
        current = current_state.snapshot
        if current.generation != expected_generation or current.runtime_paths != runtime_paths:
            raise_for_config_load_result(current.config_load_result)
            raise _stale_snapshot_error()
        _save_config_to_file(validated_payload, runtime_paths=runtime_paths)
        _cleanup_removed_google_oauth_client_services(current.runtime_config, validated_config, runtime_paths)
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload,
            runtime_config=validated_config,
            config_load_result=ConfigLoadResult(success=True),
            backend_managed_services=_backend_managed_services_for_config(validated_config),
        )
        return result


def _validate_replacement_payload(
    new_config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> tuple[Config, dict[str, Any]]:
    """Validate one replacement config payload off-lock."""
    return _validated_config_payload(new_config, runtime_paths)


def _validate_raw_config_source(
    source: str,
    runtime_paths: constants.RuntimePaths,
) -> tuple[Config, dict[str, Any]]:
    """Validate raw YAML source against the current runtime without mutating the live file."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=runtime_paths.config_path.parent,
        prefix=f"{runtime_paths.config_path.name}.validation.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(source)
        validation_path = Path(tmp.name)
    validation_runtime_paths = replace(runtime_paths, config_path=validation_path)
    try:
        runtime_config = load_runtime_config_model(
            validation_runtime_paths,
            tolerate_plugin_load_errors=True,
        )
        return runtime_config, runtime_config.authored_model_dump()
    finally:
        validation_path.unlink(missing_ok=True)


def _commit_replaced_snapshot(
    api_app: FastAPI,
    initial_state: ApiState,
    *,
    expected_generation: int,
    runtime_paths: constants.RuntimePaths,
    validated_payload: dict[str, Any],
    validated_config: Config,
) -> int:
    """Commit one previously validated replacement payload if the snapshot is still current."""
    with initial_state.config_lock:
        current_state = _app_config_state(api_app)
        current = current_state.snapshot
        if current.generation != expected_generation or current.runtime_paths != runtime_paths:
            raise _stale_snapshot_error()
        _save_config_to_file(validated_payload, runtime_paths=runtime_paths)
        _cleanup_removed_google_oauth_client_services(current.runtime_config, validated_config, runtime_paths)
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload,
            runtime_config=validated_config,
            config_load_result=ConfigLoadResult(success=True),
            backend_managed_services=_backend_managed_services_for_config(validated_config),
        )
        return current_state.snapshot.generation


def _commit_raw_replaced_snapshot(
    api_app: FastAPI,
    initial_state: ApiState,
    *,
    expected_generation: int,
    runtime_paths: constants.RuntimePaths,
    validated_payload: dict[str, Any],
    validated_config: Config,
    source: str,
) -> int:
    """Commit one raw replacement payload if the targeted snapshot is still current."""
    with initial_state.config_lock:
        current_state = _app_config_state(api_app)
        current = current_state.snapshot
        if current.generation != expected_generation or current.runtime_paths != runtime_paths:
            raise _stale_snapshot_error()
        _save_raw_config_source_to_file(source, runtime_paths=runtime_paths)
        _cleanup_removed_google_oauth_client_services(current.runtime_config, validated_config, runtime_paths)
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload,
            runtime_config=validated_config,
            config_load_result=ConfigLoadResult(success=True),
            backend_managed_services=_backend_managed_services_for_config(validated_config),
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
    initial_state = _app_config_state(api_app)
    if initial_snapshot is None:
        with initial_state.config_lock:
            snapshot = _app_config_state(api_app).snapshot
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
    except ConfigRuntimeValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e
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
    initial_state = _app_config_state(api_app)
    if initial_snapshot is None:
        with initial_state.config_lock:
            snapshot = _app_config_state(api_app).snapshot
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
    except ConfigRuntimeValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e
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
    initial_state = _app_config_state(api_app)
    if initial_snapshot is None:
        with initial_state.config_lock:
            snapshot = _app_config_state(api_app).snapshot
    else:
        snapshot = initial_snapshot
    try:
        _raise_if_generation_mismatch(snapshot, expected_generation)
        validated_config, validated_payload = _validate_raw_config_source(source, snapshot.runtime_paths)
        return _commit_raw_replaced_snapshot(
            api_app,
            initial_state,
            expected_generation=snapshot.generation,
            runtime_paths=snapshot.runtime_paths,
            validated_payload=validated_payload,
            validated_config=validated_config,
            source=source,
        )
    except HTTPException:
        raise
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        raise HTTPException(status_code=422, detail=_config_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{error_prefix}: {exc!s}") from exc


def load_config_from_file(
    runtime_paths: constants.RuntimePaths,
    *,
    config_data: dict[str, Any],
    config_lock: threading.Lock,
) -> ConfigLoadResult:
    """Load config from the runtime config file into the shared cache."""
    result, validated_payload, _runtime_config = _load_config_result(runtime_paths)
    if validated_payload is not None:
        with config_lock:
            config_data.clear()
            config_data.update(validated_payload)
    return result


def load_config_into_app(runtime_paths: constants.RuntimePaths, api_app: FastAPI) -> bool:
    """Load config from disk into one API app's committed config cache."""
    initial_state = _app_config_state(api_app)
    snapshot = initial_state.snapshot
    result, validated_payload, runtime_config = _load_config_result(runtime_paths)
    with initial_state.config_lock:
        current_state = _app_config_state(api_app)
        current = current_state.snapshot
        if current.generation != snapshot.generation or current.runtime_paths != runtime_paths:
            logger.info(
                "Discarding stale API config load after runtime swap",
                load_config_path=str(runtime_paths.config_path),
                active_config_path=str(current.runtime_paths.config_path),
            )
            return False
        if runtime_config is not None:
            _cleanup_removed_google_oauth_client_services(current.runtime_config, runtime_config, runtime_paths)
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload if validated_payload is not None else current.config_data,
            runtime_config=runtime_config if runtime_config is not None else current.runtime_config,
            config_load_result=result,
            backend_managed_services=(
                _backend_managed_services_for_config(runtime_config)
                if runtime_config is not None
                else current.backend_managed_services
            ),
        )
    return result.success


def read_app_committed_config[T](
    api_app: FastAPI,
    reader: Callable[[dict[str, Any]], T],
) -> T:
    """Read committed API config for one app only when the current file is valid."""
    initial_state = _app_config_state(api_app)
    with initial_state.config_lock:
        snapshot = _app_config_state(api_app).snapshot
        raise_for_config_load_result(snapshot.config_load_result)
        if not snapshot.config_data:
            _raise_missing_loaded_config()
        return reader(snapshot.config_data)


def read_app_committed_config_and_runtime[T](
    api_app: FastAPI,
    reader: Callable[[dict[str, Any]], T],
) -> tuple[T, constants.RuntimePaths]:
    """Read committed API config and runtime from one coherent published snapshot."""
    initial_state = _app_config_state(api_app)
    with initial_state.config_lock:
        snapshot = _app_config_state(api_app).snapshot
        raise_for_config_load_result(snapshot.config_load_result)
        if not snapshot.config_data:
            _raise_missing_loaded_config()
        return reader(snapshot.config_data), snapshot.runtime_paths


def read_app_committed_runtime_config(
    api_app: FastAPI,
) -> tuple[Config, constants.RuntimePaths]:
    """Read one validated runtime config and runtime from the same published snapshot."""
    initial_state = _app_config_state(api_app)
    with initial_state.config_lock:
        snapshot = _app_config_state(api_app).snapshot
        raise_for_config_load_result(snapshot.config_load_result)
        if not snapshot.config_data:
            _raise_missing_loaded_config()
        runtime_paths = snapshot.runtime_paths
        if snapshot.runtime_config is not None:
            return snapshot.runtime_config, runtime_paths
        return Config.model_validate(snapshot.config_data, context={"runtime_paths": runtime_paths}), runtime_paths


def read_committed_config[T](
    request: Request,
    reader: Callable[[dict[str, Any]], T],
) -> T:
    """Read committed API config only when the current on-disk config is valid."""
    snapshot = _request_or_current_snapshot(request)
    raise_for_config_load_result(snapshot.config_load_result)
    if not snapshot.config_data:
        _raise_missing_loaded_config()
    return reader(snapshot.config_data)


def read_committed_config_and_runtime[T](
    request: Request,
    reader: Callable[[dict[str, Any]], T],
) -> tuple[T, constants.RuntimePaths]:
    """Read committed API config and runtime from one coherent request snapshot."""
    snapshot = _request_or_current_snapshot(request)
    raise_for_config_load_result(snapshot.config_load_result)
    if not snapshot.config_data:
        _raise_missing_loaded_config()
    return reader(snapshot.config_data), snapshot.runtime_paths


def read_committed_runtime_config(
    request: Request,
) -> tuple[Config, constants.RuntimePaths]:
    """Read one validated runtime config and runtime from one coherent request snapshot."""
    snapshot = _request_or_current_snapshot(request)
    raise_for_config_load_result(snapshot.config_load_result)
    if not snapshot.config_data:
        _raise_missing_loaded_config()
    runtime_paths = snapshot.runtime_paths
    if snapshot.runtime_config is not None:
        return snapshot.runtime_config, runtime_paths
    return Config.model_validate(snapshot.config_data, context={"runtime_paths": runtime_paths}), runtime_paths


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


def write_app_committed_config[T](
    api_app: FastAPI,
    mutate: Callable[[dict[str, Any]], T],
    *,
    error_prefix: str,
) -> T:
    """Mutate committed API config from the last valid cache snapshot."""
    return _build_and_commit_mutation(api_app, mutate, error_prefix=error_prefix)


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


def replace_app_committed_config(
    api_app: FastAPI,
    new_config: dict[str, Any],
    *,
    error_prefix: str,
) -> int:
    """Replace the entire committed API config with one freshly validated payload."""
    return _build_and_commit_replacement(api_app, new_config, error_prefix=error_prefix)


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


async def watch_config(
    stop_event: asyncio.Event,
    runtime_paths: constants.RuntimePaths,
    on_config_change: Callable[[], bool],
    *,
    watch_file_impl: WatchFileFn = watch_file,
) -> None:
    """Watch the runtime config file and reload the in-memory cache when it changes."""

    async def _handle_config_change() -> None:
        logger.info("Config file changed", path=str(runtime_paths.config_path))
        on_config_change()

    await watch_file_impl(runtime_paths.config_path, _handle_config_change, stop_event)
