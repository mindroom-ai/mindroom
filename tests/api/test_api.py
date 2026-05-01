"""Tests for the dashboard backend API endpoints."""

import asyncio
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Annotated, Any, NoReturn, cast
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from mindroom import constants, frontend_assets
from mindroom.api import auth, config_lifecycle, frontend, main, runtime_reload
from mindroom.api import tools as tools_api
from mindroom.api import workers as workers_api
from mindroom.commands.config_commands import apply_config_change
from mindroom.config.main import Config
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.matrix.health import mark_matrix_sync_loop_started, mark_matrix_sync_success, reset_matrix_sync_health
from mindroom.runtime_state import reset_runtime_state, set_runtime_ready, set_runtime_starting
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key
from mindroom.workers.models import WorkerHandle

TEST_WORKER_AUTH = "token"


def _runtime_paths(tmp_path: Path, *, process_env: dict[str, str] | None = None) -> constants.RuntimePaths:
    return constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env or {},
    )


def _config_with_worker_scope(
    worker_scope: str | None,
    *,
    worker_grantable_credentials: list[str] | None = None,
) -> Config:
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
            "agents": {
                "general": {
                    "display_name": "General",
                    "role": "test",
                    "tools": ["homeassistant"],
                    "instructions": ["hi"],
                    "rooms": ["lobby"],
                },
            },
            "defaults": {
                "markdown": True,
                "worker_grantable_credentials": worker_grantable_credentials,
            },
        },
    )
    config.agents["general"].worker_scope = worker_scope
    return config


def _authored_config_payload(agent_name: str) -> dict[str, Any]:
    return {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {
            agent_name: {
                "display_name": agent_name.title(),
                "role": "valid",
                "rooms": [],
            },
        },
    }


def _validated_authored_payload(
    runtime_paths: constants.RuntimePaths,
    agent_name: str,
) -> dict[str, Any]:
    return Config.validate_with_runtime(
        _authored_config_payload(agent_name),
        runtime_paths,
    ).authored_model_dump()


def _publish_committed_runtime_config(
    api_app: FastAPI,
    runtime_paths: constants.RuntimePaths,
    authored_payload: dict[str, Any],
) -> None:
    """Publish one committed config snapshot for request-bound API tests."""
    main.initialize_api_app(api_app, runtime_paths)
    context = main._app_context(api_app)
    runtime_config = Config.validate_with_runtime(authored_payload, runtime_paths)
    context.config_data = runtime_config.authored_model_dump()
    context.runtime_config = runtime_config
    context.config_load_result = main.ConfigLoadResult(success=True)
    context.auth_state = None


def test_api_main_does_not_reexport_config_lifecycle_pass_through_helpers() -> None:
    """Config lifecycle helpers should live in the lifecycle module only."""
    for helper_name in (
        "_app_config_data",
        "_app_config_lock",
        "_run_config_write",
        "_run_request_config_write",
        "_read_committed_config",
        "_read_request_committed_config",
        "_load_config_from_file",
    ):
        assert not hasattr(main, helper_name)


class _ContextSwapLock:
    def __init__(self, on_enter: Callable[[], None] | None = None) -> None:
        self.on_enter = on_enter

    def __enter__(self) -> object:
        if self.on_enter is not None:
            on_enter = self.on_enter
            self.on_enter = None
            on_enter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


def test_init_supabase_auth_returns_none_without_credentials(tmp_path: Path) -> None:
    """Supabase auth should stay disabled when credentials are incomplete."""
    runtime_paths = _runtime_paths(tmp_path)
    assert auth._init_supabase_auth(runtime_paths, None, None) is None
    assert auth._init_supabase_auth(runtime_paths, "https://supabase.test", None) is None
    assert auth._init_supabase_auth(runtime_paths, None, "anon-key") is None


def test_init_supabase_auth_raises_when_auto_install_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Missing supabase dependency should error with disable hint when auto-install is off."""
    install_calls: list[str] = []
    runtime_paths = _runtime_paths(tmp_path)

    def _missing_supabase(_name: str) -> NoReturn:
        module_name = "supabase"
        raise ModuleNotFoundError(module_name)

    def _auto_install(extra_name: str, _runtime_paths: constants.RuntimePaths) -> bool:
        install_calls.append(extra_name)
        return False

    monkeypatch.setattr(auth.importlib, "import_module", _missing_supabase)
    monkeypatch.setattr(auth, "auto_install_enabled", lambda _runtime_paths: False)
    monkeypatch.setattr(auth, "auto_install_tool_extra", _auto_install)

    with pytest.raises(ImportError, match="MINDROOM_NO_AUTO_INSTALL_TOOLS"):
        auth._init_supabase_auth(runtime_paths, "https://supabase.test", "anon-key")

    assert install_calls == ["supabase"]


def test_init_supabase_auth_raises_when_auto_install_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Missing dependency should error with install hint when auto-install attempt fails."""
    install_calls: list[str] = []
    runtime_paths = _runtime_paths(tmp_path)

    def _missing_supabase(_name: str) -> NoReturn:
        module_name = "supabase"
        raise ModuleNotFoundError(module_name)

    def _auto_install(extra_name: str, _runtime_paths: constants.RuntimePaths) -> bool:
        install_calls.append(extra_name)
        return False

    monkeypatch.setattr(auth.importlib, "import_module", _missing_supabase)
    monkeypatch.setattr(auth, "auto_install_enabled", lambda _runtime_paths: True)
    monkeypatch.setattr(auth, "auto_install_tool_extra", _auto_install)

    with pytest.raises(ImportError, match=r"mindroom\[supabase\]") as err:
        auth._init_supabase_auth(runtime_paths, "https://supabase.test", "anon-key")

    assert install_calls == ["supabase"]
    assert "MINDROOM_NO_AUTO_INSTALL_TOOLS" not in str(err.value)


def test_validate_supabase_token_catches_supabase_auth_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Invalid Supabase tokens should fail auth instead of surfacing optional-package internals."""

    class _FakeAuthError(Exception):
        pass

    class _FakeAuth:
        @staticmethod
        def get_user(_token: str) -> NoReturn:
            msg = "invalid jwt"
            raise _FakeAuthError(msg)

    class _FakeClient:
        auth = _FakeAuth()

    imported_modules: list[str] = []

    def _fake_import_module(module_name: str) -> SimpleNamespace:
        imported_modules.append(module_name)
        assert module_name == "supabase_auth.errors"
        return SimpleNamespace(AuthError=_FakeAuthError)

    monkeypatch.setattr(auth.importlib, "import_module", _fake_import_module)
    auth_state = auth.ApiAuthState(
        runtime_paths=_runtime_paths(tmp_path),
        settings=auth.ApiAuthSettings(
            platform_login_url="https://platform.example.com/login",
            supabase_url="https://supabase.example.com",
            supabase_anon_key="anon-key",
            account_id=None,
            mindroom_api_key=None,
        ),
        supabase_auth=_FakeClient(),
    )

    assert auth._validate_supabase_token("bad-token", auth_state) is None
    assert imported_modules == ["supabase_auth.errors"]


def test_ensure_frontend_dist_dir_builds_repo_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Source checkouts should auto-build frontend assets when they are missing."""
    frontend_source_dir = tmp_path / "frontend"
    frontend_source_dir.mkdir()
    (frontend_source_dir / "package.json").write_text("{}")
    frontend_dist_dir = frontend_source_dir / "dist"

    commands: list[tuple[list[str], Path]] = []

    def _fake_run(command: list[str], *, check: bool, cwd: Path) -> None:
        assert check is True
        commands.append((command, cwd))
        if command[1:] == ["run", "vite", "build"]:
            frontend_dist_dir.mkdir()

    monkeypatch.setattr(frontend_assets, "_PACKAGE_FRONTEND_DIR", tmp_path / "package-assets")
    monkeypatch.setattr(frontend_assets, "_REPO_FRONTEND_SOURCE_DIR", frontend_source_dir)
    monkeypatch.setattr(frontend_assets, "_REPO_FRONTEND_DIST_DIR", frontend_dist_dir)
    monkeypatch.setattr(frontend_assets, "_FRONTEND_BUILD_ATTEMPTED", False)
    monkeypatch.setattr(frontend_assets.shutil, "which", lambda name: "/usr/bin/bun" if name == "bun" else None)
    monkeypatch.setattr(frontend_assets.subprocess, "run", _fake_run)

    assert frontend_assets.ensure_frontend_dist_dir(_runtime_paths(tmp_path)) == frontend_dist_dir
    assert commands == [
        (["/usr/bin/bun", "install", "--frozen-lockfile"], frontend_source_dir),
        (["/usr/bin/bun", "run", "tsc"], frontend_source_dir),
        (["/usr/bin/bun", "run", "vite", "build"], frontend_source_dir),
    ]


def test_ensure_frontend_dist_dir_respects_disable_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Source checkouts should not auto-build when explicitly disabled."""
    frontend_source_dir = tmp_path / "frontend"
    frontend_source_dir.mkdir()
    (frontend_source_dir / "package.json").write_text("{}")

    monkeypatch.setattr(frontend_assets, "_PACKAGE_FRONTEND_DIR", tmp_path / "package-assets")
    monkeypatch.setattr(frontend_assets, "_REPO_FRONTEND_SOURCE_DIR", frontend_source_dir)
    monkeypatch.setattr(frontend_assets, "_REPO_FRONTEND_DIST_DIR", frontend_source_dir / "dist")
    monkeypatch.setattr(frontend_assets, "_FRONTEND_BUILD_ATTEMPTED", False)
    monkeypatch.setattr(frontend_assets.shutil, "which", lambda _name: "/usr/bin/bun")

    assert (
        frontend_assets.ensure_frontend_dist_dir(
            _runtime_paths(tmp_path, process_env={"MINDROOM_AUTO_BUILD_FRONTEND": "0"}),
        )
        is None
    )


def test_ensure_frontend_dist_dir_uses_runtime_relative_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Relative MINDROOM_FRONTEND_DIST should resolve from the runtime config directory."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    frontend_dist_dir = config_dir / "frontend-dist"
    frontend_dist_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    (config_dir / ".env").write_text("MINDROOM_FRONTEND_DIST=frontend-dist\n", encoding="utf-8")

    monkeypatch.setattr(frontend_assets, "_PACKAGE_FRONTEND_DIR", tmp_path / "missing-package-assets")
    monkeypatch.setattr(frontend_assets, "_REPO_FRONTEND_DIST_DIR", tmp_path / "missing-repo-dist")
    monkeypatch.setattr(frontend_assets, "_FRONTEND_BUILD_ATTEMPTED", True)

    runtime_paths = constants.resolve_primary_runtime_paths(config_path=config_path, process_env={})

    assert frontend_assets.ensure_frontend_dist_dir(runtime_paths) == frontend_dist_dir.resolve()


def test_ensure_writable_config_path_seeds_from_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Managed deployments should seed the writable config from the mounted template."""
    writable_config = tmp_path / "data" / "config.yaml"
    template_config = tmp_path / "template.yaml"
    template_config.write_text("agents: {}\nmodels: {}\n", encoding="utf-8")
    monkeypatch.setenv("MINDROOM_CONFIG_TEMPLATE", str(template_config))
    runtime_paths = constants.resolve_runtime_paths(config_path=writable_config)

    assert constants.ensure_writable_config_path(runtime_paths=runtime_paths) is True
    assert writable_config.read_text(encoding="utf-8") == template_config.read_text(encoding="utf-8")


def test_api_lifespan_syncs_env_credentials_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """API startup should run env credential sync via the FastAPI lifespan hook."""
    sync_calls: list[str] = []
    watch_calls: list[str] = []
    main.initialize_api_app(
        main.app,
        constants.resolve_primary_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={},
        ),
    )

    async def _fake_watch_config(
        stop_event: asyncio.Event,
        _app: FastAPI,
    ) -> None:
        assert main._app_runtime_paths(_app) == main._app_runtime_paths(main.app)
        watch_calls.append("watch")
        await stop_event.wait()

    monkeypatch.setattr(
        main,
        "sync_env_to_credentials",
        lambda runtime_paths: sync_calls.append(str(runtime_paths.config_path)),
    )
    monkeypatch.setattr(main, "_watch_config", _fake_watch_config)

    with TestClient(main.app) as client:
        assert client.get("/api/health").status_code == 200

    assert len(sync_calls) == 1
    assert watch_calls == ["watch"]


def test_exported_api_app_has_initialized_runtime_paths() -> None:
    """The exported module app should be runnable without separate initialization."""
    assert isinstance(main._app_runtime_paths(main.app), constants.RuntimePaths)


def test_initialize_api_app_initializes_fresh_app_state(tmp_path: Path) -> None:
    """A freshly constructed FastAPI app should get the full MindRoom API state."""
    fresh_app = FastAPI()
    runtime_paths = _runtime_paths(tmp_path)

    main.initialize_api_app(fresh_app, runtime_paths)

    assert main._app_runtime_paths(fresh_app) == runtime_paths
    assert main._app_context(fresh_app).config_data == {}
    assert hasattr(config_lifecycle.require_api_state(fresh_app).config_lock, "acquire")
    assert auth.app_auth_state(fresh_app).runtime_paths == runtime_paths


def test_app_auth_state_refreshes_after_runtime_swap(tmp_path: Path) -> None:
    """Replacing app runtime paths should invalidate cached auth settings."""
    fresh_app = FastAPI()
    initial_runtime = _runtime_paths(tmp_path, process_env={})
    refreshed_runtime = _runtime_paths(
        tmp_path,
        process_env={"MINDROOM_API_KEY": "updated-key"},
    )

    main.initialize_api_app(fresh_app, initial_runtime)
    assert auth.app_auth_state(fresh_app).settings.mindroom_api_key is None

    main.initialize_api_app(fresh_app, refreshed_runtime)

    assert auth.app_auth_state(fresh_app).settings.mindroom_api_key == "updated-key"


def test_initialize_api_app_clears_config_cache_when_config_path_changes(tmp_path: Path) -> None:
    """Swapping an app to a different config file should drop the previous cached payload."""
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first_runtime = constants.resolve_primary_runtime_paths(
        config_path=first_dir / "config.yaml",
        storage_path=first_dir / "mindroom_data",
        process_env={},
    )
    second_runtime = constants.resolve_primary_runtime_paths(
        config_path=second_dir / "config.yaml",
        storage_path=second_dir / "mindroom_data",
        process_env={},
    )
    first_runtime.config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "agents": {"first": {"display_name": "First", "role": "r", "rooms": ["lobby"]}},
                "defaults": {"markdown": True},
            },
        ),
        encoding="utf-8",
    )
    second_runtime.config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "agents": {"second": {"display_name": "Second", "role": "r", "rooms": ["lobby"]}},
                "defaults": {"markdown": True},
            },
        ),
        encoding="utf-8",
    )
    fresh_app = FastAPI()

    main.initialize_api_app(fresh_app, first_runtime)
    config_lifecycle.load_config_into_app(first_runtime, fresh_app)
    assert set(main._app_context(fresh_app).config_data["agents"]) == {"first"}

    main.initialize_api_app(fresh_app, second_runtime)

    assert main._app_context(fresh_app).config_data == {}


def test_initialize_api_app_clears_config_cache_when_runtime_changes(tmp_path: Path) -> None:
    """Swapping to the same config path under a different runtime should drop cached config."""
    runtime_one = _runtime_paths(tmp_path, process_env={})
    runtime_two = _runtime_paths(tmp_path, process_env={"MINDROOM_NAMESPACE": "ns12"})
    runtime_one.config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "agents": {"first": {"display_name": "First", "role": "r", "rooms": ["lobby"]}},
                "defaults": {"markdown": True},
            },
        ),
        encoding="utf-8",
    )
    fresh_app = FastAPI()

    main.initialize_api_app(fresh_app, runtime_one)
    config_lifecycle.load_config_into_app(runtime_one, fresh_app)
    assert set(main._app_context(fresh_app).config_data["agents"]) == {"first"}

    main.initialize_api_app(fresh_app, runtime_two)

    assert main._app_context(fresh_app).config_data == {}


