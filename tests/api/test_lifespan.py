"""Tests for API process lifecycle work."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI

from mindroom import constants
from mindroom.api import config_lifecycle, main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_api_lifespan_syncs_credentials_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Credential filesystem work should run outside the API event loop."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={},
    )
    api_app = FastAPI()
    main.initialize_api_app(api_app, runtime_paths)
    config_lifecycle.app_state(api_app).orchestrator_knowledge_refresh_scheduler = object()  # type: ignore[assignment]
    event_loop_thread = threading.get_ident()
    credential_sync_thread: int | None = None

    def _sync_credentials(runtime_paths: constants.RuntimePaths) -> None:
        nonlocal credential_sync_thread
        assert runtime_paths is main._app_runtime_paths(api_app)
        credential_sync_thread = threading.get_ident()

    async def _no_op_async(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(constants, "ensure_writable_config_path", lambda **_kwargs: None)
    monkeypatch.setattr(config_lifecycle, "load_config_into_app", lambda *_args: True)
    monkeypatch.setattr(main, "sync_env_to_credentials", _sync_credentials)
    monkeypatch.setattr(main, "_sync_standalone_knowledge_watchers", _no_op_async)
    monkeypatch.setattr(main, "_watch_config", _no_op_async)
    monkeypatch.setattr(main, "_worker_cleanup_loop", _no_op_async)

    async with main._lifespan(api_app):
        pass

    assert credential_sync_thread is not None
    assert credential_sync_thread != event_loop_thread
