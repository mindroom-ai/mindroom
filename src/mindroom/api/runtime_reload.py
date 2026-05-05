"""Runtime config reload helpers for API route modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI, HTTPException

from mindroom.api import config_lifecycle
from mindroom.api.config_lifecycle import ApiSnapshot, ConfigLoadResult
from mindroom.workers.runtime import clear_worker_validation_snapshot_cache

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom import constants
    from mindroom.config.main import Config

_UNSET = object()


def _published_snapshot(
    snapshot: ApiSnapshot,
    *,
    runtime_paths: constants.RuntimePaths | None = None,
    config_data: dict[str, Any] | None = None,
    runtime_config: Config | None | object = _UNSET,
    config_load_result: ConfigLoadResult | None | object = _UNSET,
    auth_state: object = _UNSET,
) -> ApiSnapshot:
    """Return one new published snapshot with an incremented generation."""
    updated_runtime_paths = snapshot.runtime_paths if runtime_paths is None else runtime_paths
    updated_config_data = snapshot.config_data if config_data is None else config_data
    updated_runtime_config = snapshot.runtime_config if runtime_config is _UNSET else runtime_config
    updated_config_load_result = (
        snapshot.config_load_result
        if config_load_result is _UNSET
        else cast("ConfigLoadResult | None", config_load_result)
    )
    updated_auth_state = snapshot.auth_state if auth_state is _UNSET else auth_state
    return ApiSnapshot(
        generation=snapshot.generation + 1,
        runtime_paths=updated_runtime_paths,
        config_data=updated_config_data,
        runtime_config=cast("Config | None", updated_runtime_config),
        config_load_result=updated_config_load_result,
        auth_state=updated_auth_state,
    )


def _reload_api_runtime_config(
    api_app: FastAPI,
    runtime_paths: constants.RuntimePaths,
    *,
    expected_snapshot: ApiSnapshot | None = None,
    mutate_runtime: Callable[[constants.RuntimePaths], constants.RuntimePaths] | None = None,
) -> None:
    """Rebind the API app to one runtime and surface structured config reload failures."""
    app_state = config_lifecycle.app_state(api_app)
    api_state = config_lifecycle.require_api_state(api_app)
    with api_state.config_lock:
        current_snapshot = api_state.snapshot
        if expected_snapshot is not None and (
            current_snapshot.generation != expected_snapshot.generation
            or current_snapshot.runtime_paths != expected_snapshot.runtime_paths
        ):
            raise HTTPException(
                status_code=409,
                detail="Configuration changed while request was in progress. Retry the operation.",
            )
        target_runtime_paths = runtime_paths if mutate_runtime is None else mutate_runtime(runtime_paths)
        app_state.api_auth_account_id = target_runtime_paths.env_value("ACCOUNT_ID")
        auth_state = current_snapshot.auth_state if current_snapshot.runtime_paths == target_runtime_paths else None
        config_data = current_snapshot.config_data if current_snapshot.runtime_paths == target_runtime_paths else {}
        runtime_config = (
            current_snapshot.runtime_config if current_snapshot.runtime_paths == target_runtime_paths else None
        )
        config_load_result = (
            current_snapshot.config_load_result if current_snapshot.runtime_paths == target_runtime_paths else None
        )
        refreshed_snapshot = _published_snapshot(
            current_snapshot,
            runtime_paths=target_runtime_paths,
            config_data=config_data,
            runtime_config=runtime_config,
            auth_state=auth_state,
            config_load_result=config_load_result,
        )
        api_state.snapshot = refreshed_snapshot
        result, validated_payload, loaded_runtime_config = config_lifecycle._load_config_result(target_runtime_paths)
        api_state.snapshot = _published_snapshot(
            refreshed_snapshot,
            config_data=validated_payload if validated_payload is not None else refreshed_snapshot.config_data,
            runtime_config=loaded_runtime_config
            if loaded_runtime_config is not None
            else refreshed_snapshot.runtime_config,
            config_load_result=result,
        )
    config_lifecycle.raise_for_config_load_result(result)
    clear_worker_validation_snapshot_cache()