def test_load_config_into_app_discards_stale_results_after_runtime_swap(tmp_path: Path) -> None:
    """A late load result from an old runtime must not poison the current app cache."""
    fresh_app = FastAPI()
    first_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        process_env={},
    )
    second_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        process_env={},
    )
    started = threading.Event()
    allow_finish = threading.Event()
    original_loader = config_lifecycle.load_runtime_config_model

    def _fake_loader(
        runtime_paths: constants.RuntimePaths,
        *,
        tolerate_plugin_load_errors: bool = False,
    ) -> Config:
        if runtime_paths == first_runtime:
            started.set()
            allow_finish.wait(timeout=1)
            message = "invalid old config"
            raise yaml.YAMLError(message)
        if runtime_paths == second_runtime:
            return Config.validate_with_runtime(
                {
                    "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                    "router": {"model": "default"},
                    "agents": {
                        "second": {
                            "display_name": "Second",
                            "role": "valid",
                            "rooms": [],
                        },
                    },
                },
                second_runtime,
            )
        return original_loader(
            runtime_paths,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
        )

    with patch.object(config_lifecycle, "load_runtime_config_model", side_effect=_fake_loader):
        main.initialize_api_app(fresh_app, first_runtime)

        stale_thread = threading.Thread(
            target=config_lifecycle.load_config_into_app,
            args=(first_runtime, fresh_app),
        )
        stale_thread.start()
        assert started.wait(timeout=1)

        main.initialize_api_app(fresh_app, second_runtime)
        assert config_lifecycle.load_config_into_app(second_runtime, fresh_app) is True
        allow_finish.set()
        stale_thread.join(timeout=1)

    context = main._app_context(fresh_app)
    assert context.runtime_paths == second_runtime
    assert context.config_load_result == main.ConfigLoadResult(success=True)
    assert set(context.config_data["agents"]) == {"second"}


def test_load_config_into_app_ignores_runtime_mismatches_after_api_runtime_swap(tmp_path: Path) -> None:
    """Loading one old runtime after a swap must not overwrite the current app context."""
    fresh_app = FastAPI()
    first_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        process_env={},
    )
    second_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        process_env={},
    )
    first_runtime.config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"first": {"display_name": "First", "role": "old", "rooms": []}},
            },
        ),
        encoding="utf-8",
    )
    second_runtime.config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"second": {"display_name": "Second", "role": "new", "rooms": []}},
            },
        ),
        encoding="utf-8",
    )

    main.initialize_api_app(fresh_app, first_runtime)
    assert config_lifecycle.load_config_into_app(first_runtime, fresh_app) is True
    assert set(main._app_context(fresh_app).config_data["agents"]) == {"first"}

    main.initialize_api_app(fresh_app, second_runtime)
    assert config_lifecycle.load_config_into_app(second_runtime, fresh_app) is True
    assert set(main._app_context(fresh_app).config_data["agents"]) == {"second"}

    assert config_lifecycle.load_config_into_app(first_runtime, fresh_app) is False
    assert set(main._app_context(fresh_app).config_data["agents"]) == {"second"}
    assert main._app_context(fresh_app).config_load_result == main.ConfigLoadResult(success=True)


def test_reload_api_runtime_config_does_not_clear_worker_snapshot_on_failed_load(tmp_path: Path) -> None:
    """Failed API config loads should keep the last-good worker validation cache."""
    fresh_app = FastAPI()
    runtime_paths = _runtime_paths(tmp_path)
    main.initialize_api_app(fresh_app, runtime_paths)
    failure = main.ConfigLoadResult(success=False, error_status_code=422, error_detail="bad config")

    with (
        patch.object(config_lifecycle, "_load_config_result", return_value=(failure, None, None)),
        patch("mindroom.api.runtime_reload.clear_worker_validation_snapshot_cache") as mock_clear_snapshot_cache,
        pytest.raises(HTTPException) as exc_info,
    ):
        runtime_reload.reload_api_runtime_config(fresh_app, runtime_paths)

    assert exc_info.value.status_code == 422
    mock_clear_snapshot_cache.assert_not_called()


def test_read_app_committed_config_uses_current_context_after_runtime_swap(tmp_path: Path) -> None:
    """Read helpers should use the runtime context that is current after locking."""
    fresh_app = FastAPI()
    first_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        process_env={},
    )
    second_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        process_env={},
    )
    swap_lock = _ContextSwapLock()
    first_snapshot = main.ApiSnapshot(
        generation=0,
        runtime_paths=first_runtime,
        config_data=_validated_authored_payload(first_runtime, "old"),
        config_load_result=main.ConfigLoadResult(success=True),
    )
    second_snapshot = main.ApiSnapshot(
        generation=1,
        runtime_paths=second_runtime,
        config_data=_validated_authored_payload(second_runtime, "new"),
        config_load_result=main.ConfigLoadResult(success=True),
    )
    fresh_app_state = config_lifecycle.ensure_app_state(fresh_app)
    fresh_app_state.api_state = main.ApiState(
        config_lock=cast("config_lifecycle.ApiConfigLock", swap_lock),
        snapshot=first_snapshot,
    )
    swap_lock.on_enter = lambda: setattr(
        fresh_app_state,
        "api_state",
        main.ApiState(
            config_lock=cast("config_lifecycle.ApiConfigLock", swap_lock),
            snapshot=second_snapshot,
        ),
    )

    loaded_agents = config_lifecycle.read_app_committed_config(
        fresh_app,
        lambda config_data: list(config_data["agents"]),
    )

    assert loaded_agents == ["new"]


def test_write_app_committed_config_uses_current_context_after_runtime_swap(tmp_path: Path) -> None:
    """Write helpers should mutate the runtime context that is current after locking."""
    fresh_app = FastAPI()
    first_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        process_env={},
    )
    second_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        process_env={},
    )
    first_runtime.config_path.write_text(
        yaml.safe_dump(_authored_config_payload("old")),
        encoding="utf-8",
    )
    second_runtime.config_path.write_text(
        yaml.safe_dump(_authored_config_payload("new")),
        encoding="utf-8",
    )
    swap_lock = _ContextSwapLock()
    first_snapshot = main.ApiSnapshot(
        generation=0,
        runtime_paths=first_runtime,
        config_data=_validated_authored_payload(first_runtime, "old"),
        config_load_result=main.ConfigLoadResult(success=True),
    )
    second_snapshot = main.ApiSnapshot(
        generation=1,
        runtime_paths=second_runtime,
        config_data=_validated_authored_payload(second_runtime, "new"),
        config_load_result=main.ConfigLoadResult(success=True),
    )
    fresh_app_state = config_lifecycle.ensure_app_state(fresh_app)
    fresh_app_state.api_state = main.ApiState(
        config_lock=cast("config_lifecycle.ApiConfigLock", swap_lock),
        snapshot=first_snapshot,
    )
    swap_lock.on_enter = lambda: setattr(
        fresh_app_state,
        "api_state",
        main.ApiState(
            config_lock=cast("config_lifecycle.ApiConfigLock", swap_lock),
            snapshot=second_snapshot,
        ),
    )

    def _mutate(config_data: dict[str, Any]) -> None:
        config_data["agents"]["written"] = {
            "display_name": "Written",
            "role": "updated",
            "rooms": [],
        }

    config_lifecycle.write_app_committed_config(
        fresh_app,
        _mutate,
        error_prefix="Failed to update configuration",
    )

    assert set(main._app_context(fresh_app).config_data["agents"]) == {"new", "written"}
    assert set(yaml.safe_load(first_runtime.config_path.read_text(encoding="utf-8"))["agents"]) == {"old"}
    assert set(yaml.safe_load(second_runtime.config_path.read_text(encoding="utf-8"))["agents"]) == {
        "new",
        "written",
    }


def test_api_lifespan_loads_config_from_injected_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bundled API startup should load config from the runtime injected before lifespan starts."""
    config_path = tmp_path / "custom-config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"only_alt": {"display_name": "OnlyAlt", "role": "alt", "rooms": []}},
            },
        ),
        encoding="utf-8",
    )
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
    )
    main.initialize_api_app(main.app, runtime_paths)
    main._app_context(main.app).config_data = {"agents": {"wrong": {"display_name": "Wrong"}}}

    async def _idle_watch_config(
        stop_event: asyncio.Event,
        _app: FastAPI,
    ) -> None:
        await stop_event.wait()

    async def _idle_worker_cleanup(stop_event: asyncio.Event, _app: FastAPI) -> None:
        await stop_event.wait()

    monkeypatch.setattr(main, "sync_env_to_credentials", lambda runtime_paths: None)  # noqa: ARG005
    monkeypatch.setattr(main, "_watch_config", _idle_watch_config)
    monkeypatch.setattr(main, "_worker_cleanup_loop", _idle_worker_cleanup)

    with TestClient(main.app) as client:
        response = client.post("/api/config/load")

    assert response.status_code == 200
    assert set(response.json()["agents"]) == {"only_alt"}


@pytest.mark.asyncio
async def test_watch_config_follows_runtime_swaps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Config watching should follow the app's current runtime paths."""
    loaded_paths: list[Path] = []
    load_event = asyncio.Event()
    stop_event = asyncio.Event()
    first_config_path = tmp_path / "first.yaml"
    second_config_path = tmp_path / "second.yaml"
    first_config_path.write_text("models: {}\n", encoding="utf-8")
    second_config_path.write_text("models: {}\n", encoding="utf-8")
    first_runtime = constants.resolve_primary_runtime_paths(config_path=first_config_path, process_env={})
    second_runtime = constants.resolve_primary_runtime_paths(config_path=second_config_path, process_env={})
    main.initialize_api_app(main.app, first_runtime)
    monkeypatch.setattr(
        config_lifecycle,
        "load_config_into_app",
        lambda runtime_paths, _app: _record_loaded_path(runtime_paths, loaded_paths, load_event),
    )

    watch_task = asyncio.create_task(main._watch_config(stop_event, main.app, poll_interval_seconds=0.01))

    await asyncio.sleep(0.02)
    first_timestamp = time.time() + 1
    first_config_path.write_text("models: {default: {provider: openai, id: gpt-5.4}}\n", encoding="utf-8")
    os.utime(first_config_path, (first_timestamp, first_timestamp))
    await asyncio.wait_for(load_event.wait(), timeout=1)
    assert loaded_paths == [first_config_path]

    main.initialize_api_app(main.app, second_runtime)
    load_event.clear()
    await asyncio.sleep(0.02)
    second_timestamp = first_timestamp + 1
    second_config_path.write_text("models: {default: {provider: openai, id: gpt-5.4}}\n", encoding="utf-8")
    os.utime(second_config_path, (second_timestamp, second_timestamp))
    await asyncio.wait_for(load_event.wait(), timeout=1)
    assert loaded_paths == [first_config_path, second_config_path]

    stop_event.set()
    await watch_task

    assert loaded_paths == [first_config_path, second_config_path]


def _record_loaded_path(
    runtime_paths: constants.RuntimePaths,
    loaded_paths: list[Path],
    load_event: asyncio.Event,
) -> None:
    loaded_paths.append(runtime_paths.config_path)
    load_event.set()


@pytest.mark.asyncio
async def test_worker_cleanup_loop_uses_current_runtime_after_runtime_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Worker cleanup should use the current runtime, not the startup runtime."""
    stop_event = asyncio.Event()
    first_runtime = constants.resolve_primary_runtime_paths(config_path=tmp_path / "first.yaml", process_env={})
    second_runtime = constants.resolve_primary_runtime_paths(config_path=tmp_path / "second.yaml", process_env={})
    cleanup_paths: list[Path] = []

    class _FakeRuntimeConfig:
        def get_worker_grantable_credentials(self) -> frozenset[str]:
            return constants.DEFAULT_WORKER_GRANTABLE_CREDENTIALS

    def _fake_cleanup(
        runtime_paths: constants.RuntimePaths,
        *,
        runtime_config: object | None = None,
        worker_grantable_credentials: frozenset[str] | None = None,
    ) -> int:
        del runtime_config
        assert worker_grantable_credentials == constants.DEFAULT_WORKER_GRANTABLE_CREDENTIALS
        cleanup_paths.append(runtime_paths.config_path)
        if len(cleanup_paths) == 1:
            main.initialize_api_app(main.app, second_runtime)
        else:
            stop_event.set()
        return 0

    async def _fake_to_thread(func: Callable[..., int], *args: object, **kwargs: object) -> int:
        return func(*args, **kwargs)

    def _read_current_runtime_config(
        api_app: FastAPI,
    ) -> tuple[_FakeRuntimeConfig, constants.RuntimePaths]:
        return _FakeRuntimeConfig(), main._app_runtime_paths(api_app)

    main.initialize_api_app(main.app, first_runtime)
    monkeypatch.setattr(main, "_worker_cleanup_interval_seconds", lambda _runtime_paths: 0.01)
    monkeypatch.setattr(main, "_cleanup_workers_once", _fake_cleanup)
    monkeypatch.setattr(main.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(
        main.config_lifecycle,
        "read_app_committed_runtime_config",
        _read_current_runtime_config,
    )

    await main._worker_cleanup_loop(stop_event, main.app, idle_poll_interval_seconds=0.01)

    assert cleanup_paths == [first_runtime.config_path, second_runtime.config_path]


def test_health_check(test_client: TestClient) -> None:
    """Test the health check endpoint."""
    reset_matrix_sync_health()
    response = test_client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["last_sync_time"] is None


def test_health_check_reports_stale_matrix_sync(test_client: TestClient) -> None:
    """Ready runtimes should fail health checks when Matrix sync responses go stale."""
    reset_matrix_sync_health()
    stale_sync_time = datetime.now(UTC) - timedelta(seconds=181)
    mark_matrix_sync_loop_started("router")
    mark_matrix_sync_success("router", stale_sync_time)
    set_runtime_ready()

    response = test_client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "last_sync_time": stale_sync_time.isoformat(),
        "stale_sync_entities": ["router"],
    }
    reset_matrix_sync_health()
    reset_runtime_state()


def test_health_startup_grace_before_first_sync(test_client: TestClient) -> None:
    """Health should return 200 during startup before the first sync callback arrives."""
    reset_matrix_sync_health()
    mark_matrix_sync_loop_started("router")
    mark_matrix_sync_loop_started("general")
    set_runtime_ready()

    response = test_client.get("/api/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["last_sync_time"] is None
    assert "stale_sync_entities" not in data

    reset_matrix_sync_health()
    reset_runtime_state()


def test_health_after_watchdog_restart_stays_unhealthy_until_sync(test_client: TestClient) -> None:
    """A restart must not hide a stale sync timestamp until a new sync succeeds."""
    reset_matrix_sync_health()
    stale_time = datetime.now(UTC) - timedelta(seconds=300)
    mark_matrix_sync_loop_started("router")
    mark_matrix_sync_success("router", stale_time)
    set_runtime_ready()

    # Confirm it's stale first
    response = test_client.get("/api/health")
    assert response.status_code == 503

    # Simulate watchdog restart.
    mark_matrix_sync_loop_started("router")

    response = test_client.get("/api/health")
    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "last_sync_time": stale_time.isoformat(),
        "stale_sync_entities": ["router"],
    }

    reset_matrix_sync_health()
    reset_runtime_state()


def test_health_mixed_entities_some_stale(test_client: TestClient) -> None:
    """If one entity is stale and another is fresh, health should report unhealthy."""
    reset_matrix_sync_health()
    fresh_time = datetime.now(UTC) - timedelta(seconds=5)
    stale_time = datetime.now(UTC) - timedelta(seconds=300)
    mark_matrix_sync_loop_started("router")
    mark_matrix_sync_success("router", fresh_time)
    mark_matrix_sync_loop_started("general")
    mark_matrix_sync_success("general", stale_time)
    set_runtime_ready()

    response = test_client.get("/api/health")

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["stale_sync_entities"] == ["general"]

    reset_matrix_sync_health()
    reset_runtime_state()


def test_health_mixed_entities_one_in_startup_grace(test_client: TestClient) -> None:
    """An entity still in startup grace (no sync yet) should not cause 503."""
    reset_matrix_sync_health()
    fresh_time = datetime.now(UTC) - timedelta(seconds=5)
    mark_matrix_sync_loop_started("router")
    mark_matrix_sync_success("router", fresh_time)
    # general just started, no sync callback yet
    mark_matrix_sync_loop_started("general")
    set_runtime_ready()

    response = test_client.get("/api/health")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "stale_sync_entities" not in data

    reset_matrix_sync_health()
    reset_runtime_state()


def test_health_shutdown_clears_entity(test_client: TestClient) -> None:
    """After clearing an entity, it should no longer affect health."""
    from mindroom.matrix.health import clear_matrix_sync_state  # noqa: PLC0415

    reset_matrix_sync_health()
    mark_matrix_sync_loop_started("router")
    mark_matrix_sync_success("router", datetime.now(UTC) - timedelta(seconds=5))
    mark_matrix_sync_loop_started("general")
    mark_matrix_sync_success("general", datetime.now(UTC) - timedelta(seconds=300))
    set_runtime_ready()

    # Unhealthy due to stale general
    response = test_client.get("/api/health")
    assert response.status_code == 503

    # Shut down the stale entity
    clear_matrix_sync_state("general")

    response = test_client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

    reset_matrix_sync_health()
    reset_runtime_state()


def test_readiness_check_reports_idle(test_client: TestClient) -> None:
    """Readiness should stay closed until the runtime reports successful startup."""
    reset_runtime_state()

    response = test_client.get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "idle", "detail": "MindRoom is not ready"}


def test_readiness_check_reports_ready(test_client: TestClient) -> None:
    """Readiness should open once the orchestrator marks startup complete."""
    set_runtime_starting()
    set_runtime_ready()

    response = test_client.get("/api/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    reset_runtime_state()


def test_readiness_check_reports_startup_detail(test_client: TestClient) -> None:
    """Readiness should expose the current startup stage while the runtime is still booting."""
    set_runtime_starting("Setting up Matrix rooms and memberships")

    response = test_client.get("/api/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "starting",
        "detail": "Setting up Matrix rooms and memberships",
    }
    reset_runtime_state()


def test_worker_cleanup_once_skips_when_backend_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Background worker cleanup should no-op when no backend is configured."""
    monkeypatch.setattr(main, "primary_worker_backend_available", lambda *_args, **_kwargs: False)

    assert (
        main._cleanup_workers_once(
            main._app_runtime_paths(main.app),
            worker_grantable_credentials=constants.DEFAULT_WORKER_GRANTABLE_CREDENTIALS,
        )
        == 0
    )


