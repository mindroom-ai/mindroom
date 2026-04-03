"""Helpers for API config loading, writing, and file-watcher lifecycle."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from mindroom import constants
from mindroom.config.main import Config, ConfigRuntimeValidationError, iter_config_validation_messages
from mindroom.config.main import load_config as load_runtime_config_model
from mindroom.file_watcher import watch_file
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from types import TracebackType

logger = get_logger(__name__)


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


class _WatchFile(Protocol):
    async def __call__(
        self,
        file_path: Path | str,
        callback: Callable[[], Awaitable[None]],
        stop_event: asyncio.Event | None = None,
    ) -> None: ...


class _ApiConfigContext(Protocol):
    """App-bound API config state required by the shared config access helpers."""

    runtime_paths: constants.RuntimePaths
    config_data: dict[str, Any]
    config_lock: ApiConfigLock
    config_load_result: ConfigLoadResult | None


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


def load_runtime_config(runtime_paths: constants.RuntimePaths) -> tuple[Config, constants.RuntimePaths]:
    """Load the current runtime config and raise HTTPException on user-facing failures."""
    try:
        return load_runtime_config_model(runtime_paths), runtime_paths
    except (ValidationError, ConfigRuntimeValidationError, yaml.YAMLError, OSError, UnicodeError) as exc:
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


def _app_config_context(api_app: FastAPI) -> _ApiConfigContext:
    """Return the app-bound API config state."""
    context = getattr(api_app.state, "api_context", None)
    if context is None:
        msg = "API context is not initialized"
        raise TypeError(msg)
    return cast("_ApiConfigContext", context)


def api_runtime_paths(request: Request) -> constants.RuntimePaths:
    """Return the API request's committed runtime paths."""
    return _app_config_context(request.app).runtime_paths


def run_config_write[T](
    runtime_paths: constants.RuntimePaths,
    config_data: dict[str, Any],
    config_lock: ApiConfigLock,
    mutate: Callable[[dict[str, Any]], T],
    *,
    error_prefix: str,
    config_load_result: ConfigLoadResult | None = None,
) -> T:
    """Validate, save, and swap config under lock."""
    with config_lock:
        try:
            raise_for_config_load_result(config_load_result)
            if not config_data:
                _raise_missing_loaded_config()
            candidate_config = deepcopy(config_data)
            result = mutate(candidate_config)
            validated_payload = _validated_config_payload(candidate_config, runtime_paths)
            _save_config_to_file(validated_payload, runtime_paths=runtime_paths)
        except HTTPException:
            raise
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
        except ConfigRuntimeValidationError as e:
            raise HTTPException(status_code=422, detail=e.errors()) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{error_prefix}: {e!s}") from e
        else:
            config_data.clear()
            config_data.update(validated_payload)
            return result


def replace_config_write(
    runtime_paths: constants.RuntimePaths,
    config_data: dict[str, Any],
    config_lock: ApiConfigLock,
    new_config: dict[str, Any],
    *,
    error_prefix: str,
) -> None:
    """Validate and replace the committed config without depending on cached state."""
    with config_lock:
        try:
            validated_payload = _validated_config_payload(new_config, runtime_paths)
            _save_config_to_file(validated_payload, runtime_paths=runtime_paths)
        except HTTPException:
            raise
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
        except ConfigRuntimeValidationError as e:
            raise HTTPException(status_code=422, detail=e.errors()) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{error_prefix}: {e!s}") from e
        else:
            config_data.clear()
            config_data.update(validated_payload)


def load_config_from_file(
    runtime_paths: constants.RuntimePaths,
    *,
    config_data: dict[str, Any],
    config_lock: ApiConfigLock,
) -> ConfigLoadResult:
    """Load config from the runtime config file into the shared cache."""
    try:
        validated_payload = load_runtime_config_model(runtime_paths).authored_model_dump()
        with config_lock:
            config_data.clear()
            config_data.update(validated_payload)
    except (ValidationError, ConfigRuntimeValidationError, yaml.YAMLError, OSError, UnicodeError) as exc:
        detail = _config_error_detail(exc)
        logger.warning(
            "Failed to load API config due to validation",
            config_path=str(runtime_paths.config_path),
            errors=detail,
        )
        return ConfigLoadResult(success=False, error_status_code=422, error_detail=detail)
    except Exception:
        logger.exception("Failed to load API config", config_path=str(runtime_paths.config_path))
        return ConfigLoadResult(success=False, error_status_code=500, error_detail="Failed to load configuration")
    else:
        logger.info("Loaded API config", config_path=str(runtime_paths.config_path))
        return ConfigLoadResult(success=True)


def load_config_into_app(runtime_paths: constants.RuntimePaths, api_app: FastAPI) -> bool:
    """Load config from disk into one API app's committed config cache."""
    context = _app_config_context(api_app)
    result = load_config_from_file(
        runtime_paths,
        config_data=context.config_data,
        config_lock=context.config_lock,
    )
    context.config_load_result = result
    return result.success


def read_app_committed_config[T](
    api_app: FastAPI,
    reader: Callable[[dict[str, Any]], T],
) -> T:
    """Read committed API config for one app only when the current file is valid."""
    context = _app_config_context(api_app)
    with context.config_lock:
        raise_for_config_load_result(context.config_load_result)
        if not context.config_data:
            _raise_missing_loaded_config()
        return reader(context.config_data)


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
    context = _app_config_context(api_app)
    result = run_config_write(
        context.runtime_paths,
        context.config_data,
        context.config_lock,
        mutate,
        error_prefix=error_prefix,
        config_load_result=context.config_load_result,
    )
    context.config_load_result = ConfigLoadResult(success=True)
    return result


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
    context = _app_config_context(api_app)
    replace_config_write(
        context.runtime_paths,
        context.config_data,
        context.config_lock,
        new_config,
        error_prefix=error_prefix,
    )
    context.config_load_result = ConfigLoadResult(success=True)


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
