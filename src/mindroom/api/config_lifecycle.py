"""Helpers for API config loading, writing, and file-watcher lifecycle."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, cast

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from mindroom import constants
from mindroom.config.main import (
    CONFIG_LOAD_USER_ERROR_TYPES,
    Config,
    ConfigRuntimeValidationError,
    iter_config_validation_messages,
)
from mindroom.config.main import load_config as load_runtime_config_model
from mindroom.file_watcher import watch_file
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from types import TracebackType

logger = get_logger(__name__)
_UNSET = object()


@dataclass(frozen=True)
class ConfigLoadResult:
    """Outcome of one API config-file load attempt."""

    success: bool
    error_status_code: int | None = None
    error_detail: object | None = None


class ApiConfigLock(Protocol):
    """Lock protocol used to guard API config cache updates."""

    def __enter__(self) -> object:
        """Acquire the lock."""
        ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Release the lock."""
        ...


@dataclass
class ApiSnapshot:
    """One published API runtime snapshot."""

    generation: int
    runtime_paths: constants.RuntimePaths
    config_data: dict[str, Any]
    config_load_result: ConfigLoadResult | None = None
    auth_state: Any | None = None


@dataclass
class ApiState:
    """Stable holder for the current API runtime snapshot."""

    config_lock: ApiConfigLock
    snapshot: ApiSnapshot


class _WatchFile(Protocol):
    async def __call__(
        self,
        file_path: Path | str,
        callback: Callable[[], Awaitable[None]],
        stop_event: asyncio.Event | None = None,
    ) -> None: ...


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
) -> tuple[ConfigLoadResult, dict[str, Any] | None]:
    """Load and validate one config file without mutating shared app state."""
    try:
        validated_payload = load_runtime_config_model(runtime_paths).authored_model_dump()
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        detail = _config_error_detail(exc)
        logger.warning(
            "Failed to load API config due to validation",
            config_path=str(runtime_paths.config_path),
            errors=detail,
        )
        return ConfigLoadResult(success=False, error_status_code=422, error_detail=detail), None
    except Exception:
        logger.exception("Failed to load API config", config_path=str(runtime_paths.config_path))
        return (
            ConfigLoadResult(success=False, error_status_code=500, error_detail="Failed to load configuration"),
            None,
        )
    else:
        logger.info("Loaded API config", config_path=str(runtime_paths.config_path))
        return ConfigLoadResult(success=True), validated_payload


def load_runtime_config(runtime_paths: constants.RuntimePaths) -> tuple[Config, constants.RuntimePaths]:
    """Load the current runtime config and raise HTTPException on user-facing failures."""
    try:
        return load_runtime_config_model(runtime_paths), runtime_paths
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


def _validated_config_payload(
    raw_config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> dict[str, Any]:
    """Normalize and validate one config payload against the active runtime."""
    validated_config = Config.validate_with_runtime(raw_config, runtime_paths)
    return validated_config.authored_model_dump()


def _app_config_state(api_app: FastAPI) -> ApiState:
    """Return the app-bound API config state."""
    state = getattr(api_app.state, "api_state", None)
    if state is None:
        msg = "API context is not initialized"
        raise TypeError(msg)
    return cast("ApiState", state)


def _published_snapshot(
    snapshot: ApiSnapshot,
    *,
    config_data: dict[str, Any] | None = None,
    config_load_result: ConfigLoadResult | None | object = _UNSET,
) -> ApiSnapshot:
    """Return one new published snapshot with an incremented generation."""
    updated_config_data = snapshot.config_data if config_data is None else config_data
    updated_load_result = (
        snapshot.config_load_result
        if config_load_result is _UNSET
        else cast("ConfigLoadResult | None", config_load_result)
    )
    return replace(
        snapshot,
        generation=snapshot.generation + 1,
        config_data=updated_config_data,
        config_load_result=updated_load_result,
    )


def _stale_snapshot_error() -> HTTPException:
    """Return the shared stale-write error used when state changed mid-request."""
    return HTTPException(
        status_code=409,
        detail="Configuration changed while request was in progress. Retry the operation.",
    )


def api_runtime_paths(request: Request) -> constants.RuntimePaths:
    """Return the API request's committed runtime paths."""
    return _app_config_state(request.app).snapshot.runtime_paths


def _build_mutated_config[T](
    snapshot: ApiSnapshot,
    mutate: Callable[[dict[str, Any]], T],
    runtime_paths: constants.RuntimePaths,
) -> tuple[T, dict[str, Any]]:
    """Build one validated config payload from a committed snapshot off-lock."""
    raise_for_config_load_result(snapshot.config_load_result)
    if not snapshot.config_data:
        _raise_missing_loaded_config()
    candidate_config = deepcopy(snapshot.config_data)
    result = mutate(candidate_config)
    validated_payload = _validated_config_payload(candidate_config, runtime_paths)
    return result, validated_payload


def _commit_mutated_snapshot[T](
    api_app: FastAPI,
    initial_state: ApiState,
    *,
    expected_generation: int,
    runtime_paths: constants.RuntimePaths,
    validated_payload: dict[str, Any],
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
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload,
            config_load_result=ConfigLoadResult(success=True),
        )
        return result


def _validate_replacement_payload(
    new_config: dict[str, Any],
    runtime_paths: constants.RuntimePaths,
) -> dict[str, Any]:
    """Validate one replacement config payload off-lock."""
    return _validated_config_payload(new_config, runtime_paths)


def _commit_replaced_snapshot(
    api_app: FastAPI,
    initial_state: ApiState,
    *,
    expected_generation: int,
    runtime_paths: constants.RuntimePaths,
    validated_payload: dict[str, Any],
) -> None:
    """Commit one previously validated replacement payload if the snapshot is still current."""
    with initial_state.config_lock:
        current_state = _app_config_state(api_app)
        current = current_state.snapshot
        if current.generation != expected_generation or current.runtime_paths != runtime_paths:
            raise _stale_snapshot_error()
        _save_config_to_file(validated_payload, runtime_paths=runtime_paths)
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload,
            config_load_result=ConfigLoadResult(success=True),
        )