def test_worker_cleanup_once_skips_kubernetes_without_committed_runtime_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kubernetes cleanup should skip the cycle when no committed runtime config is available."""
    monkeypatch.setattr(main, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main, "primary_worker_backend_name", lambda *_args, **_kwargs: "kubernetes")

    def _unexpected_get_primary_worker_manager(*_args: object, **_kwargs: object) -> object:
        msg = "cleanup should not build a Kubernetes worker manager without a committed snapshot"
        raise AssertionError(msg)

    monkeypatch.setattr(main, "get_primary_worker_manager", _unexpected_get_primary_worker_manager)

    assert main._cleanup_workers_once(main._app_runtime_paths(main.app)) == 0


def test_worker_cleanup_once_cleans_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Background worker cleanup should delegate to the configured worker manager."""

    class _FakeWorkerManager:
        backend_name = "kubernetes"

        def cleanup_idle_workers(self) -> list[WorkerHandle]:
            return [
                WorkerHandle(
                    worker_id="worker-1",
                    worker_key="worker-key",
                    endpoint="http://worker/api/sandbox-runner/execute",
                    auth_token=TEST_WORKER_AUTH,
                    status="idle",
                    backend_name="kubernetes",
                    last_used_at=1.0,
                    created_at=0.0,
                ),
            ]

    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    monkeypatch.setattr(main, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main, "primary_worker_backend_name", lambda *_args, **_kwargs: "kubernetes")
    captured_kwargs: dict[str, object] = {}

    def _fake_get_primary_worker_manager(*_args: object, **kwargs: object) -> _FakeWorkerManager:
        captured_kwargs.update(kwargs)
        return _FakeWorkerManager()

    monkeypatch.setattr(main, "get_primary_worker_manager", _fake_get_primary_worker_manager)

    runtime_paths = main._app_runtime_paths(main.app)
    runtime_config = Config.validate_with_runtime({}, runtime_paths)
    assert (
        main._cleanup_workers_once(
            runtime_paths,
            runtime_config=runtime_config,
            worker_grantable_credentials=runtime_config.get_worker_grantable_credentials(),
        )
        == 1
    )
    assert captured_kwargs["kubernetes_tool_validation_snapshot"] is not None
    assert captured_kwargs["worker_grantable_credentials"] == runtime_config.get_worker_grantable_credentials()


