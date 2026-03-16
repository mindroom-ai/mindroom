"""Helpers for API config loading, writing, and file-watcher lifecycle."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Protocol

import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from mindroom import constants
from mindroom.config.main import Config
from mindroom.config.main import load_config as load_runtime_config_model
from mindroom.file_watcher import watch_file
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable
    from pathlib import Path
    from types import TracebackType

logger = get_logger(__name__)


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


def load_runtime_config(runtime_paths: constants.RuntimePaths) -> tuple[Config, Path]:
    """Load the current runtime config and return it with its path."""
    return load_runtime_config_model(runtime_paths), runtime_paths.config_path


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
    return validated_config.model_dump(exclude_none=True)


def run_config_write[T](
    runtime_paths: constants.RuntimePaths,
    config_data: dict[str, Any],
    config_lock: ApiConfigLock,
    mutate: Callable[[dict[str, Any]], T],
    *,
    error_prefix: str,
) -> T:
    """Validate, save, and swap config under lock."""
    with config_lock:
        try:
            candidate_config = deepcopy(config_data)
            result = mutate(candidate_config)
            validated_payload = _validated_config_payload(candidate_config, runtime_paths)
            _save_config_to_file(validated_payload, runtime_paths=runtime_paths)
        except HTTPException:
            raise
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=e.errors(include_context=False)) from e
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"{error_prefix}: {e!s}") from e
        else:
            config_data.clear()
            config_data.update(validated_payload)
            return result


def load_config_from_file(
    runtime_paths: constants.RuntimePaths,
    *,
    config_data: dict[str, Any],
    config_lock: ApiConfigLock,
) -> bool:
    """Load config from the runtime config file into the shared cache."""
    try:
        validated_payload = load_runtime_config_model(runtime_paths).model_dump(exclude_none=True)
        with config_lock:
            config_data.clear()
            config_data.update(validated_payload)
    except Exception:
        logger.exception("Failed to load API config", config_path=str(runtime_paths.config_path))
        return False
    else:
        logger.info("Loaded API config", config_path=str(runtime_paths.config_path))
        return True


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