def _build_and_commit_mutation[T](
    api_app: FastAPI,
    mutate: Callable[[dict[str, Any]], T],
    *,
    error_prefix: str,
) -> T:
    """Build one config mutation off-lock and commit it only if still current."""
    initial_state = _app_config_state(api_app)
    with initial_state.config_lock:
        snapshot = _app_config_state(api_app).snapshot
    try:
        result, validated_payload = _build_mutated_config(snapshot, mutate, snapshot.runtime_paths)
        return _commit_mutated_snapshot(
            api_app,
            initial_state,
            expected_generation=snapshot.generation,
            runtime_paths=snapshot.runtime_paths,
            validated_payload=validated_payload,
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
) -> None:
    """Build one replacement payload off-lock and commit it only if still current."""
    initial_state = _app_config_state(api_app)
    with initial_state.config_lock:
        snapshot = _app_config_state(api_app).snapshot
    try:
        validated_payload = _validate_replacement_payload(new_config, snapshot.runtime_paths)
        _commit_replaced_snapshot(
            api_app,
            initial_state,
            expected_generation=snapshot.generation,
            runtime_paths=snapshot.runtime_paths,
            validated_payload=validated_payload,
        )
    except HTTPException:
        raise
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
    except ConfigRuntimeValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{error_prefix}: {e!s}") from e


def load_config_from_file(
    runtime_paths: constants.RuntimePaths,
    *,
    config_data: dict[str, Any],
    config_lock: ApiConfigLock,
) -> ConfigLoadResult:
    """Load config from the runtime config file into the shared cache."""
    result, validated_payload = _load_config_result(runtime_paths)
    if validated_payload is not None:
        with config_lock:
            config_data.clear()
            config_data.update(validated_payload)
    return result


def load_config_into_app(runtime_paths: constants.RuntimePaths, api_app: FastAPI) -> bool:
    """Load config from disk into one API app's committed config cache."""
    initial_state = _app_config_state(api_app)
    snapshot = initial_state.snapshot
    result, validated_payload = _load_config_result(runtime_paths)
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
        current_state.snapshot = _published_snapshot(
            current,
            config_data=validated_payload if validated_payload is not None else current.config_data,
            config_load_result=result,
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


def read_committed_config[T](
    request: Request,
    reader: Callable[[dict[str, Any]], T],
) -> T:
    """Read committed API config only when the current on-disk config is valid."""
    return read_app_committed_config(request.app, reader)


def write_committed_config[T](
    request: Request,
    mutate: Callable[[dict[str, Any]], T],
    *,
    error_prefix: str,
) -> T:
    """Mutate committed API config from the last valid cache snapshot."""
    return write_app_committed_config(request.app, mutate, error_prefix=error_prefix)


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
) -> None:
    """Replace the entire committed API config with one freshly validated payload."""
    replace_app_committed_config(request.app, new_config, error_prefix=error_prefix)


def replace_app_committed_config(
    api_app: FastAPI,
    new_config: dict[str, Any],
    *,
    error_prefix: str,
) -> None:
    """Replace the entire committed API config with one freshly validated payload."""
    _build_and_commit_replacement(api_app, new_config, error_prefix=error_prefix)


async def watch_config(
    stop_event: asyncio.Event,
    runtime_paths: constants.RuntimePaths,
    on_config_change: Callable[[], bool],
    *,
    watch_file_impl: _WatchFile = watch_file,
) -> None:
    """Watch the runtime config file and reload the in-memory cache when it changes."""

    async def _handle_config_change() -> None:
        logger.info("Config file changed", path=str(runtime_paths.config_path))
        on_config_change()

    await watch_file_impl(runtime_paths.config_path, _handle_config_change, stop_event=stop_event)