def test_list_workers_endpoint(test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard should expose backend-neutral worker metadata."""

    class _FakeWorkerManager:
        def list_workers(self, *, include_idle: bool = True) -> list[WorkerHandle]:
            assert include_idle is True
            return [
                WorkerHandle(
                    worker_id="worker-1",
                    worker_key="worker-key",
                    endpoint="http://worker/api/sandbox-runner/execute",
                    auth_token=TEST_WORKER_AUTH,
                    status="ready",
                    backend_name="kubernetes",
                    last_used_at=12.0,
                    created_at=1.0,
                    debug_metadata={"namespace": "mindroom-instances"},
                ),
            ]

    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_IMAGE", "ghcr.io/mindroom-ai/mindroom:latest")
    monkeypatch.setenv("MINDROOM_KUBERNETES_WORKER_STORAGE_PVC_NAME", "mindroom-storage")
    monkeypatch.setattr(workers_api, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(workers_api, "primary_worker_backend_name", lambda *_args, **_kwargs: "kubernetes")
    captured_kwargs: dict[str, object] = {}

    def _fake_get_primary_worker_manager(*_args: object, **kwargs: object) -> _FakeWorkerManager:
        captured_kwargs.update(kwargs)
        return _FakeWorkerManager()

    monkeypatch.setattr(
        workers_api,
        "get_primary_worker_manager",
        _fake_get_primary_worker_manager,
    )

    response = test_client.get("/api/workers")

    assert response.status_code == 200
    assert response.json()["workers"][0]["worker_key"] == "worker-key"
    assert response.json()["workers"][0]["backend_name"] == "kubernetes"
    assert captured_kwargs["kubernetes_tool_validation_snapshot"] is not None
    assert captured_kwargs["worker_grantable_credentials"] == constants.DEFAULT_WORKER_GRANTABLE_CREDENTIALS


def test_cleanup_workers_endpoint(test_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard should expose manual idle-worker cleanup."""

    class _FakeWorkerManager:
        idle_timeout_seconds = 60.0

        def cleanup_idle_workers(self) -> list[WorkerHandle]:
            return [
                WorkerHandle(
                    worker_id="worker-1",
                    worker_key="worker-key",
                    endpoint="http://worker/api/sandbox-runner/execute",
                    auth_token=TEST_WORKER_AUTH,
                    status="idle",
                    backend_name="kubernetes",
                    last_used_at=12.0,
                    created_at=1.0,
                ),
            ]

    monkeypatch.setattr(workers_api, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        workers_api,
        "get_primary_worker_manager",
        lambda *_args, **_kwargs: _FakeWorkerManager(),
    )

    response = test_client.post("/api/workers/cleanup")

    assert response.status_code == 200
    assert response.json()["idle_timeout_seconds"] == 60.0
    assert response.json()["cleaned_workers"][0]["status"] == "idle"


def test_load_config(test_client: TestClient) -> None:
    """Test loading configuration."""
    response = test_client.post("/api/config/load")
    assert response.status_code == 200

    config = response.json()
    assert "agents" in config
    assert "models" in config
    assert "test_agent" in config["agents"]


def test_get_agents(test_client: TestClient) -> None:
    """Test getting all agents."""
    # First load config
    test_client.post("/api/config/load")

    response = test_client.get("/api/config/agents")
    assert response.status_code == 200

    agents = response.json()
    assert isinstance(agents, list)
    assert len(agents) > 0

    # Check agent structure
    agent = agents[0]
    assert "id" in agent
    assert "display_name" in agent
    assert "tools" in agent
    assert "rooms" in agent


def test_create_agent(test_client: TestClient, sample_agent_data: dict[str, Any], temp_config_file: Path) -> None:
    """Test creating a new agent."""
    # Load config first
    test_client.post("/api/config/load")

    response = test_client.post("/api/config/agents", json=sample_agent_data)
    assert response.status_code == 200

    result = response.json()
    assert "id" in result
    assert result["success"] is True

    # Verify it was saved to file
    with temp_config_file.open() as f:
        config = yaml.safe_load(f)
    assert result["id"] in config["agents"]
    assert config["agents"][result["id"]]["display_name"] == sample_agent_data["display_name"]


def test_update_agent(test_client: TestClient, temp_config_file: Path) -> None:
    """Test updating an existing agent."""
    # Load config first
    test_client.post("/api/config/load")

    update_data = {"display_name": "Updated Test Agent", "tools": ["calculator", "file"], "rooms": ["updated_room"]}

    response = test_client.put("/api/config/agents/test_agent", json=update_data)
    assert response.status_code == 200

    result = response.json()
    assert result["success"] is True

    # Verify file was updated
    with temp_config_file.open() as f:
        config = yaml.safe_load(f)
    assert config["agents"]["test_agent"]["display_name"] == "Updated Test Agent"
    assert "file" in config["agents"]["test_agent"]["tools"]
    assert "updated_room" in config["agents"]["test_agent"]["rooms"]


def test_delete_agent(test_client: TestClient, temp_config_file: Path) -> None:
    """Test deleting an agent."""
    # Load config first
    test_client.post("/api/config/load")

    response = test_client.delete("/api/config/agents/test_agent")
    assert response.status_code == 200

    result = response.json()
    assert result["success"] is True

    # Verify it was removed from file
    with temp_config_file.open() as f:
        config = yaml.safe_load(f)
    assert "test_agent" not in config["agents"]


def test_get_tools(test_client: TestClient) -> None:
    """Test getting available tools."""
    # First test the new endpoint that returns full tool metadata
    response = test_client.get("/api/tools/")
    assert response.status_code == 200

    data = response.json()
    assert "tools" in data
    assert isinstance(data["tools"], list)
    assert len(data["tools"]) > 0

    # Check that some expected tools are present
    tool_names = {tool["name"] for tool in data["tools"]}
    assert "calculator" in tool_names
    assert "file" in tool_names
    assert "shell" in tool_names

    # Check that tools have the expected structure
    first_tool = data["tools"][0]
    assert "name" in first_tool
    assert "display_name" in first_tool
    assert "description" in first_tool
    assert "category" in first_tool
    assert "icon_color" in first_tool  # New field we added

    shell_tool = next(tool for tool in data["tools"] if tool["name"] == "shell")
    assert shell_tool["agent_override_fields"] == [
        {
            "authored_override": True,
            "default": None,
            "description": "Extra env var names or glob patterns exposed to shell execution for this agent only.",
            "label": "Env Passthrough",
            "name": "extra_env_passthrough",
            "options": None,
            "placeholder": "GITEA_TOKEN",
            "required": False,
            "type": "string[]",
            "validation": None,
        },
        {
            "authored_override": True,
            "default": None,
            "description": "Path entries prepended to PATH for this agent's shell tool only.",
            "label": "PATH Prepend",
            "name": "shell_path_prepend",
            "options": None,
            "placeholder": "/run/wrappers/bin",
            "required": False,
            "type": "string[]",
            "validation": None,
        },
    ]

    calculator_tool = next(tool for tool in data["tools"] if tool["name"] == "calculator")
    assert calculator_tool["agent_override_fields"] is None


def test_non_oauth_auth_provider_uses_required_credential_fields(tmp_path: Path) -> None:
    """Custom non-OAuth auth providers should still use ordinary credential presence."""
    runtime_paths = _runtime_paths(tmp_path)
    credentials_manager = get_runtime_credentials_manager(runtime_paths)
    save_scoped_credentials(
        "my_shared_creds",
        {"api_key": "secret"},
        credentials_manager=credentials_manager,
        worker_target=None,
    )
    context = tools_api._ResolvedToolAvailabilityContext(
        execution_scope=None,
        dashboard_configuration_supported=True,
        status_authoritative=True,
        credentials_manager=credentials_manager,
        worker_target=None,
        allowed_shared_services=None,
        auth_provider_credential_services={},
        oauth_providers={},
        runtime_paths=runtime_paths,
    )
    tools = [
        {
            "name": "custom_shared_api",
            "status": "requires_config",
            "auth_provider": "my_shared_creds",
            "config_fields": [{"name": "api_key", "required": True}],
        },
    ]

    tools_api._update_tools_statuses(tools, context)

    assert tools[0]["status"] == "available"


@pytest.mark.parametrize(
    ("service", "execution_scope", "expected_allowed_services"),
    [
        ("google_drive", "shared", frozenset({"google_drive"})),
        ("google_calendar", "shared", frozenset({"google_calendar"})),
        ("google_sheets", "shared", frozenset({"google_sheets"})),
        ("gmail", "shared", frozenset({"gmail"})),
        ("google_drive_oauth", "shared", frozenset({"google_drive_oauth"})),
        ("weather", "shared", frozenset({"weather"})),
        ("google_drive", "user", frozenset({"weather"})),
        ("google_drive", "user_agent", frozenset({"weather"})),
    ],
)
def test_effective_allowed_shared_services_uses_credential_policy(
    tmp_path: Path,
    service: str,
    execution_scope: str,
    expected_allowed_services: frozenset[str],
) -> None:
    """Tool availability should apply local-only credential policy before worker grants."""
    context = tools_api._ResolvedToolAvailabilityContext(
        execution_scope=execution_scope,
        dashboard_configuration_supported=True,
        status_authoritative=True,
        credentials_manager=get_runtime_credentials_manager(_runtime_paths(tmp_path)),
        worker_target=None,
        allowed_shared_services=frozenset({"weather"}),
        auth_provider_credential_services={},
        oauth_providers={},
        runtime_paths=_runtime_paths(tmp_path),
    )

    assert tools_api._effective_allowed_shared_services(service, context) == expected_allowed_services


def test_get_tools_marks_shared_only_integrations_unsupported_for_isolating_worker_scope(
    test_client: TestClient,
) -> None:
    """Shared-only integrations should stay visible but be marked unsupported."""
    config = _config_with_worker_scope("user")
    runtime_paths = main._app_runtime_paths(main.app)

    with patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert tools_by_name["homeassistant"]["execution_scope_supported"] is False
    assert tools_by_name["spotify"]["execution_scope_supported"] is False
    assert tools_by_name["gmail"]["execution_scope_supported"] is True
    assert tools_by_name["google_calendar"]["execution_scope_supported"] is True
    assert tools_by_name["google_sheets"]["execution_scope_supported"] is True
    assert "calculator" in tools_by_name
    assert tools_by_name["calculator"]["execution_scope_supported"] is True


def test_get_tools_execution_scope_override_marks_backend_tools_unsupported(
    test_client: TestClient,
) -> None:
    """Draft execution-scope overrides should drive shared-only tool support flags."""
    config = _config_with_worker_scope("shared")
    runtime_paths = main._app_runtime_paths(main.app)
    tools = [
        {
            "name": "homeassistant",
            "display_name": "Home Assistant",
            "description": "HA",
            "category": "automation",
            "status": "requires_config",
            "setup_type": "special",
            "config_fields": None,
        },
        {
            "name": "calculator",
            "display_name": "Calculator",
            "description": "Calc",
            "category": "utility",
            "status": "available",
            "setup_type": "none",
            "config_fields": None,
        },
    ]

    with (
        patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general&execution_scope=user")

    assert response.status_code == 200
    assert response.json()["status_authoritative"] is False
    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert tools_by_name["homeassistant"]["execution_scope_supported"] is False
    assert tools_by_name["homeassistant"]["dashboard_configuration_supported"] is False
    assert "calculator" in tools_by_name
    assert tools_by_name["calculator"]["execution_scope_supported"] is True


def test_get_tools_explicit_unscoped_override_does_not_fall_back_to_saved_scope(
    test_client: TestClient,
) -> None:
    """An explicit unscoped draft must not fall back to the persisted agent scope."""
    config = _config_with_worker_scope("user")
    runtime_paths = main._app_runtime_paths(main.app)
    tools = [
        {
            "name": "homeassistant",
            "display_name": "Home Assistant",
            "description": "HA",
            "category": "automation",
            "status": "requires_config",
            "setup_type": "special",
            "config_fields": None,
        },
        {
            "name": "calculator",
            "display_name": "Calculator",
            "description": "Calc",
            "category": "utility",
            "status": "available",
            "setup_type": "none",
            "config_fields": None,
        },
    ]

    with (
        patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general&execution_scope=unscoped")

    assert response.status_code == 200
    assert response.json()["status_authoritative"] is False
    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert "homeassistant" in tools_by_name
    assert "calculator" in tools_by_name
    assert tools_by_name["homeassistant"]["dashboard_configuration_supported"] is False


def test_get_tools_unknown_agent_rejected(test_client: TestClient) -> None:
    """Tool preview should reject unknown agents instead of falling back to shared state."""
    config = _config_with_worker_scope("shared")
    runtime_paths = main._app_runtime_paths(main.app)

    with patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)):
        response = test_client.get("/api/tools/?agent_name=missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown agent: missing"


def test_get_tools_marks_allowlisted_shared_ui_scoped_tools_available(test_client: TestClient) -> None:
    """Scoped tool preview should reflect allowlisted shared credentials regardless of source."""
    config = _config_with_worker_scope("user", worker_grantable_credentials=["weather"])
    runtime_paths = main._app_runtime_paths(main.app)
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("weather", {"WEATHER_API_KEY": "secret", "_source": "ui"})
    tools = [
        {
            "name": "weather",
            "display_name": "Weather",
            "description": "Weather lookup",
            "category": "information",
            "status": "requires_config",
            "setup_type": "api_key",
            "config_fields": [
                {
                    "name": "WEATHER_API_KEY",
                    "required": True,
                },
            ],
        },
    ]

    with (
        patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general&execution_scope=user")

    assert response.status_code == 200
    assert response.json()["status_authoritative"] is False
    tool = response.json()["tools"][0]
    assert tool["name"] == "weather"
    assert tool["status"] == "available"
    assert tool["dashboard_configuration_supported"] is False


def test_get_tools_hides_non_allowlisted_shared_scoped_credentials(test_client: TestClient) -> None:
    """Scoped tool preview should not claim non-allowlisted shared credentials are worker-visible."""
    config = _config_with_worker_scope("user")
    runtime_paths = main._app_runtime_paths(main.app)
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials("weather", {"WEATHER_API_KEY": "secret", "_source": "ui"})
    tools = [
        {
            "name": "weather",
            "display_name": "Weather",
            "description": "Weather lookup",
            "category": "information",
            "status": "requires_config",
            "setup_type": "api_key",
            "config_fields": [
                {
                    "name": "WEATHER_API_KEY",
                    "required": True,
                },
            ],
        },
    ]

    with (
        patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general&execution_scope=user")

    assert response.status_code == 200
    assert response.json()["status_authoritative"] is False
    tool = response.json()["tools"][0]
    assert tool["name"] == "weather"
    assert tool["status"] == "requires_config"
    assert tool["dashboard_configuration_supported"] is False


def test_get_tools_shared_scope_homeassistant_ignores_worker_allowlist(test_client: TestClient) -> None:
    """Shared-scope local integrations should reflect shared credentials without worker mirroring config."""
    config = _config_with_worker_scope("shared")
    runtime_paths = main._app_runtime_paths(main.app)
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "homeassistant",
        {
            "instance_url": "http://homeassistant.local:8123",
            "access_token": "ha-token",
            "_source": "ui",
        },
    )
    tools = [
        {
            "name": "homeassistant",
            "display_name": "Home Assistant",
            "description": "Home automation",
            "category": "home",
            "status": "requires_config",
            "setup_type": "special",
            "auth_provider": None,
            "config_fields": [],
        },
    ]

    with (
        patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    assert response.json()["status_authoritative"] is True
    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert tools_by_name["homeassistant"]["status"] == "available"


def test_get_tools_requires_oauth_token_for_generic_auth_provider(test_client: TestClient) -> None:
    """Generic OAuth-backed tools should not look connected from config-only credentials."""
    config = _config_with_worker_scope("shared")
    app_runtime_paths = main._app_runtime_paths(main.app)
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=app_runtime_paths.config_path,
        storage_path=app_runtime_paths.storage_root,
        process_env={
            "GOOGLE_DRIVE_CLIENT_ID": "client-id",
            "GOOGLE_DRIVE_CLIENT_SECRET": "client-secret",
        },
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="standalone",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    worker_key = resolve_worker_key("shared", identity, agent_name="general")
    assert worker_key is not None
    scoped_manager = manager.for_worker(worker_key)
    scoped_manager.save_credentials(
        "google_drive",
        {
            "list_files": True,
            "max_read_size": 1048576,
            "_source": "ui",
        },
    )
    tools = [
        {
            "name": "google_drive",
            "display_name": "Google Drive",
            "description": "Drive access",
            "category": "productivity",
            "status": "requires_config",
            "setup_type": "oauth",
            "auth_provider": "google_drive",
            "config_fields": [
                {
                    "name": "max_read_size",
                    "required": False,
                },
            ],
        },
    ]

    with (
        patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    tool = response.json()["tools"][0]
    assert tool["name"] == "google_drive"
    assert tool["status"] == "requires_config"

    manager.save_credentials(
        "google_drive_oauth",
        {
            "token": "drive-token",
            "refresh_token": "drive-refresh-token",
            "scopes": [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
            "_source": "oauth",
        },
    )
    with (
        patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        connected_response = test_client.get("/api/tools/?agent_name=general")

    assert connected_response.status_code == 200
    connected_tool = connected_response.json()["tools"][0]
    assert connected_tool["status"] == "available"


def test_get_tools_marks_google_oauth_tool_available_with_service_account(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Google OAuth-backed tools should be available when service-account auth is configured."""
    config = _config_with_worker_scope("shared")
    app_runtime_paths = main._app_runtime_paths(main.app)
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=app_runtime_paths.config_path,
        storage_path=app_runtime_paths.storage_root,
        process_env={
            "GOOGLE_SERVICE_ACCOUNT_FILE": str(tmp_path / "google-service-account.json"),
        },
    )
    tools = [
        {
            "name": "google_drive",
            "display_name": "Google Drive",
            "description": "Drive access",
            "category": "productivity",
            "status": "requires_config",
            "setup_type": "oauth",
            "auth_provider": "google_drive",
            "config_fields": [],
        },
    ]

    with (
        patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    tool = response.json()["tools"][0]
    assert tool["name"] == "google_drive"
    assert tool["status"] == "available"


def test_get_tools_does_not_treat_requester_owned_scoped_credentials_as_dashboard_truth(
    test_client: TestClient,
) -> None:
    """Requester-owned scoped credentials must not flip isolated dashboard status to available."""
    config = _config_with_worker_scope("user")
    runtime_paths = main._app_runtime_paths(main.app)
    tools = [
        {
            "name": "weather",
            "display_name": "Weather",
            "description": "Weather lookup",
            "category": "information",
            "status": "requires_config",
            "setup_type": "api_key",
            "config_fields": [
                {
                    "name": "WEATHER_API_KEY",
                    "required": True,
                },
            ],
        },
    ]

    with (
        patch("mindroom.api.tools._read_tools_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
        patch("mindroom.api.tools.load_scoped_credentials") as mock_load_scoped_credentials,
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    body = response.json()
    assert body["status_authoritative"] is False
    tool = body["tools"][0]
    assert tool["name"] == "weather"
    assert tool["status"] == "requires_config"
    assert tool["dashboard_configuration_supported"] is False
    mock_load_scoped_credentials.assert_not_called()


def test_get_tools_uses_one_runtime_snapshot(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """The tools route should not mix one old config read with a newer runtime."""
    from mindroom.api import tools as tools_api  # noqa: PLC0415

    first_runtime = main._app_runtime_paths(main.app)
    second_runtime = _runtime_paths(tmp_path / "second-runtime")
    second_runtime.config_path.parent.mkdir(parents=True, exist_ok=True)
    second_runtime.config_path.write_text(
        yaml.safe_dump(_authored_config_payload("new_agent")),
        encoding="utf-8",
    )
    captured_runtime_paths: list[constants.RuntimePaths] = []

    def _read_tools_runtime_config(_request: object) -> tuple[Config, constants.RuntimePaths]:
        main.initialize_api_app(main.app, second_runtime)
        config_lifecycle.load_config_into_app(second_runtime, main.app)
        return _config_with_worker_scope("shared"), first_runtime

    def _resolve_tool_availability_context(
        _request: object,
        *,
        runtime_paths: constants.RuntimePaths,
        config: Config,
        agent_name: str | None,
        execution_scope_override_provided: bool,
        execution_scope_override: str | None,
    ) -> tools_api._ResolvedToolAvailabilityContext:
        _ = (config, agent_name, execution_scope_override_provided, execution_scope_override)
        captured_runtime_paths.append(runtime_paths)
        return tools_api._ResolvedToolAvailabilityContext(
            execution_scope=None,
            dashboard_configuration_supported=True,
            status_authoritative=True,
            credentials_manager=MagicMock(),
            worker_target=None,
            allowed_shared_services=None,
            auth_provider_credential_services={},
            oauth_providers={},
            runtime_paths=runtime_paths,
        )

    with (
        patch("mindroom.api.tools._read_tools_runtime_config", side_effect=_read_tools_runtime_config),
        patch(
            "mindroom.api.tools.resolved_tool_metadata_for_runtime",
            side_effect=lambda runtime_paths, _config, **_kwargs: (captured_runtime_paths.append(runtime_paths), {})[1],
        ),
        patch(
            "mindroom.api.tools.export_tools_metadata",
            return_value=[
                {
                    "name": "calculator",
                    "display_name": "Calculator",
                    "description": "Calc",
                    "category": "utility",
                    "status": "available",
                    "setup_type": "none",
                    "config_fields": None,
                },
            ],
        ),
        patch(
            "mindroom.api.tools._resolve_tool_availability_context",
            side_effect=_resolve_tool_availability_context,
        ),
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    assert captured_runtime_paths == [first_runtime, first_runtime]


def test_homeassistant_connect_oauth_uses_pending_oauth_state(api_key_client: TestClient) -> None:
    """Home Assistant connect should use state instead of encoding agent_name in the callback URL."""
    config = _config_with_worker_scope("shared")
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200
    _publish_committed_runtime_config(
        api_key_client.app,
        main._app_runtime_paths(api_key_client.app),
        config.model_dump(),
    )
    response = api_key_client.post(
        "/api/homeassistant/connect/oauth?agent_name=general",
        json={
            "instance_url": "homeassistant.local:8123",
            "client_id": "client-id",
        },
    )

    assert response.status_code == 200
    auth_url = response.json()["auth_url"]
    parsed = urlparse(auth_url)
    params = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "http://homeassistant.local:8123/auth/authorize"
    assert params["state"][0]
    assert params["state"][0] != "general"
    assert "agent_name=general" not in params["redirect_uri"][0]


def test_homeassistant_oauth_callback_uses_pending_payload_not_live_credentials(
    api_key_client: TestClient,
) -> None:
    """Home Assistant OAuth should save only the final token payload, not temp callback state."""
    config = _config_with_worker_scope("shared")
    target = MagicMock()
    target.target_manager = MagicMock()
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200

    token_response = MagicMock()
    token_response.status_code = 200
    token_response.json.return_value = {
        "access_token": "ha-access",
        "refresh_token": "ha-refresh",
        "expires_in": 3600,
    }
    async_client = MagicMock()
    async_client.__aenter__.return_value.post.return_value = token_response
    _publish_committed_runtime_config(
        api_key_client.app,
        main._app_runtime_paths(api_key_client.app),
        config.model_dump(),
    )

    with (
        patch("mindroom.api.homeassistant_integration.resolve_request_credentials_target", return_value=target),
        patch("mindroom.api.homeassistant_integration.httpx.AsyncClient", return_value=async_client),
    ):
        connect_response = api_key_client.post(
            "/api/homeassistant/connect/oauth?agent_name=general",
            json={
                "instance_url": "homeassistant.local:8123",
                "client_id": "client-id",
            },
        )
        assert connect_response.status_code == 200
        state = parse_qs(urlparse(connect_response.json()["auth_url"]).query)["state"][0]

        callback_response = api_key_client.get(
            f"/api/homeassistant/callback?code=test-code&state={state}",
            follow_redirects=False,
        )

    assert callback_response.status_code in {302, 307}
    async_client.__aenter__.return_value.post.assert_called_once_with(
        "http://homeassistant.local:8123/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": "test-code",
            "client_id": "client-id",
        },
        timeout=10.0,
    )
    target.target_manager.save_credentials.assert_called_once_with(
        "homeassistant",
        {
            "instance_url": "http://homeassistant.local:8123",
            "client_id": "client-id",
            "access_token": "ha-access",
            "refresh_token": "ha-refresh",
            "expires_in": 3600,
            "_source": "ui",
        },
    )


def test_homeassistant_connect_rejects_draft_execution_scope_override(
    api_key_client: TestClient,
) -> None:
    """Home Assistant connect must reject draft-only execution-scope overrides."""
    config = _config_with_worker_scope("user")
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200
    with patch(
        "mindroom.api.config_lifecycle.read_committed_runtime_config",
        return_value=(config, main._app_runtime_paths(api_key_client.app)),
    ):
        connect_response = api_key_client.post(
            "/api/homeassistant/connect/oauth?agent_name=general&execution_scope=shared",
            json={
                "instance_url": "homeassistant.local:8123",
                "client_id": "client-id",
            },
        )
    assert connect_response.status_code == 409
    assert "Save the configuration before managing credentials" in connect_response.json()["detail"]
    assert "execution_scope=shared" in connect_response.json()["detail"]


def test_spotify_connect_uses_pending_oauth_state(
    api_key_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spotify connect should issue an opaque server-bound state token."""
    config = _config_with_worker_scope("shared")
    issued_state: dict[str, str] = {}

    class _FakeSpotifyOAuth:
        def get_authorize_url(self, state: str | None = None) -> str:
            issued_state["state"] = state or ""
            return "https://accounts.spotify.test/authorize"

    main.initialize_api_app(
        main.app,
        constants.resolve_primary_runtime_paths(
            config_path=main._app_runtime_paths(main.app).config_path,
            storage_path=main._app_runtime_paths(main.app).storage_root,
            process_env={
                **dict(main._app_runtime_paths(main.app).process_env),
                "SPOTIFY_CLIENT_ID": "client-id",
                "SPOTIFY_CLIENT_SECRET": "client-secret",
            },
        ),
    )
    main._app_context(main.app).auth_state = auth.ApiAuthState(
        runtime_paths=main._app_runtime_paths(main.app),
        settings=auth.ApiAuthSettings(
            platform_login_url=None,
            supabase_url=None,
            supabase_anon_key=None,
            account_id=None,
            mindroom_api_key="test-key",
        ),
        supabase_auth=None,
    )

    def _spotify_oauth_factory(**_kwargs: object) -> _FakeSpotifyOAuth:
        return _FakeSpotifyOAuth()

    monkeypatch.setattr(
        "mindroom.api.integrations._ensure_spotify_packages",
        lambda _runtime_paths: (object, _spotify_oauth_factory),
    )
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200
    _publish_committed_runtime_config(
        api_key_client.app,
        main._app_runtime_paths(api_key_client.app),
        config.model_dump(),
    )
    response = api_key_client.post("/api/integrations/spotify/connect?agent_name=general")

    assert response.status_code == 200
    assert response.json()["auth_url"] == "https://accounts.spotify.test/authorize"
    assert issued_state["state"]
    assert issued_state["state"] != "general"


def test_spotify_connect_rejects_draft_execution_scope_override(api_key_client: TestClient) -> None:
    """Spotify connect must reject draft-only execution-scope overrides."""
    config = _config_with_worker_scope("user")

    main.initialize_api_app(
        main.app,
        constants.resolve_primary_runtime_paths(
            config_path=main._app_runtime_paths(main.app).config_path,
            storage_path=main._app_runtime_paths(main.app).storage_root,
            process_env={
                **dict(main._app_runtime_paths(main.app).process_env),
                "SPOTIFY_CLIENT_ID": "client-id",
                "SPOTIFY_CLIENT_SECRET": "client-secret",
            },
        ),
    )
    main._app_context(main.app).auth_state = auth.ApiAuthState(
        runtime_paths=main._app_runtime_paths(main.app),
        settings=auth.ApiAuthSettings(
            platform_login_url=None,
            supabase_url=None,
            supabase_anon_key=None,
            account_id=None,
            mindroom_api_key="test-key",
        ),
        supabase_auth=None,
    )
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200
    with patch(
        "mindroom.api.config_lifecycle.read_committed_runtime_config",
        return_value=(config, main._app_runtime_paths(api_key_client.app)),
    ):
        connect_response = api_key_client.post(
            "/api/integrations/spotify/connect?agent_name=general&execution_scope=shared",
        )
    assert connect_response.status_code == 409
    assert "Save the configuration before managing credentials" in connect_response.json()["detail"]
    assert "execution_scope=shared" in connect_response.json()["detail"]


def test_spotify_status_rejects_isolating_worker_scope(test_client: TestClient) -> None:
    """Spotify dashboard status should reject unsupported worker scopes."""
    config = _config_with_worker_scope("user")
    with patch(
        "mindroom.api.config_lifecycle.read_committed_runtime_config",
        return_value=(config, main._app_runtime_paths(test_client.app)),
    ):
        response = test_client.get("/api/integrations/spotify/status?agent_name=general")

    assert response.status_code == 400
    assert "worker_scope=user" in response.json()["detail"]


def test_spotify_callback_preserves_runtime_validation_error(
    api_key_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spotify callback should pass through structured runtime config validation errors."""
    config = _config_with_worker_scope("shared")

    class _FakeSpotifyOAuth:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def get_authorize_url(self, state: str | None = None) -> str:
            return f"https://accounts.spotify.test/authorize?state={state}"

        def get_access_token(self, _code: str) -> dict[str, Any]:
            return {"access_token": "spotify-token"}

    class _FakeSpotify:
        def __init__(self, auth: str) -> None:
            self.auth = auth

        def current_user(self) -> dict[str, str]:
            return {"display_name": "Spotify User"}

    main.initialize_api_app(
        main.app,
        constants.resolve_primary_runtime_paths(
            config_path=main._app_runtime_paths(main.app).config_path,
            storage_path=main._app_runtime_paths(main.app).storage_root,
            process_env={
                **dict(main._app_runtime_paths(main.app).process_env),
                "SPOTIFY_CLIENT_ID": "client-id",
                "SPOTIFY_CLIENT_SECRET": "client-secret",
            },
        ),
    )
    main._app_context(main.app).auth_state = auth.ApiAuthState(
        runtime_paths=main._app_runtime_paths(main.app),
        settings=auth.ApiAuthSettings(
            platform_login_url=None,
            supabase_url=None,
            supabase_anon_key=None,
            account_id=None,
            mindroom_api_key="test-key",
        ),
        supabase_auth=None,
    )
    monkeypatch.setattr(
        "mindroom.api.integrations._ensure_spotify_packages",
        lambda _runtime_paths: (_FakeSpotify, _FakeSpotifyOAuth),
    )
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200

    invalid_detail = [{"loc": ["config"], "msg": "Invalid plugin name: BadName", "type": "value_error"}]
    _publish_committed_runtime_config(
        api_key_client.app,
        main._app_runtime_paths(api_key_client.app),
        config.model_dump(),
    )
    with patch(
        "mindroom.api.config_lifecycle.read_committed_runtime_config",
        side_effect=[
            (config, main._app_runtime_paths(api_key_client.app)),
            HTTPException(status_code=422, detail=invalid_detail),
        ],
    ):
        connect_response = api_key_client.post("/api/integrations/spotify/connect?agent_name=general")
        state = parse_qs(urlparse(connect_response.json()["auth_url"]).query)["state"][0]
        callback_response = api_key_client.get(f"/api/integrations/spotify/callback?code=test-code&state={state}")

    assert callback_response.status_code == 422
    assert callback_response.json()["detail"] == invalid_detail


def test_get_tools_includes_openclaw_compat_metadata(test_client: TestClient) -> None:
    """openclaw_compat should appear as a registered tool in the tools response."""
    response = test_client.get("/api/tools/")
    assert response.status_code == 200

    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert "openclaw_compat" in tools_by_name

    tool = tools_by_name["openclaw_compat"]
    assert tool["category"] == "development"
    assert tool["status"] == "available"
    assert tool["setup_type"] == "none"
    assert tool["helper_text"] is not None
    assert "shell" in tool["helper_text"]
    assert tool["display_name"] == "OpenClaw Compat"


def test_get_rooms(test_client: TestClient) -> None:
    """Test getting all rooms."""
    # Load config first
    test_client.post("/api/config/load")

    response = test_client.get("/api/rooms")
    assert response.status_code == 200

    rooms = response.json()
    assert isinstance(rooms, list)
    assert "test_room" in rooms


def test_save_config(test_client: TestClient, temp_config_file: Path) -> None:
    """Test saving entire configuration."""
    new_config = {
        "memory": {
            "embedder": {
                "provider": "ollama",
                "config": {"model": "nomic-embed-text", "host": "http://localhost:11434"},
            },
        },
        "models": {"default": {"provider": "test", "id": "test-model-2"}},
        "agents": {
            "new_agent": {
                "display_name": "New Agent",
                "role": "New role",
                "tools": [],
                "instructions": [],
                "rooms": ["new_room"],
            },
        },
        "defaults": {},
        "router": {"model": "ollama"},
    }

    response = test_client.put("/api/config/save", json=new_config)
    assert response.status_code == 200

    # Verify file was updated
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert saved_config["models"]["default"]["id"] == "test-model-2"
    assert "new_agent" in saved_config["agents"]
    # The editor-facing authored serializer preserves explicit emptiness
    # instead of materializing model defaults into saved config.
    assert saved_config["defaults"] == {}


def test_save_config_preserves_explicit_compaction_model_null_clear(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Config save/load should preserve explicit null clears for inherited compaction models."""
    new_config = {
        "models": {
            "default": {"provider": "openai", "id": "test-model", "context_window": 48_000},
            "summary": {"provider": "openai", "id": "summary-model", "context_window": 32_000},
        },
        "defaults": {
            "compaction": {
                "enabled": True,
                "model": "summary",
            },
        },
        "agents": {
            "new_agent": {
                "display_name": "New Agent",
                "role": "New role",
                "tools": [],
                "instructions": [],
                "rooms": ["new_room"],
                "compaction": {
                    "model": None,
                },
            },
        },
    }

    save_response = test_client.put("/api/config/save", json=new_config)
    assert save_response.status_code == 200

    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert saved_config["agents"]["new_agent"]["compaction"] == {"model": None}

    load_response = test_client.post("/api/config/load")
    assert load_response.status_code == 200
    loaded_config = load_response.json()
    assert loaded_config["agents"]["new_agent"]["compaction"] == {"model": None}


def test_save_config_rejects_runtime_sensitive_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """API save should validate against the request runtime before writing to disk."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test", "rooms": []}},
            },
        ),
        encoding="utf-8",
    )
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        process_env={"MINDROOM_NAMESPACE": "prod1"},
    )
    main.initialize_api_app(main.app, runtime_paths)

    async def _idle_watch_config(
        stop_event: asyncio.Event,
        _app: FastAPI,
    ) -> None:
        await stop_event.wait()

    async def _idle_worker_cleanup(stop_event: asyncio.Event, _app: FastAPI) -> None:
        await stop_event.wait()

    monkeypatch.setattr(main, "sync_env_to_credentials", lambda runtime_paths: None)  # noqa: ARG005
    monkeypatch.setattr(main, "_watch_config", _idle_watch_config)
    monkeypatch.setattr(main, "_worker_cleanup_loop", _idle_worker_cleanup)

    with TestClient(main.app) as client:
        response = client.put(
            "/api/config/save",
            json={
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test", "rooms": []}},
                "mindroom_user": {"username": "mindroom_assistant_prod1", "display_name": "Owner"},
            },
        )

    assert response.status_code == 422
    saved_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "mindroom_user" not in saved_config


def test_save_config_rejects_plugin_with_invalid_dedicated_hooks_module(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """API save should reject plugin configs whose dedicated hooks module cannot load."""
    plugin_root = temp_config_file.parent / "plugins" / "broken-hooks"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps(
            {
                "name": "broken-hooks",
                "tools_module": "tools.py",
                "hooks_module": "hooks.py",
                "skills": [],
            },
        ),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text("TOOLS_IMPORTED = True\n", encoding="utf-8")
    (plugin_root / "hooks.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    response = test_client.put(
        "/api/config/save",
        json={
            "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
            "router": {"model": "default"},
            "agents": {"assistant": {"display_name": "Assistant", "role": "test", "rooms": []}},
            "plugins": ["./plugins/broken-hooks"],
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["config"]
    assert "hooks.py" in detail[0]["msg"]
    assert detail[0]["type"] == "value_error"


def test_save_config_can_recover_from_invalid_reload(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Full config replacement should recover from an invalid on-disk config."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    temp_config_file.write_text("agents:\n  broken: [\n", encoding="utf-8")
    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is False

    valid_config = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {
            "recovered_agent": {
                "display_name": "Recovered Agent",
                "role": "Recovered role",
                "tools": [],
                "instructions": [],
                "rooms": ["recovery_room"],
            },
        },
    }

    response = test_client.put("/api/config/save", json=valid_config)

    assert response.status_code == 200
    saved_config = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
    assert saved_config["agents"]["recovered_agent"]["display_name"] == "Recovered Agent"
    assert "plugins" not in saved_config
    assert main._app_context(main.app).config_load_result == main.ConfigLoadResult(success=True)


def test_get_raw_config_source_returns_current_invalid_file(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Raw config source should remain readable even when structured load fails."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    invalid_source = "agents:\n  broken: [\n"
    temp_config_file.write_text(invalid_source, encoding="utf-8")

    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is False

    response = test_client.get("/api/config/raw")

    assert response.status_code == 200
    assert response.json() == {"source": invalid_source}


def test_get_raw_config_source_returns_replacement_text_for_non_utf8_invalid_file(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Raw recovery should stay usable even when config.yaml contains unreadable bytes."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    temp_config_file.write_bytes(b"agents:\n  broken: \xff\n")

    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is False

    response = test_client.get("/api/config/raw")

    assert response.status_code == 200
    assert response.json() == {"source": "agents:\n  broken: \ufffd\n"}


def test_save_raw_config_source_can_recover_from_invalid_reload(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Raw config recovery should replace an invalid file and republish the structured cache."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    temp_config_file.write_text("agents:\n  broken: [\n", encoding="utf-8")

    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is False

    valid_source = yaml.safe_dump(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
            "router": {"model": "default"},
            "agents": {
                "recovered_agent": {
                    "display_name": "Recovered Agent",
                    "role": "Recovered role",
                    "tools": [],
                    "instructions": [],
                    "rooms": ["recovery_room"],
                },
            },
        },
        sort_keys=True,
    )

    response = test_client.put("/api/config/raw", json={"source": valid_source})

    assert response.status_code == 200
    assert temp_config_file.read_text(encoding="utf-8") == valid_source
    assert main._app_context(main.app).config_load_result == main.ConfigLoadResult(success=True)

    load_response = test_client.post("/api/config/load")
    assert load_response.status_code == 200
    assert load_response.json()["agents"]["recovered_agent"]["display_name"] == "Recovered Agent"


def test_config_generation_headers_protect_full_and_raw_save_endpoints(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Config load/raw endpoints should expose generations and reject stale full/raw saves."""
    initial_load = test_client.post("/api/config/load")
    assert initial_load.status_code == 200
    initial_generation = int(initial_load.headers[config_lifecycle.CONFIG_GENERATION_HEADER])

    initial_raw = test_client.get("/api/config/raw")
    assert initial_raw.status_code == 200
    assert int(initial_raw.headers[config_lifecycle.CONFIG_GENERATION_HEADER]) == initial_generation

    save_response = test_client.put(
        "/api/config/save",
        headers={config_lifecycle.CONFIG_GENERATION_HEADER: str(initial_generation)},
        json=_authored_config_payload("updated"),
    )
    assert save_response.status_code == 200
    updated_generation = int(save_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER])
    assert updated_generation > initial_generation

    stale_save_response = test_client.put(
        "/api/config/save",
        headers={config_lifecycle.CONFIG_GENERATION_HEADER: str(initial_generation)},
        json=_authored_config_payload("stale"),
    )
    assert stale_save_response.status_code == 409

    replacement_source = yaml.safe_dump(_authored_config_payload("raw_updated"), sort_keys=True)
    raw_save_response = test_client.put(
        "/api/config/raw",
        headers={config_lifecycle.CONFIG_GENERATION_HEADER: str(updated_generation)},
        json={"source": replacement_source},
    )
    assert raw_save_response.status_code == 200
    raw_generation = int(raw_save_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER])
    assert raw_generation > updated_generation
    assert temp_config_file.read_text(encoding="utf-8") == replacement_source

    stale_raw_save_response = test_client.put(
        "/api/config/raw",
        headers={config_lifecycle.CONFIG_GENERATION_HEADER: str(updated_generation)},
        json={"source": replacement_source},
    )
    assert stale_raw_save_response.status_code == 409


def test_first_party_config_writers_advance_generation_before_watcher_reload(
    test_client: TestClient,
) -> None:
    """Command-side config writes should publish a newer generation before the file watcher runs."""
    initial_load = test_client.post("/api/config/load")
    assert initial_load.status_code == 200
    initial_generation = int(initial_load.headers[config_lifecycle.CONFIG_GENERATION_HEADER])

    response = asyncio.run(
        apply_config_change(
            "defaults.markdown",
            False,
            main._app_context(main.app).runtime_paths,
        ),
    )

    assert "Configuration updated successfully" in response

    stale_save_response = test_client.put(
        "/api/config/save",
        headers={config_lifecycle.CONFIG_GENERATION_HEADER: str(initial_generation)},
        json=_authored_config_payload("stale"),
    )

    assert stale_save_response.status_code == 409


def test_validate_raw_config_source_uses_unique_validation_files(tmp_path: Path) -> None:
    """Concurrent raw validation should not let one request read another request's temp file."""
    runtime_paths = _runtime_paths(tmp_path)
    first_source = yaml.safe_dump(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
            "router": {"model": "default"},
            "agents": {"agent_a": {"display_name": "Agent A", "role": "role a", "rooms": []}},
        },
        sort_keys=True,
    )
    second_source = yaml.safe_dump(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
            "router": {"model": "default"},
            "agents": {"agent_b": {"display_name": "Agent B", "role": "role b", "rooms": []}},
        },
        sort_keys=True,
    )
    first_entered = threading.Event()
    second_entered = threading.Event()
    call_lock = threading.Lock()
    call_count = 0
    original_loader = config_lifecycle.load_runtime_config_model
    results: list[tuple[Config, dict[str, Any]] | None] = [None, None]

    def _interleaving_loader(
        validation_runtime_paths: constants.RuntimePaths,
        *,
        tolerate_plugin_load_errors: bool = False,
    ) -> Config:
        nonlocal call_count
        with call_lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_entered.set()
            assert second_entered.wait(timeout=5)
        else:
            assert first_entered.wait(timeout=5)
            second_entered.set()
        return original_loader(
            validation_runtime_paths,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
        )

    def _run_validation(index: int, source: str) -> None:
        results[index] = config_lifecycle._validate_raw_config_source(source, runtime_paths)

    with patch("mindroom.api.config_lifecycle.load_runtime_config_model", side_effect=_interleaving_loader):
        first_thread = threading.Thread(target=_run_validation, args=(0, first_source))
        second_thread = threading.Thread(target=_run_validation, args=(1, second_source))
        first_thread.start()
        second_thread.start()
        first_thread.join(timeout=5)
        second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert results[0] is not None
    assert results[1] is not None
    assert results[0][1]["agents"] == {"agent_a": {"display_name": "Agent A", "role": "role a", "rooms": []}}
    assert results[1][1]["agents"] == {"agent_b": {"display_name": "Agent B", "role": "role b", "rooms": []}}


def test_write_app_committed_config_restores_original_config_before_releasing_lock(tmp_path: Path) -> None:
    """Failed config writes should leave committed config unchanged before the lock exits."""
    main.initialize_api_app(
        main.app,
        constants.resolve_primary_runtime_paths(
            config_path=tmp_path / "config.yaml",
            process_env={"MINDROOM_NAMESPACE": "prod1"},
        ),
    )
    original_config = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {"assistant": {"display_name": "Assistant", "role": "test", "rooms": []}},
    }
    context = main._app_context(main.app)
    context.config_data = yaml.safe_load(yaml.safe_dump(original_config))

    class _AssertingLock:
        def __enter__(self) -> object:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            if exc_type is not None:
                assert context.config_data == original_config
            return False

    app_state = config_lifecycle.require_api_state(main.app)
    original_lock = app_state.config_lock
    app_state.config_lock = _AssertingLock()
    try:
        with pytest.raises(HTTPException) as exc_info:
            config_lifecycle.write_app_committed_config(
                main.app,
                lambda candidate_config: candidate_config.update(
                    {
                        "mindroom_user": {
                            "username": "mindroom_assistant_prod1",
                            "display_name": "Owner",
                        },
                    },
                ),
                error_prefix="Failed to save configuration",
            )
    finally:
        app_state.config_lock = original_lock

    assert exc_info.value.status_code == 422
    assert context.config_data == original_config


def test_write_app_committed_config_returns_422_for_invalid_plugin_manifest_name(tmp_path: Path) -> None:
    """API config writes should surface invalid plugin manifests as user config errors."""
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "BadName", "tools_module": None, "skills": []}),
        encoding="utf-8",
    )
    main.initialize_api_app(
        main.app,
        constants.resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={}),
    )
    context = main._app_context(main.app)
    context.config_data = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
        "plugins": [],
    }

    with pytest.raises(HTTPException) as exc_info:
        config_lifecycle.write_app_committed_config(
            main.app,
            lambda candidate_config: candidate_config.update({"plugins": ["./plugins/bad-name"]}),
            error_prefix="Failed to save configuration",
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail[0]["loc"] == ("config",)
    assert "Invalid plugin name" in str(exc_info.value.detail[0]["msg"])
    assert exc_info.value.detail[0]["type"] == "value_error"


def test_write_app_committed_config_checks_current_config_load_result_after_acquiring_lock(tmp_path: Path) -> None:
    """Config writes should see a reload failure published while they are waiting on the lock."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={},
    )
    main.initialize_api_app(main.app, runtime_paths)
    context = main._app_context(main.app)
    original_config = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {"assistant": {"display_name": "Assistant", "role": "test", "rooms": []}},
    }
    context.config_data = yaml.safe_load(yaml.safe_dump(original_config))
    context.config_load_result = main.ConfigLoadResult(success=True)

    class _SignalingLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()
            self.blocked = threading.Event()
            self.allow_waiter = threading.Event()

        def __enter__(self) -> "_SignalingLock":
            if self._lock.acquire(blocking=False):
                return self
            self.blocked.set()
            self.allow_waiter.wait(timeout=1)
            self._lock.acquire()
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            self._lock.release()
            return False

    signaling_lock = _SignalingLock()
    app_state = config_lifecycle.require_api_state(main.app)
    original_lock = app_state.config_lock
    app_state.config_lock = signaling_lock
    error_detail = [{"loc": ("config",), "msg": "current config is invalid", "type": "value_error"}]
    captured_exception: list[HTTPException] = []

    def _run_write() -> None:
        try:
            config_lifecycle.write_app_committed_config(
                main.app,
                lambda candidate_config: candidate_config["agents"].update(
                    {
                        "new_agent": {
                            "display_name": "New Agent",
                            "role": "test",
                            "rooms": [],
                        },
                    },
                ),
                error_prefix="Failed to save configuration",
            )
        except HTTPException as exc:
            captured_exception.append(exc)

    try:
        with signaling_lock:
            writer_thread = threading.Thread(target=_run_write)
            writer_thread.start()
            assert signaling_lock.blocked.wait(timeout=1)
            context.config_load_result = main.ConfigLoadResult(
                success=False,
                error_status_code=422,
                error_detail=error_detail,
            )
            signaling_lock.allow_waiter.set()

        writer_thread.join(timeout=1)
    finally:
        app_state.config_lock = original_lock

    assert not writer_thread.is_alive()
    assert len(captured_exception) == 1
    assert captured_exception[0].status_code == 422
    assert captured_exception[0].detail == error_detail
    assert context.config_data == original_config


def test_api_config_load_accepts_missing_plugin_path_in_degraded_mode(temp_config_file: Path) -> None:
    """API config loads should mirror runtime degraded mode for missing plugins."""
    temp_config_file.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": ["./plugins/missing"],
            },
        ),
        encoding="utf-8",
    )
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=temp_config_file, process_env={})
    main.initialize_api_app(main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is True
    client = TestClient(main.app)

    response = client.post("/api/config/load")

    assert response.status_code == 200
    assert response.json()["agents"]["assistant"]["display_name"] == "Assistant"
    assert response.json()["plugins"] == [{"path": "./plugins/missing"}]


def test_api_config_load_returns_422_for_malformed_yaml(temp_config_file: Path) -> None:
    """API config loads should surface malformed YAML as a user config error too."""
    temp_config_file.write_text("agents:\n  bad: [\n", encoding="utf-8")
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=temp_config_file, process_env={})
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    client = TestClient(main.app)

    response = client.post("/api/config/load")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["config"]
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]


def test_api_config_load_does_not_serve_stale_cache_after_invalid_reload(temp_config_file: Path) -> None:
    """API config loads should return the latest parse error instead of stale last-known-good data."""
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=temp_config_file, process_env={})
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    client = TestClient(main.app)

    initial_response = client.post("/api/config/load")
    assert initial_response.status_code == 200
    assert "test_agent" in initial_response.json()["agents"]

    temp_config_file.write_text("agents:\n  broken: [\n", encoding="utf-8")

    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is False

    response = client.post("/api/config/load")

    assert response.status_code == 422
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]
    assert "test_agent" in main._app_context(main.app).config_data["agents"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/config/agents",
        "/api/config/teams",
        "/api/config/models",
        "/api/config/room-models",
        "/api/rooms",
    ],
)
def test_api_cached_read_endpoints_refuse_stale_config_after_invalid_reload(
    api_key_client: TestClient,
    temp_config_file: Path,
    path: str,
) -> None:
    """Config-backed API reads should return the current parse error instead of stale cache."""
    runtime_paths = main._app_runtime_paths(api_key_client.app)
    temp_config_file.write_text("agents:\n  broken: [\n", encoding="utf-8")

    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is False

    response = api_key_client.get(path, headers={"Authorization": "Bearer test-key"})

    assert response.status_code == 422
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]


def test_api_cached_write_endpoints_refuse_stale_config_after_invalid_reload(
    api_key_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Config writes should not mutate from stale cache after the current file becomes malformed."""
    runtime_paths = main._app_runtime_paths(api_key_client.app)
    invalid_source = "agents:\n  broken: [\n"
    temp_config_file.write_text(invalid_source, encoding="utf-8")

    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is False

    response = api_key_client.put(
        "/api/config/models/default",
        headers={"Authorization": "Bearer test-key"},
        json={"provider": "openai", "id": "gpt-5.4"},
    )

    assert response.status_code == 422
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]
    assert temp_config_file.read_text(encoding="utf-8") == invalid_source


def test_api_key_raw_endpoints_recover_from_invalid_reload(
    api_key_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Protected raw config endpoints should stay usable after the structured cache becomes invalid."""
    runtime_paths = main._app_runtime_paths(api_key_client.app)
    invalid_source = "agents:\n  broken: [\n"
    temp_config_file.write_text(invalid_source, encoding="utf-8")

    assert config_lifecycle.load_config_into_app(runtime_paths, main.app) is False

    raw_response = api_key_client.get(
        "/api/config/raw",
        headers={"Authorization": "Bearer test-key"},
    )
    assert raw_response.status_code == 200
    assert raw_response.json() == {"source": invalid_source}

    valid_source = yaml.safe_dump(_authored_config_payload("recovered"), sort_keys=True)
    save_response = api_key_client.put(
        "/api/config/raw",
        headers={
            "Authorization": "Bearer test-key",
            config_lifecycle.CONFIG_GENERATION_HEADER: raw_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER],
        },
        json={"source": valid_source},
    )
    assert save_response.status_code == 200

    load_response = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer test-key"},
    )
    assert load_response.status_code == 200
    assert load_response.json()["agents"]["recovered"]["display_name"] == "Recovered"


def test_load_config_into_app_omits_legacy_null_optional_sections(tmp_path: Path) -> None:
    """API config loads should drop legacy null optional sections from authored config data."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-5.4\n"
        "agents: {}\n"
        "teams: null\n"
        "plugins: null\n"
        "router:\n"
        "  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=config_path, process_env={})

    config_lifecycle.load_config_into_app(runtime_paths, main.app)

    config_data = main._app_context(main.app).config_data
    assert "teams" not in config_data
    assert "plugins" not in config_data


def test_write_app_committed_config_omits_legacy_null_optional_sections(tmp_path: Path) -> None:
    """API config writes should drop legacy null optional sections from authored config data."""
    config_path = tmp_path / "config.yaml"
    main.initialize_api_app(
        main.app,
        constants.resolve_primary_runtime_paths(config_path=config_path, process_env={}),
    )
    main._app_context(main.app).config_data = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {},
        "teams": None,
        "plugins": None,
    }

    config_lifecycle.write_app_committed_config(
        main.app,
        lambda _candidate_config: None,
        error_prefix="Failed to save configuration",
    )

    config_data = main._app_context(main.app).config_data
    assert "teams" not in config_data
    assert "plugins" not in config_data


def test_error_handling_agent_not_found(test_client: TestClient) -> None:
    """Test error handling for non-existent agent."""
    test_client.post("/api/config/load")

    # PUT still targets the specific agent ID, but runtime-aware validation now rejects empty payloads.
    response = test_client.put("/api/config/agents/non_existent", json={})
    assert response.status_code == 422

    # DELETE should return 404 for non-existent agent
    response = test_client.delete("/api/config/agents/really_non_existent")
    assert response.status_code == 404


def test_cors_headers(test_client: TestClient) -> None:
    """Test CORS headers are present."""
    # Test with a regular request (CORS headers are added to responses)
    response = test_client.get("/api/health")
    # TestClient doesn't simulate CORS middleware properly
    # In a real browser environment, these headers would be present
    assert response.status_code == 200


def test_frontend_root_serves_index(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Root path should serve the bundled dashboard index when assets are available."""
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

    response = test_client.get("/")
    assert response.status_code == 200
    assert "MindRoom Dashboard" in response.text


def test_frontend_spa_routes_fall_back_to_index(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unknown non-API paths should return index.html for client-side routing."""
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

    response = test_client.get("/agents")
    assert response.status_code == 200
    assert "MindRoom Dashboard" in response.text


def test_frontend_does_not_shadow_unknown_api_routes(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unknown API paths should remain 404 instead of falling back to the SPA."""
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

    response = test_client.get("/api/not-real")
    assert response.status_code == 404


def test_frontend_blocks_path_traversal(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Path traversal attempts must not leak files outside the frontend directory."""
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-leak")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

    # Starlette normalizes bare `..` segments, so percent-encoded traversal
    # is the real attack vector that _resolve_frontend_asset must block.
    for traversal_path in ["assets/..%2F..%2Fsecret.txt", "..%2Fsecret.txt"]:
        response = test_client.get(f"/{traversal_path}")
        assert response.status_code == 404, f"Path traversal not blocked for {traversal_path}"
        assert "do-not-leak" not in response.text


def test_frontend_redirects_to_login_when_api_key_auth_is_enabled(
    api_key_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Protected standalone dashboards should send unauthenticated users to the login page."""
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

    response = api_key_client.get("/", follow_redirects=False)
    assert response.status_code == 307
    location = urlparse(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query) == {"next": ["/"]}


def test_frontend_login_page_renders_for_api_key_auth(api_key_client: TestClient) -> None:
    """Standalone API-key auth should expose a simple login form."""
    response = api_key_client.get("/login?next=/agents")
    assert response.status_code == 200
    assert "Enter the dashboard API key to continue" in response.text
    assert "MINDROOM_API_KEY" in response.text
    assert ".env" in response.text


def test_frontend_login_page_serializes_oauth_next_path_without_html_entities(
    api_key_client: TestClient,
) -> None:
    """Standalone login must preserve OAuth query parameters in the JS redirect target."""
    next_path = "/api/oauth/test_drive/authorize?agent_name=general&execution_scope=user"

    response = api_key_client.get("/login", params={"next": next_path})

    assert response.status_code == 200
    next_path_line = next(line for line in response.text.splitlines() if "const nextPath =" in line)
    next_path_literal = next_path_line.split("=", 1)[1].strip().removesuffix(";")
    assert json.loads(next_path_literal) == next_path
    assert "&amp;execution_scope" not in response.text


def test_frontend_login_propagates_trusted_upstream_auth_misconfiguration(
    api_key_client: TestClient,
) -> None:
    """Trusted-upstream setup errors should stay visible on frontend auth checks."""
    runtime_paths = main._app_runtime_paths(api_key_client.app)
    main._app_context(api_key_client.app).auth_state = auth.ApiAuthState(
        runtime_paths=runtime_paths,
        settings=auth.ApiAuthSettings(
            platform_login_url=None,
            supabase_url=None,
            supabase_anon_key=None,
            account_id=None,
            mindroom_api_key="test-key",
            trusted_upstream=auth.TrustedUpstreamAuthSettings(enabled=True),
        ),
        supabase_auth=None,
    )

    response = api_key_client.get("/login?next=/agents")

    assert response.status_code == 500
    assert "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER" in response.json()["detail"]


def test_api_key_cookie_auth_allows_protected_requests(api_key_client: TestClient) -> None:
    """A valid standalone auth session cookie should work without bearer headers."""
    response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert response.status_code == 200
    assert response.cookies.get("mindroom_api_key") == "test-key"

    response = api_key_client.post("/api/config/load")
    assert response.status_code == 200


def test_frontend_serves_after_api_key_login(
    api_key_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Authenticated standalone users should receive the bundled dashboard."""
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200

    response = api_key_client.get("/")
    assert response.status_code == 200
    assert "MindRoom Dashboard" in response.text


def test_get_teams_empty(test_client: TestClient) -> None:
    """Test getting teams when none exist."""
    test_client.post("/api/config/load")

    response = test_client.get("/api/config/teams")
    assert response.status_code == 200
    teams = response.json()
    assert isinstance(teams, list)
    assert len(teams) == 0


def test_agent_policies_endpoint_uses_backend_policy(test_client: TestClient) -> None:
    """Draft agent policies should come from the backend delegation/private policy."""
    response = test_client.post(
        "/api/config/agent-policies",
        json={
            "defaults": {},
            "agents": {
                "helper": {
                    "display_name": "Helper",
                    "role": "Helps",
                    "tools": [],
                    "instructions": [],
                    "rooms": ["lobby"],
                },
                "leader": {
                    "display_name": "Leader",
                    "role": "Leads",
                    "tools": [],
                    "instructions": [],
                    "rooms": ["lobby"],
                    "delegate_to": ["mind"],
                },
                "mind": {
                    "display_name": "Mind",
                    "role": "Private",
                    "tools": [],
                    "instructions": [],
                    "rooms": ["lobby"],
                    "private": {"per": "user"},
                },
            },
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "agent_policies": {
            "helper": {
                "agent_name": "helper",
                "is_private": False,
                "effective_execution_scope": None,
                "scope_label": "unscoped",
                "scope_source": "unscoped",
                "dashboard_credentials_supported": True,
                "team_eligibility_reason": None,
                "private_knowledge_base_id": None,
                "private_workspace_enabled": False,
                "private_agent_knowledge_enabled": False,
            },
            "leader": {
                "agent_name": "leader",
                "is_private": False,
                "effective_execution_scope": None,
                "scope_label": "unscoped",
                "scope_source": "unscoped",
                "dashboard_credentials_supported": True,
                "team_eligibility_reason": "Delegates to private agent 'mind', so it cannot participate in teams yet.",
                "private_knowledge_base_id": None,
                "private_workspace_enabled": False,
                "private_agent_knowledge_enabled": False,
            },
            "mind": {
                "agent_name": "mind",
                "is_private": True,
                "effective_execution_scope": "user",
                "scope_label": "private.per=user",
                "scope_source": "private.per",
                "dashboard_credentials_supported": False,
                "team_eligibility_reason": "Private agents cannot participate in teams yet.",
                "private_knowledge_base_id": None,
                "private_workspace_enabled": True,
                "private_agent_knowledge_enabled": False,
            },
        },
    }


def test_create_team(test_client: TestClient, temp_config_file: Path) -> None:
    """Test creating a new team."""
    test_client.post("/api/config/load")

    team_data = {
        "display_name": "Test Team",
        "role": "Testing team functionality",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
        "model": "default",
        "mode": "coordinate",
    }

    response = test_client.post("/api/config/teams", json=team_data)
    assert response.status_code == 200

    result = response.json()
    assert "id" in result
    assert result["id"] == "test_team"
    assert result["success"] is True

    # Verify file was updated
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert "teams" in saved_config
    assert "test_team" in saved_config["teams"]
    assert saved_config["teams"]["test_team"]["display_name"] == "Test Team"
    assert saved_config["teams"]["test_team"]["agents"] == ["test_agent"]


def test_get_teams_with_data(test_client: TestClient) -> None:
    """Test getting teams after creating one."""
    test_client.post("/api/config/load")

    # Create a team first
    team_data = {
        "display_name": "Test Team",
        "role": "Testing team functionality",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
        "model": "default",
        "mode": "coordinate",
    }
    test_client.post("/api/config/teams", json=team_data)

    # Now get teams
    response = test_client.get("/api/config/teams")
    assert response.status_code == 200

    teams = response.json()
    assert isinstance(teams, list)
    assert len(teams) == 1

    team = teams[0]
    assert team["id"] == "test_team"
    assert team["display_name"] == "Test Team"
    assert team["agents"] == ["test_agent"]
    assert team["mode"] == "coordinate"


def test_update_team(test_client: TestClient, temp_config_file: Path) -> None:
    """Test updating an existing team."""
    test_client.post("/api/config/load")

    new_agent_data = {
        "display_name": "New Agent",
        "role": "Another test agent",
        "tools": ["calculator"],
        "instructions": ["Test instruction"],
        "rooms": ["test_room"],
    }
    create_agent_response = test_client.post("/api/config/agents", json=new_agent_data)
    assert create_agent_response.status_code == 200

    # Create a team first
    team_data = {
        "display_name": "Test Team",
        "role": "Testing team functionality",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
        "model": "default",
        "mode": "coordinate",
    }
    test_client.post("/api/config/teams", json=team_data)

    # Update the team
    updated_data = {
        "display_name": "Updated Team",
        "role": "Updated role",
        "agents": ["test_agent", "new_agent"],
        "rooms": ["test-room", "new-room"],
        "model": "gpt-4",
        "mode": "collaborate",
    }

    response = test_client.put("/api/config/teams/test_team", json=updated_data)
    assert response.status_code == 200

    # Verify file was updated
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert saved_config["teams"]["test_team"]["display_name"] == "Updated Team"
    assert saved_config["teams"]["test_team"]["agents"] == ["test_agent", "new_agent"]
    assert saved_config["teams"]["test_team"]["mode"] == "collaborate"


def test_delete_team(test_client: TestClient, temp_config_file: Path) -> None:
    """Test deleting a team."""
    test_client.post("/api/config/load")

    # Create a team first
    team_data = {
        "display_name": "Test Team",
        "role": "Testing team functionality",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
    }
    test_client.post("/api/config/teams", json=team_data)

    # Delete the team
    response = test_client.delete("/api/config/teams/test_team")
    assert response.status_code == 200

    # Verify it's deleted from file
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert "teams" not in saved_config or "test_team" not in saved_config.get("teams", {})

    # Verify it's not returned in list
    response = test_client.get("/api/config/teams")
    teams = response.json()
    assert len(teams) == 0


def test_delete_nonexistent_team(test_client: TestClient) -> None:
    """Test deleting a team that doesn't exist."""
    test_client.post("/api/config/load")

    response = test_client.delete("/api/config/teams/nonexistent_team")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_create_team_unique_id(test_client: TestClient) -> None:
    """Test that creating teams with same display name generates unique IDs."""
    test_client.post("/api/config/load")

    team_data = {
        "display_name": "Test Team",
        "role": "First team",
        "agents": ["test_agent"],
        "rooms": ["test-room"],
    }

    # Create first team
    response1 = test_client.post("/api/config/teams", json=team_data)
    assert response1.status_code == 200
    assert response1.json()["id"] == "test_team"

    # Create second team with same display name
    team_data["role"] = "Second team"
    response2 = test_client.post("/api/config/teams", json=team_data)
    assert response2.status_code == 200
    assert response2.json()["id"] == "test_team_1"

    # Create third team with same display name
    team_data["role"] = "Third team"
    response3 = test_client.post("/api/config/teams", json=team_data)
    assert response3.status_code == 200
    assert response3.json()["id"] == "test_team_2"


def test_get_room_models(test_client: TestClient) -> None:
    """Test getting room-specific model overrides."""
    test_client.post("/api/config/load")

    response = test_client.get("/api/config/room-models")
    assert response.status_code == 200
    room_models = response.json()
    assert isinstance(room_models, dict)


def test_update_room_models(test_client: TestClient, temp_config_file: Path) -> None:
    """Test updating room-specific model overrides."""
    test_client.post("/api/config/load")

    room_models = {"lobby": "gpt-4", "tech-room": "claude-3", "general": "default"}

    response = test_client.put("/api/config/room-models", json=room_models)
    assert response.status_code == 200

    # Verify file was updated
    with temp_config_file.open() as f:
        saved_config = yaml.safe_load(f)

    assert "room_models" in saved_config
    assert saved_config["room_models"]["lobby"] == "gpt-4"
    assert saved_config["room_models"]["tech-room"] == "claude-3"

    # Verify we can retrieve the updated room models
    response = test_client.get("/api/config/room-models")
    assert response.status_code == 200
    retrieved_models = response.json()
    assert retrieved_models["lobby"] == "gpt-4"
    assert retrieved_models["tech-room"] == "claude-3"


# ---------------------------------------------------------------------------
# MINDROOM_API_KEY authentication tests
# ---------------------------------------------------------------------------


@pytest.fixture
def api_key_client(temp_config_file: Path) -> TestClient:
    """Create a test client with MINDROOM_API_KEY enabled."""
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=temp_config_file, process_env={})
    main.initialize_api_app(main.app, runtime_paths)
    main._app_context(main.app).auth_state = auth.ApiAuthState(
        runtime_paths=runtime_paths,
        settings=auth.ApiAuthSettings(
            platform_login_url=None,
            supabase_url=None,
            supabase_anon_key=None,
            account_id=None,
            mindroom_api_key="test-key",
        ),
        supabase_auth=None,
    )
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)
    return TestClient(main.app)


def test_api_key_health_stays_open(api_key_client: TestClient) -> None:
    """Health endpoint should remain accessible without auth even when API key is set."""
    response = api_key_client.get("/api/health")
    assert response.status_code == 200


def test_api_key_readiness_stays_open(api_key_client: TestClient) -> None:
    """Readiness endpoint should remain accessible without auth even when API key is set."""
    set_runtime_ready()

    response = api_key_client.get("/api/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    reset_runtime_state()


def test_api_key_valid_key_allows_access(api_key_client: TestClient) -> None:
    """A valid Bearer token should grant access to protected endpoints."""
    response = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 200


def test_protected_read_keeps_auth_time_snapshot_after_runtime_swap(tmp_path: Path) -> None:
    """Protected reads should stay on the auth-time snapshot even if the app swaps before the handler reads."""
    runtime_a = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        storage_path=tmp_path / "first-store",
        process_env={"MINDROOM_API_KEY": "key-a"},
    )
    runtime_b = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        storage_path=tmp_path / "second-store",
        process_env={"MINDROOM_API_KEY": "key-b"},
    )
    payload_a = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "old",
                "rooms": ["old-room"],
            },
        },
    }
    payload_b = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
        "router": {"model": "default"},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "new",
                "rooms": ["new-room"],
            },
        },
    }
    original_read = config_lifecycle.read_committed_config

    def _swap_then_read(request: Request, reader: Callable[[dict[str, Any]], object]) -> object:
        _publish_committed_runtime_config(main.app, runtime_b, payload_b)
        return original_read(request, reader)

    with (
        patch.object(config_lifecycle, "read_committed_config", side_effect=_swap_then_read),
        TestClient(main.app) as client,
    ):
        _publish_committed_runtime_config(main.app, runtime_a, payload_a)
        response = client.get(
            "/api/rooms",
            headers={"Authorization": "Bearer key-a"},
        )

    assert response.status_code == 200
    assert response.json() == ["old-room"]


def test_protected_write_rejects_runtime_swap_after_auth(tmp_path: Path) -> None:
    """Protected writes should fail stale instead of mutating a newer runtime after auth succeeds."""
    runtime_a = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        storage_path=tmp_path / "first-store",
        process_env={"MINDROOM_API_KEY": "key-a"},
    )
    runtime_b = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        storage_path=tmp_path / "second-store",
        process_env={"MINDROOM_API_KEY": "key-b"},
    )
    payload_a = _authored_config_payload("old")
    payload_b = _authored_config_payload("new")
    runtime_b.config_path.write_text(yaml.safe_dump(payload_b), encoding="utf-8")
    original_replace = config_lifecycle.replace_committed_config

    def _swap_then_replace(
        request: Request,
        new_config: dict[str, Any],
        *,
        error_prefix: str,
        expected_generation: int | None = None,
    ) -> int:
        _publish_committed_runtime_config(main.app, runtime_b, payload_b)
        return original_replace(
            request,
            new_config,
            error_prefix=error_prefix,
            expected_generation=expected_generation,
        )

    with (
        patch.object(config_lifecycle, "replace_committed_config", side_effect=_swap_then_replace),
        TestClient(main.app) as client,
    ):
        _publish_committed_runtime_config(main.app, runtime_a, payload_a)
        response = client.put(
            "/api/config/save",
            headers={"Authorization": "Bearer key-a"},
            json=_authored_config_payload("written"),
        )

    assert response.status_code == 409
    assert Config.validate_with_runtime(yaml.safe_load(runtime_b.config_path.read_text(encoding="utf-8")), runtime_b)
    assert yaml.safe_load(runtime_b.config_path.read_text(encoding="utf-8"))["agents"] == payload_b["agents"]


def test_protected_raw_read_keeps_auth_time_snapshot_after_runtime_swap(tmp_path: Path) -> None:
    """Protected raw reads should stay on the auth-time snapshot after a runtime swap."""
    runtime_a = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        storage_path=tmp_path / "first-store",
        process_env={"MINDROOM_API_KEY": "key-a"},
    )
    runtime_b = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        storage_path=tmp_path / "second-store",
        process_env={"MINDROOM_API_KEY": "key-b"},
    )
    payload_a = _authored_config_payload("old")
    payload_b = _authored_config_payload("new")
    source_a = yaml.safe_dump(payload_a, sort_keys=True)
    source_b = yaml.safe_dump(payload_b, sort_keys=True)
    runtime_a.config_path.write_text(source_a, encoding="utf-8")
    runtime_b.config_path.write_text(source_b, encoding="utf-8")
    original_read = config_lifecycle.read_raw_config_source

    def _swap_then_read(request: Request) -> str:
        _publish_committed_runtime_config(main.app, runtime_b, payload_b)
        return original_read(request)

    with (
        patch.object(config_lifecycle, "read_raw_config_source", side_effect=_swap_then_read),
        TestClient(main.app) as client,
    ):
        _publish_committed_runtime_config(main.app, runtime_a, payload_a)
        response = client.get(
            "/api/config/raw",
            headers={"Authorization": "Bearer key-a"},
        )

    assert response.status_code == 200
    assert response.json() == {"source": source_a}


def test_protected_raw_write_rejects_runtime_swap_after_auth(tmp_path: Path) -> None:
    """Protected raw writes should fail stale instead of writing to a newer runtime."""
    runtime_a = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        storage_path=tmp_path / "first-store",
        process_env={"MINDROOM_API_KEY": "key-a"},
    )
    runtime_b = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        storage_path=tmp_path / "second-store",
        process_env={"MINDROOM_API_KEY": "key-b"},
    )
    payload_a = _authored_config_payload("old")
    payload_b = _authored_config_payload("new")
    source_b = yaml.safe_dump(payload_b, sort_keys=True)
    runtime_b.config_path.write_text(source_b, encoding="utf-8")
    original_replace = config_lifecycle.replace_raw_config_source

    def _swap_then_replace(
        request: Request,
        source: str,
        *,
        error_prefix: str,
        expected_generation: int | None = None,
    ) -> int:
        _publish_committed_runtime_config(main.app, runtime_b, payload_b)
        return original_replace(
            request,
            source,
            error_prefix=error_prefix,
            expected_generation=expected_generation,
        )

    with (
        patch.object(config_lifecycle, "replace_raw_config_source", side_effect=_swap_then_replace),
        TestClient(main.app) as client,
    ):
        _publish_committed_runtime_config(main.app, runtime_a, payload_a)
        response = client.put(
            "/api/config/raw",
            headers={"Authorization": "Bearer key-a"},
            json={"source": yaml.safe_dump(_authored_config_payload("written"), sort_keys=True)},
        )

    assert response.status_code == 409
    assert runtime_b.config_path.read_text(encoding="utf-8") == source_b


def test_protected_crud_write_rejects_runtime_swap_after_auth(tmp_path: Path) -> None:
    """Legacy CRUD writes should fail stale instead of mutating a newer runtime after auth succeeds."""
    runtime_a = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        storage_path=tmp_path / "first-store",
        process_env={"MINDROOM_API_KEY": "key-a"},
    )
    runtime_b = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        storage_path=tmp_path / "second-store",
        process_env={"MINDROOM_API_KEY": "key-b"},
    )
    payload_a = _authored_config_payload("old")
    payload_b = _authored_config_payload("new")
    runtime_b.config_path.write_text(yaml.safe_dump(payload_b), encoding="utf-8")
    original_write = config_lifecycle.write_committed_config

    def _swap_then_write(
        request: Request,
        mutate: Callable[[dict[str, Any]], object],
        *,
        error_prefix: str,
    ) -> object:
        _publish_committed_runtime_config(main.app, runtime_b, payload_b)
        return original_write(request, mutate, error_prefix=error_prefix)

    with (
        patch.object(config_lifecycle, "write_committed_config", side_effect=_swap_then_write),
        TestClient(main.app) as client,
    ):
        _publish_committed_runtime_config(main.app, runtime_a, payload_a)
        response = client.put(
            "/api/config/agents/assistant",
            headers={"Authorization": "Bearer key-a"},
            json={
                "display_name": "Assistant",
                "role": "updated",
                "rooms": ["updated-room"],
            },
        )

    assert response.status_code == 409
    assert Config.validate_with_runtime(yaml.safe_load(runtime_b.config_path.read_text(encoding="utf-8")), runtime_b)
    assert yaml.safe_load(runtime_b.config_path.read_text(encoding="utf-8"))["agents"] == payload_b["agents"]


def test_api_key_missing_header_rejects(api_key_client: TestClient) -> None:
    """Missing Authorization header should return 401 when API key is set."""
    response = api_key_client.post("/api/config/load")
    assert response.status_code == 401


def test_api_key_wrong_key_rejects(api_key_client: TestClient) -> None:
    """Wrong Bearer token should return 401."""
    response = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


def test_api_key_protects_teams(api_key_client: TestClient) -> None:
    """Teams endpoint should reject unauthenticated requests when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get("/api/config/teams")
    assert response.status_code == 401


def test_api_key_protects_models(api_key_client: TestClient) -> None:
    """Models endpoint should reject unauthenticated requests when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get("/api/config/models")
    assert response.status_code == 401


def test_api_key_protects_rooms(api_key_client: TestClient) -> None:
    """Rooms endpoint should reject unauthenticated requests when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get("/api/rooms")
    assert response.status_code == 401


def test_api_key_protects_room_models(api_key_client: TestClient) -> None:
    """Room-models endpoint should reject unauthenticated requests when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get("/api/config/room-models")
    assert response.status_code == 401


def test_api_key_authenticated_teams_access(api_key_client: TestClient) -> None:
    """Teams endpoint should work with valid auth when API key is set."""
    api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"})
    response = api_key_client.get(
        "/api/config/teams",
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 200


def _trusted_auth_test_app(runtime_paths: constants.RuntimePaths) -> FastAPI:
    """Create a minimal app that returns the authenticated API user."""
    api_app = FastAPI()
    main.initialize_api_app(api_app, runtime_paths)

    @api_app.get("/whoami")
    async def _whoami(auth_user: Annotated[dict[str, Any], Depends(auth.verify_user)]) -> dict[str, Any]:
        return auth_user

    return api_app


def test_trusted_upstream_headers_ignored_when_disabled(tmp_path: Path) -> None:
    """Trusted identity headers must do nothing unless explicitly enabled."""
    api_app = _trusted_auth_test_app(_runtime_paths(tmp_path))

    with TestClient(api_app) as client:
        response = client.get(
            "/whoami",
            headers={
                "X-Trusted-User": "alice",
                "X-Trusted-Email": "alice@example.com",
                "X-Trusted-Matrix-User": "@alice:example.org",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"user_id": "standalone", "email": None}


def test_trusted_upstream_headers_populate_auth_user_when_enabled(tmp_path: Path) -> None:
    """Enabled trusted upstream auth should expose stable user and email fields."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
            "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
        },
    )
    api_app = _trusted_auth_test_app(runtime_paths)

    with TestClient(api_app) as client:
        response = client.get(
            "/whoami",
            headers={
                "X-Trusted-User": "alice",
                "X-Trusted-Email": "alice@example.com",
                "X-Trusted-Matrix-User": "@alice:example.org",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "alice",
        "email": "alice@example.com",
        "matrix_user_id": "@alice:example.org",
        "auth_source": "trusted_upstream",
    }


def test_trusted_upstream_auth_prefers_matrix_header_over_email_template(tmp_path: Path) -> None:
    """A real Matrix identity header should win over a derived email mapping."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
            "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE": "@{localpart}:wrong.example",
        },
    )
    api_app = _trusted_auth_test_app(runtime_paths)

    with TestClient(api_app) as client:
        response = client.get(
            "/whoami",
            headers={
                "X-Trusted-User": "alice",
                "X-Trusted-Email": "alice@example.com",
                "X-Trusted-Matrix-User": "@alice:example.org",
            },
        )

    assert response.status_code == 200
    assert response.json()["matrix_user_id"] == "@alice:example.org"


def test_trusted_upstream_auth_derives_matrix_user_id_from_email_localpart(tmp_path: Path) -> None:
    """Trusted email-only deployments may derive the Matrix identity from a template."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE": "@{localpart}:example.org",
        },
    )
    api_app = _trusted_auth_test_app(runtime_paths)

    with TestClient(api_app) as client:
        response = client.get(
            "/whoami",
            headers={
                "X-Trusted-User": "alice",
                "X-Trusted-Email": "alice@example.com",
            },
        )

    assert response.status_code == 200
    assert response.json()["matrix_user_id"] == "@alice:example.org"


def test_trusted_upstream_auth_email_template_requires_email_header_config(tmp_path: Path) -> None:
    """Email-to-Matrix derivation should fail clearly when the email header is not configured."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE": "@{localpart}:example.org",
        },
    )
    api_app = _trusted_auth_test_app(runtime_paths)

    with TestClient(api_app) as client:
        response = client.get(
            "/whoami",
            headers={"X-Trusted-User": "alice"},
        )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "Trusted upstream email-to-Matrix template is set but MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER is not set"
    )


@pytest.mark.parametrize(
    "template",
    ["@alice:example.org", "@{localpart}-{localpart}:example.org"],
)
def test_trusted_upstream_auth_email_template_requires_exactly_one_localpart_placeholder(
    tmp_path: Path,
    template: str,
) -> None:
    """Trusted auth should reject constant or ambiguous email-to-Matrix templates."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
            "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE": template,
        },
    )
    api_app = _trusted_auth_test_app(runtime_paths)

    with TestClient(api_app) as client:
        response = client.get(
            "/whoami",
            headers={
                "X-Trusted-User": "alice",
                "X-Trusted-Email": "alice@example.com",
                "X-Trusted-Matrix-User": "@alice:example.org",
            },
        )

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "Trusted upstream email-to-Matrix template must contain exactly one {localpart} placeholder"
    )


def test_trusted_upstream_auth_rejects_invalid_derived_matrix_user_id(tmp_path: Path) -> None:
    """Derived Matrix IDs must pass the same Matrix parser as explicit headers."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE": "@{localpart}:example.org.",
        },
    )
    api_app = _trusted_auth_test_app(runtime_paths)

    with TestClient(api_app) as client:
        response = client.get(
            "/whoami",
            headers={
                "X-Trusted-User": "alice",
                "X-Trusted-Email": "alice@example.com",
            },
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid trusted upstream Matrix user id"


@pytest.mark.parametrize("matrix_user_id", ["@Alice:example.org", "@:example.org"])
def test_trusted_upstream_auth_accepts_historical_matrix_user_id(tmp_path: Path, matrix_user_id: str) -> None:
    """Trusted Matrix identities may use historical Matrix localparts."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
        },
    )
    api_app = _trusted_auth_test_app(runtime_paths)

    with TestClient(api_app) as client:
        response = client.get(
            "/whoami",
            headers={
                "X-Trusted-User": "alice",
                "X-Trusted-Matrix-User": matrix_user_id,
            },
        )

    assert response.status_code == 200
    assert response.json()["matrix_user_id"] == matrix_user_id


def test_trusted_upstream_auth_requires_configured_user_header(tmp_path: Path) -> None:
    """A trusted upstream deployment must fail closed when the user id header is absent."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
        },
    )
    api_app = _trusted_auth_test_app(runtime_paths)

    with TestClient(api_app) as client:
        response = client.get("/whoami")

    assert response.status_code == 401
    assert "trusted upstream identity header" in response.json()["detail"]


@pytest.mark.parametrize(
    "matrix_user_id",
    [
        "@alice:example.org extra",
        "@alice:",
        "@alice:[::::]",
        "@alice:example.org.",
    ],
)
def test_trusted_upstream_auth_rejects_invalid_matrix_user_id(tmp_path: Path, matrix_user_id: str) -> None:
    """Trusted upstream Matrix IDs must still be concrete Matrix user IDs."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
            "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
            "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
        },
    )
    api_app = _trusted_auth_test_app(runtime_paths)

    with TestClient(api_app) as client:
        response = client.get(
            "/whoami",
            headers={
                "X-Trusted-User": "alice",
                "X-Trusted-Matrix-User": matrix_user_id,
            },
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid trusted upstream Matrix user id"


@pytest.mark.parametrize(
    "path",
    [
        "/api/homeassistant/callback?code=test-code&state=missing",
        "/api/integrations/spotify/callback?code=test-code&state=missing",
        "/api/oauth/google_drive/callback?code=test-code&state=missing",
    ],
)
def test_api_key_keeps_oauth_callbacks_open(
    api_key_client: TestClient,
    path: str,
) -> None:
    """Legacy public callbacks stay open, while generic OAuth callbacks require auth."""
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200

    response = api_key_client.get(path)
    assert response.status_code == 400
    assert "OAuth state is invalid or expired" in response.json()["detail"]


def _set_platform_auth(
    *,
    valid_tokens: set[str],
    platform_login_url: str = "https://platform.example.com/login",
    account_id: str | None = None,
    user_id: str = "user-123",
) -> None:
    """Configure the API module for platform-managed cookie auth tests."""

    class _FakeUser:
        id = user_id
        email = "user@example.com"

    class _FakeResponse:
        user = _FakeUser()

    class _FakeAuth:
        @staticmethod
        def get_user(token: str) -> _FakeResponse | None:
            if token not in valid_tokens:
                return None
            return _FakeResponse()

    class _FakeClient:
        auth = _FakeAuth()

    main._app_context(main.app).auth_state = auth.ApiAuthState(
        runtime_paths=main._app_runtime_paths(main.app),
        settings=auth.ApiAuthSettings(
            platform_login_url=platform_login_url,
            supabase_url="https://supabase.example.com",
            supabase_anon_key="anon-key",
            account_id=account_id,
            mindroom_api_key=None,
        ),
        supabase_auth=_FakeClient(),
    )


def test_supabase_cookie_auth_allows_access(
    test_client: TestClient,
) -> None:
    """Platform requests should authenticate from the mindroom_jwt cookie."""
    valid_cookie_token = "valid-cookie-token"  # noqa: S105
    _set_platform_auth(valid_tokens={valid_cookie_token})

    response = test_client.post(
        "/api/config/load",
        cookies={"mindroom_jwt": valid_cookie_token},
    )
    assert response.status_code == 200


def test_platform_frontend_redirects_to_login_when_cookie_missing(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Platform deployments should redirect unauthenticated dashboard requests to the platform login."""
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)
    _set_platform_auth(
        valid_tokens=set(),
        platform_login_url="https://app.example.com/auth/login",
    )

    response = test_client.get("/agents", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://app.example.com/auth/login?redirect_to=")


def test_platform_frontend_redirects_to_login_when_cookie_invalid(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invalid platform cookies must redirect to login instead of serving the SPA shell."""
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)
    _set_platform_auth(
        valid_tokens={"valid-cookie-token"},
        platform_login_url="https://app.example.com/auth/login",
    )

    response = test_client.get(
        "/agents",
        cookies={"mindroom_jwt": "definitely-invalid"},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://app.example.com/auth/login?redirect_to=")


def test_platform_frontend_serves_dashboard_with_valid_cookie(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Valid platform cookies should grant access to the bundled dashboard."""
    valid_cookie_token = "valid-cookie-token"  # noqa: S105
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)
    _set_platform_auth(
        valid_tokens={valid_cookie_token},
        platform_login_url="https://app.example.com/auth/login",
    )

    response = test_client.get(
        "/",
        cookies={"mindroom_jwt": valid_cookie_token},
    )
    assert response.status_code == 200
    assert "MindRoom Dashboard" in response.text


def test_platform_frontend_redirects_when_cookie_account_mismatches(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Platform frontend access must enforce the instance account id."""
    valid_cookie_token = "valid-cookie-token"  # noqa: S105
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")

    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)
    _set_platform_auth(
        valid_tokens={valid_cookie_token},
        platform_login_url="https://app.example.com/auth/login",
        account_id="account-owner",
        user_id="other-account",
    )

    response = test_client.get(
        "/",
        cookies={"mindroom_jwt": valid_cookie_token},
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://app.example.com/auth/login?redirect_to=")


def test_health_startup_grace_expires_after_stale_threshold(
    test_client: TestClient,
) -> None:
    """Entity without first SyncResponse becomes stale after startup grace expires."""
    from mindroom.matrix.health import _matrix_sync_state  # noqa: PLC0415

    reset_matrix_sync_health()
    reset_runtime_state()
    mark_matrix_sync_loop_started("router")
    set_runtime_ready()

    # Immediately after start, should be healthy (in grace period)
    response = test_client.get("/api/health")
    assert response.status_code == 200

    # Now simulate 601 seconds passing — beyond the 600s startup grace
    state = _matrix_sync_state["router"]
    state.loop_started_time = datetime.now(UTC) - timedelta(seconds=601)

    # Should now be stale — startup grace expired without first sync
    response = test_client.get("/api/health")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert "router" in data.get("stale_sync_entities", [])

    reset_matrix_sync_health()
    reset_runtime_state()


def test_health_repeated_restarts_do_not_extend_first_sync_grace(test_client: TestClient) -> None:
    """Repeated restarts without a successful sync must still go unhealthy."""
    from mindroom.matrix.health import _matrix_sync_state  # noqa: PLC0415

    reset_matrix_sync_health()
    reset_runtime_state()
    mark_matrix_sync_loop_started("router")
    set_runtime_ready()

    first_start_time = datetime.now(UTC) - timedelta(seconds=601)
    _matrix_sync_state["router"].loop_started_time = first_start_time

    for _ in range(3):
        mark_matrix_sync_loop_started("router")

    response = test_client.get("/api/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "last_sync_time": None,
        "stale_sync_entities": ["router"],
    }
    assert _matrix_sync_state["router"].loop_started_time == first_start_time

    reset_matrix_sync_health()
    reset_runtime_state()
