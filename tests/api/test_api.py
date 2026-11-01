"""Tests for the dashboard backend API endpoints."""

import asyncio
import copy
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, NoReturn, cast
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import jwt
import pytest
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mindroom import constants, frontend_assets
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.api import auth, config_lifecycle, frontend, homeassistant_integration, main
from mindroom.api import sandbox_runner as sandbox_runner_api
from mindroom.api import tools as tools_api
from mindroom.api import workers as workers_api
from mindroom.api.auth_user import AuthSource, AuthUser
from mindroom.api.credentials_target import RequestCredentialsTarget
from mindroom.commands.config_commands import apply_config_change
from mindroom.config.load_attempt import ConfigLoad
from mindroom.config.main import Config, dashboard_config_schema, iter_config_validation_messages
from mindroom.credentials.manager import (
    get_runtime_credentials_manager,
    save_scoped_credentials,
)
from mindroom.custom_tools.homeassistant import HomeAssistantTools
from mindroom.embeddings.embedder_health import capture_embedder_health_recorder
from mindroom.knowledge_graph.store import record_knowledge_graph_failure
from mindroom.matrix.decrypt_failure import e2ee_stats
from mindroom.matrix.health import mark_matrix_sync_loop_started, mark_matrix_sync_success, reset_matrix_sync_health
from mindroom.matrix.state import MatrixState
from mindroom.oauth.credential_lifecycle import resolve_oauth_credential_context
from mindroom.oauth.credential_store import oauth_credential_transaction
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.google import google_oauth_provider
from mindroom.personal_agents import _append_personal_agent_roster_entry
from mindroom.runtime_state import reset_runtime_state, set_runtime_ready, set_runtime_starting
from mindroom.tool_system.plugins import ConfigPlugins, config_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key, resolve_worker_target
from mindroom.workers.backend import WorkerBackend
from mindroom.workers.in_flight import worker_call_in_flight
from mindroom.workers.models import WorkerHandle, WorkerMaintenanceResult
from tests.api.conftest import (
    PLATFORM_SSO_SECRET,
    login_with_api_key,
    publish_api_payload,
    set_platform_auth,
    trusted_upstream_env,
    trusted_upstream_headers,
    trusted_upstream_jwks,
    trusted_upstream_jwt,
    trusted_upstream_jwt_key,
    use_trusted_upstream_runtime,
)
from tests.matrix_state_helpers import save_matrix_state
from tests.oauth_test_utils import publish_oauth_credentials
from tests.runtime_fakes import bind_test_embedded_runtime
from tests.test_personal_agents_config import _entry as personal_agent_entry

TEST_WORKER_AUTH = "token"


def test_worker_api_modules_share_response_dtos_and_serializer() -> None:
    """Primary and sandbox worker APIs should share the worker response contract."""
    assert workers_api.SandboxWorkerListResponse is sandbox_runner_api.SandboxWorkerListResponse
    assert workers_api.SandboxWorkerCleanupResponse is sandbox_runner_api.SandboxWorkerCleanupResponse
    assert workers_api.serialize_sandbox_worker_response is sandbox_runner_api.serialize_sandbox_worker_response

    serialized_worker = workers_api.serialize_sandbox_worker_response(
        WorkerHandle(
            worker_id="worker-1",
            worker_key="worker-key",
            endpoint="http://worker/api/sandbox-runner/execute",
            auth_token=TEST_WORKER_AUTH,
            status="failed",
            backend_name="kubernetes",
            last_used_at=12.0,
            created_at=1.0,
            last_started_at=2.0,
            startup_count=3,
            failure_count=2,
            failure_reason="startup failed",
            debug_metadata={"namespace": "mindroom-instances"},
        ),
    ).model_dump()

    assert "auth_token" not in serialized_worker
    assert serialized_worker["failure_reason"] == "startup failed"
    assert serialized_worker["debug_metadata"] == {"namespace": "mindroom-instances"}
    assert serialized_worker["last_started_at"] == 2.0


def _runtime_paths(tmp_path: Path, *, process_env: dict[str, str] | None = None) -> constants.RuntimePaths:
    return constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env or {},
    )


def _config_with_worker_scope(
    worker_scope: str | None,
    *,
    allowed_users: list[str] | None = None,
    worker_grantable_credentials: list[str] | None = None,
) -> Config:
    payload: dict[str, Any] = {
        "administrators": ["@owner:example.org"],
        "models": {"default": {"provider": "openai", "id": "gpt-5.6-luna"}},
        "agents": {
            "general": {
                "display_name": "General",
                "role": "test",
                "tools": ["homeassistant"],
                "instructions": ["hi"],
                "rooms": ["lobby"],
                "access": {"users": allowed_users or []},
                "credential_managers": allowed_users or [],
            },
        },
        "defaults": {
            "markdown": True,
            "worker_grantable_credentials": worker_grantable_credentials,
        },
    }
    config = Config.model_validate(payload)
    config.agents["general"].worker_scope = worker_scope
    return config


def _authored_config_payload(agent_name: str) -> dict[str, Any]:
    return {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "agents": {
            agent_name: {
                "display_name": agent_name.title(),
                "role": "valid",
                "rooms": [],
            },
        },
    }


def _publish_committed_runtime_config(
    api_app: FastAPI,
    runtime_paths: constants.RuntimePaths,
    authored_payload: dict[str, Any],
) -> None:
    """Publish one committed config snapshot for request-bound API tests."""
    main.initialize_api_app(api_app, runtime_paths)
    publish_api_payload(api_app, authored_payload)


def test_config_lifecycle_published_snapshot_owns_optional_runtime_fields(tmp_path: Path) -> None:
    """Snapshot publication should preserve, replace, and clear every committed field centrally."""
    first_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "first.yaml",
        process_env={},
    )
    second_runtime = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "second.yaml",
        process_env={},
    )
    runtime_config = Config.validate_with_runtime(_authored_config_payload("old"), first_runtime)
    load_error = ValueError("broken source")
    snapshot = config_lifecycle.ApiSnapshot(
        generation=7,
        runtime_paths=first_runtime,
        runtime_config=runtime_config,
        load_error=load_error,
        source_fingerprint="old-source",
    )

    preserved = config_lifecycle._published_snapshot(snapshot, increment_generation=False)

    assert preserved.generation == 7
    assert preserved.runtime_paths == first_runtime
    assert preserved.runtime_config is runtime_config
    assert preserved.load_error is load_error
    assert preserved.source_fingerprint == "old-source"

    cleared = config_lifecycle._published_snapshot(
        snapshot,
        runtime_paths=second_runtime,
        runtime_config=None,
        load_error=None,
        source_fingerprint=None,
    )

    assert cleared.generation == 8
    assert cleared.runtime_paths == second_runtime
    assert cleared.runtime_config is None
    assert cleared.load_error is None
    assert cleared.source_fingerprint is None


def _supabase_settings(url: str | None, anon_key: str | None) -> auth._ApiAuthSettings:
    return auth._ApiAuthSettings(supabase_url=url, supabase_anon_key=anon_key, account_id=None, mindroom_api_key=None)


def test_init_supabase_auth_returns_none_without_credentials(tmp_path: Path) -> None:
    """Supabase auth should stay disabled when credentials are incomplete."""
    runtime_paths = _runtime_paths(tmp_path)
    assert auth._init_supabase_auth(runtime_paths, _supabase_settings(None, None)) is None
    assert auth._init_supabase_auth(runtime_paths, _supabase_settings("https://supabase.test", None)) is None
    assert auth._init_supabase_auth(runtime_paths, _supabase_settings(None, "anon-key")) is None


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
    monkeypatch.setattr("mindroom.tool_system.dependencies._auto_install_optional_extra", _auto_install)

    with pytest.raises(ImportError, match="MINDROOM_NO_AUTO_INSTALL_TOOLS"):
        auth._init_supabase_auth(runtime_paths, _supabase_settings("https://supabase.test", "anon-key"))

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
    monkeypatch.setattr("mindroom.tool_system.dependencies._auto_install_optional_extra", _auto_install)

    with pytest.raises(ImportError, match=r"mindroom\[supabase\]") as err:
        auth._init_supabase_auth(runtime_paths, _supabase_settings("https://supabase.test", "anon-key"))

    assert install_calls == ["supabase"]
    assert "MINDROOM_NO_AUTO_INSTALL_TOOLS" not in str(err.value)


def test_init_supabase_auth_retries_import_after_auto_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Supabase auth should retry the import once after installing the optional extra."""
    imported_modules: list[str] = []
    install_calls: list[str] = []
    runtime_paths = _runtime_paths(tmp_path)

    class FakeClient:
        pass

    def create_client(url: str, key: str) -> FakeClient:
        assert url == "https://supabase.test"
        assert key == "anon-key"
        return FakeClient()

    def import_module(module_name: str) -> SimpleNamespace:
        imported_modules.append(module_name)
        if len(imported_modules) == 1:
            raise ModuleNotFoundError(module_name)
        return SimpleNamespace(create_client=create_client)

    def auto_install(extra_name: str, _runtime_paths: constants.RuntimePaths) -> bool:
        install_calls.append(extra_name)
        return True

    monkeypatch.setattr(auth.importlib, "import_module", import_module)
    monkeypatch.setattr("mindroom.tool_system.dependencies._auto_install_optional_extra", auto_install)

    supabase_auth = auth._init_supabase_auth(runtime_paths, _supabase_settings("https://supabase.test", "anon-key"))

    assert isinstance(supabase_auth, FakeClient)
    assert imported_modules == ["supabase", "supabase"]
    assert install_calls == ["supabase"]


@pytest.mark.asyncio
async def test_validate_supabase_token_catches_supabase_auth_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
        settings=auth._ApiAuthSettings(
            supabase_url="https://supabase.example.com",
            supabase_anon_key="anon-key",
            account_id=None,
            mindroom_api_key=None,
        ),
        supabase_auth=_FakeClient(),
    )

    assert await auth._validate_supabase_token("header.payload.signature", auth_state) is None
    assert imported_modules == ["supabase_auth.errors"]


@pytest.mark.asyncio
async def test_supabase_bearer_check_skips_malformed_tokens_and_leaves_event_loop_free(tmp_path: Path) -> None:
    """Malformed bearer values never reach Supabase, and a slow Supabase check does not stall other coroutines."""
    checked_tokens: list[str] = []
    loop_stayed_free: list[bool] = []
    other_coroutine_ran = threading.Event()

    class _FakeUser:
        id = "user-123"
        email = "user@example.com"

    class _FakeResponse:
        user = _FakeUser()

    class _FakeAuth:
        @staticmethod
        def get_user(token: str) -> _FakeResponse:
            checked_tokens.append(token)
            loop_stayed_free.append(other_coroutine_ran.wait(timeout=5))
            return _FakeResponse()

    class _FakeClient:
        auth = _FakeAuth()

    runtime_paths = _runtime_paths(tmp_path)
    auth_state = auth.ApiAuthState(
        runtime_paths=runtime_paths,
        settings=auth._ApiAuthSettings(
            supabase_url="https://supabase.example.com",
            supabase_anon_key="anon-key",
            account_id=None,
            mindroom_api_key=None,
        ),
        supabase_auth=_FakeClient(),
    )
    api_app = FastAPI()
    main.initialize_api_app(api_app, runtime_paths)
    config_lifecycle.app_state(api_app).api_auth_state = auth_state
    request = Request({"type": "http", "method": "GET", "path": "/api/config/load", "headers": [], "app": api_app})

    for malformed in ("x", "header.payload", "a.b.c.d", "a.b." + "c" * 16 * 1024):
        with pytest.raises(HTTPException) as rejected:
            await auth.authenticate_user(request, f"Bearer {malformed}")
        assert rejected.value.status_code == 401
    assert checked_tokens == []

    async def mark_loop_free() -> None:
        other_coroutine_ran.set()

    token = "header.payload.signature"  # noqa: S105
    auth_user, _ = await asyncio.gather(auth.authenticate_user(request, f"Bearer {token}"), mark_loop_free())

    assert auth_user == AuthUser(user_id="user-123", email="user@example.com", source=AuthSource.PLATFORM)
    assert checked_tokens == [token]
    assert loop_stayed_free == [True]


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
        if command[1:] == ["run", "build"]:
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
        (["/usr/bin/bun", "run", "build"], frontend_source_dir),
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


def test_exported_api_app_has_initialized_runtime_paths() -> None:
    """The exported module app has runtime paths from import, before `mindroom run` initializes it."""
    assert isinstance(main._app_runtime_paths(main.app), constants.RuntimePaths)


def test_initialize_api_app_initializes_fresh_app_state(tmp_path: Path) -> None:
    """A freshly constructed FastAPI app should get the full MindRoom API state."""
    fresh_app = FastAPI()
    runtime_paths = _runtime_paths(tmp_path)

    main.initialize_api_app(fresh_app, runtime_paths)

    assert main._app_runtime_paths(fresh_app) == runtime_paths
    assert main._app_context(fresh_app).runtime_config is None
    assert hasattr(config_lifecycle.require_api_state(fresh_app).config_lock, "acquire")


def test_app_auth_state_refreshes_after_runtime_swap(tmp_path: Path) -> None:
    """Replacing app runtime paths should invalidate cached auth settings."""
    fresh_app = FastAPI()
    initial_runtime = _runtime_paths(tmp_path, process_env={})
    refreshed_runtime = _runtime_paths(
        tmp_path,
        process_env={"MINDROOM_API_KEY": "updated-key"},
    )

    main.initialize_api_app(fresh_app, initial_runtime)
    assert auth._app_auth_state(fresh_app, main._app_runtime_paths(fresh_app)).settings.mindroom_api_key is None

    main.initialize_api_app(fresh_app, refreshed_runtime)

    assert (
        auth._app_auth_state(fresh_app, main._app_runtime_paths(fresh_app)).settings.mindroom_api_key == "updated-key"
    )


def test_unbinding_embedded_runtime_withdraws_script_capabilities(tmp_path: Path) -> None:
    """An API app keeps no script capabilities or agent CLI registry once its runtime stops lending them."""
    fresh_app = FastAPI()
    main.initialize_api_app(fresh_app, _runtime_paths(tmp_path))
    broker = MagicMock()
    keepalive = MagicMock()
    registry = TurnToolRegistry()
    bind_test_embedded_runtime(
        fresh_app,
        script_broker=broker,
        script_worker_keepalive=keepalive,
        agent_cli_registry=registry,
    )
    state = config_lifecycle.app_state(fresh_app)
    assert fresh_app.state.script_tool_broker is broker
    assert state.runtime.script_worker_keepalive is keepalive
    assert state.agent_cli_registry is registry

    main.unbind_embedded_runtime(fresh_app)

    assert fresh_app.state.script_tool_broker is None
    assert state.agent_cli_registry is None
    assert state.embedded is None


@pytest.mark.parametrize(
    "second_runtime",
    [
        pytest.param(lambda tmp_path: _runtime_paths(tmp_path / "second"), id="config_path"),
        pytest.param(
            lambda tmp_path: _runtime_paths(tmp_path, process_env={"MINDROOM_NAMESPACE": "ns12"}),
            id="runtime",
        ),
    ],
)
def test_initialize_api_app_clears_config_cache_on_runtime_swap(
    tmp_path: Path,
    second_runtime: Callable[[Path], constants.RuntimePaths],
) -> None:
    """Swapping an app to another config path or runtime should drop the previous cached config."""
    first_runtime = _runtime_paths(tmp_path)
    first_runtime.config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
                "agents": {"first": {"display_name": "First", "role": "r", "rooms": ["lobby"]}},
                "defaults": {"markdown": True},
            },
        ),
        encoding="utf-8",
    )
    fresh_app = FastAPI()

    main.initialize_api_app(fresh_app, first_runtime)
    config_lifecycle.load_config_into_app(first_runtime, fresh_app)
    assert set(main._app_context(fresh_app).runtime_config.authored_model_dump()["agents"]) == {"first"}
    assert main._app_context(fresh_app).source_fingerprint is not None

    main.initialize_api_app(fresh_app, second_runtime(tmp_path))

    assert main._app_context(fresh_app).runtime_config is None
    assert main._app_context(fresh_app).source_fingerprint is None


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
    first_runtime.config_path.write_text("agents: {}\n", encoding="utf-8")
    second_runtime.config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
                "router": {"model": "default"},
                "agents": {"second": {"display_name": "Second", "role": "valid", "rooms": []}},
            },
        ),
        encoding="utf-8",
    )
    started = threading.Event()
    allow_finish = threading.Event()
    original_load_attempt = config_lifecycle.load_config_attempt

    def _fake_load_attempt(runtime_paths: constants.RuntimePaths) -> ConfigLoad:
        if runtime_paths == first_runtime:
            started.set()
            allow_finish.wait()
            return ConfigLoad(
                config=None,
                source_fingerprint="stale-old-source",
                source_files=frozenset({first_runtime.config_path}),
                uses_includes=False,
                error=ValueError("invalid old config"),
            )
        return original_load_attempt(runtime_paths)

    main.initialize_api_app(fresh_app, first_runtime)

    with (
        patch.object(config_lifecycle, "load_config_attempt", side_effect=_fake_load_attempt),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        stale_result = executor.submit(config_lifecycle.load_config_into_app, first_runtime, fresh_app)
        try:
            assert started.wait(timeout=1)

            main.initialize_api_app(fresh_app, second_runtime)
            assert config_lifecycle.load_config_into_app(second_runtime, fresh_app).loaded
        finally:
            allow_finish.set()

        assert stale_result.result(timeout=1).superseded

    context = main._app_context(fresh_app)
    assert context.runtime_paths == second_runtime
    assert context.load_error is None
    assert set(context.runtime_config.authored_model_dump()["agents"]) == {"second"}


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
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
                "router": {"model": "default"},
                "agents": {"first": {"display_name": "First", "role": "old", "rooms": []}},
            },
        ),
        encoding="utf-8",
    )
    second_runtime.config_path.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
                "router": {"model": "default"},
                "agents": {"second": {"display_name": "Second", "role": "new", "rooms": []}},
            },
        ),
        encoding="utf-8",
    )

    main.initialize_api_app(fresh_app, first_runtime)
    assert config_lifecycle.load_config_into_app(first_runtime, fresh_app).loaded
    assert set(main._app_context(fresh_app).runtime_config.authored_model_dump()["agents"]) == {"first"}

    main.initialize_api_app(fresh_app, second_runtime)
    assert config_lifecycle.load_config_into_app(second_runtime, fresh_app).loaded
    assert set(main._app_context(fresh_app).runtime_config.authored_model_dump()["agents"]) == {"second"}

    assert not config_lifecycle.load_config_into_app(first_runtime, fresh_app).loaded
    assert set(main._app_context(fresh_app).runtime_config.authored_model_dump()["agents"]) == {"second"}
    assert main._app_context(fresh_app).load_error is None


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

    def touch_live_workers_of_the_runtime(_backend: WorkerBackend) -> None:
        pass

    def _fake_cleanup(
        runtime_paths: constants.RuntimePaths,
        *,
        touch_live_workers: Callable[[WorkerBackend], None],
        computer_worker_keys: frozenset[str] = frozenset(),
    ) -> int:
        assert touch_live_workers is touch_live_workers_of_the_runtime
        assert computer_worker_keys == frozenset()
        cleanup_paths.append(runtime_paths.config_path)
        if len(cleanup_paths) == 1:
            main.initialize_api_app(main.app, second_runtime)
        else:
            stop_event.set()
        return 0

    async def _fake_to_thread(func: Callable[..., int], *args: object, **kwargs: object) -> int:
        return func(*args, **kwargs)

    main.initialize_api_app(main.app, first_runtime)
    monkeypatch.setattr(main, "_worker_cleanup_interval_seconds", lambda _runtime_paths: 0.01)
    monkeypatch.setattr(main, "_cleanup_workers_once", _fake_cleanup)
    monkeypatch.setattr(main.asyncio, "to_thread", _fake_to_thread)

    await main._worker_cleanup_loop(
        stop_event,
        main.app,
        touch_live_workers=touch_live_workers_of_the_runtime,
        idle_poll_interval_seconds=0.01,
    )

    assert cleanup_paths == [first_runtime.config_path, second_runtime.config_path]


def test_worker_cleanup_touches_live_script_workers_before_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active script workers must have their leases refreshed before idle cleanup."""
    actions: list[str] = []
    worker_manager = MagicMock()
    worker_manager.backend_name = "test"
    monkeypatch.setattr(
        main,
        "lease_configured_primary_worker_manager",
        lambda *_args, **_kwargs: nullcontext(worker_manager),
    )
    monkeypatch.setattr(
        workers_api,
        "maintain_workers",
        lambda _backend: actions.append("maintain") or WorkerMaintenanceResult(cleaned=(), reconciled=()),
    )

    main._cleanup_workers_once(
        main._app_runtime_paths(main.app),
        touch_live_workers=lambda backend: actions.append("touch") if backend is worker_manager else None,
    )

    assert actions == ["touch", "maintain"]


def test_worker_cleanup_touches_workers_with_a_call_in_flight_before_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker running a long tool call has its lease refreshed before idle cleanup, however idle it looks."""
    actions: list[str] = []
    worker_manager = MagicMock()
    worker_manager.cleanup_locator = "local:test"
    worker_manager.touch_worker.side_effect = lambda key: actions.append(f"touch {key}")
    monkeypatch.setattr(
        workers_api,
        "maintain_workers",
        lambda _backend: actions.append("maintain") or WorkerMaintenanceResult(cleaned=(), reconciled=()),
    )

    with worker_call_in_flight(worker_manager, "agent:busy"):
        workers_api.maintain_leased_workers(
            worker_manager,
            touch_live_workers=lambda _backend: None,
            computer_worker_keys=frozenset(),
        )
    workers_api.maintain_leased_workers(
        worker_manager,
        touch_live_workers=lambda _backend: None,
        computer_worker_keys=frozenset(),
    )

    assert actions == ["touch agent:busy", "maintain", "maintain"]


def test_health_check(test_client: TestClient) -> None:
    """Test the health check endpoint."""
    reset_matrix_sync_health()
    response = test_client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["last_sync_time"] is None


def test_health_check_reports_recorded_embedder_failure(test_client: TestClient) -> None:
    """A recorded embedder failure appears as an additive block without flipping status."""
    reset_matrix_sync_health()
    capture_embedder_health_recorder().record("embedder authentication failed (HTTP 401)")
    try:
        response = test_client.get("/api/health")
    finally:
        capture_embedder_health_recorder().record(None)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["embedder"] == {
        "status": "failing",
        "detail": "embedder authentication failed (HTTP 401)",
    }


def test_health_check_reports_knowledge_graph_failure(test_client: TestClient) -> None:
    """A failing graph ingestion appears as an additive block without flipping status."""
    reset_matrix_sync_health()
    record_knowledge_graph_failure("ValueError: structured output did not match")
    try:
        response = test_client.get("/api/health")
    finally:
        record_knowledge_graph_failure(None)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["knowledge_graph"] == {"status": "failing", "detail": "ValueError: structured output did not match"}
    assert "knowledge_graph" not in test_client.get("/api/health").json()


def test_health_check_omits_embedder_block_when_healthy(test_client: TestClient) -> None:
    """No embedder block appears while the embedder is healthy."""
    reset_matrix_sync_health()
    capture_embedder_health_recorder().record(None)

    response = test_client.get("/api/health")

    assert response.status_code == 200
    assert "embedder" not in response.json()


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
        "e2ee": e2ee_stats().as_dict(),
        "stale_sync_entities": ["router"],
    }
    reset_matrix_sync_health()
    reset_runtime_state()


def test_health_reports_live_e2ee_counter_values(test_client: TestClient) -> None:
    """The e2ee health field must serialize the real counters, not a stale copy."""
    reset_matrix_sync_health()
    set_runtime_ready()
    before = test_client.get("/api/health").json()["e2ee"]

    e2ee_stats().record_failure("!e2ee-health:localhost", f"$health{uuid4().hex}:localhost")

    after = test_client.get("/api/health").json()["e2ee"]
    assert after["decrypt_failures"] == before["decrypt_failures"] + 1
    assert after.keys() == {"decrypt_failures", "key_requests_sent", "notices_sent"}
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
        "e2ee": e2ee_stats().as_dict(),
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
    monkeypatch.setattr(main, "lease_configured_primary_worker_manager", lambda *_args, **_kwargs: None)

    assert main._cleanup_workers_once(main._app_runtime_paths(main.app), touch_live_workers=lambda _backend: None) == 0


def test_worker_cleanup_once_maintains_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Background cleanup runs the leased manager's maintenance pass and reports how many workers it cleaned."""
    maintained_managers: list[object] = []

    class _FakeWorkerManager:
        backend_name = "kubernetes"

        def maintain_workers(self, *, now: float | None = None) -> WorkerMaintenanceResult:
            assert now is None
            maintained_managers.append(self)
            cleaned = WorkerHandle(
                worker_id="worker-1",
                worker_key="worker-key",
                endpoint="http://worker/api/sandbox-runner/execute",
                auth_token=TEST_WORKER_AUTH,
                status="idle",
                backend_name="kubernetes",
                last_used_at=1.0,
                created_at=0.0,
            )
            return WorkerMaintenanceResult(cleaned=(cleaned,), reconciled=())

    worker_manager = _FakeWorkerManager()
    monkeypatch.setattr(
        main,
        "lease_configured_primary_worker_manager",
        lambda *_args, **_kwargs: nullcontext(worker_manager),
    )

    assert main._cleanup_workers_once(main._app_runtime_paths(main.app), touch_live_workers=lambda _backend: None) == 1
    assert maintained_managers == [worker_manager]


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

    worker_manager = _FakeWorkerManager()
    monkeypatch.setattr(
        workers_api,
        "lease_configured_primary_worker_manager",
        lambda *_args, **_kwargs: nullcontext(worker_manager),
    )

    response = test_client.get("/api/workers")

    assert response.status_code == 200
    assert response.json()["workers"][0]["worker_key"] == "worker-key"
    assert response.json()["workers"][0]["backend_name"] == "kubernetes"


def test_cleanup_workers_endpoint(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """Manual cleanup refreshes live-script workers before applying the idle cutoff, off the event loop."""
    actions: list[tuple[str, object]] = []

    class _FakeWorkerManager:
        idle_timeout_seconds = 60.0

        def cleanup_idle_workers(self, *, now: float | None = None) -> list[WorkerHandle]:
            assert now is None
            actions.append(("cleanup", self))
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

    worker_manager = _FakeWorkerManager()

    class _Lease:
        released = False

        def __enter__(self) -> _FakeWorkerManager:
            return worker_manager

        def __exit__(self, *_args: object) -> None:
            self.released = True

    lease = _Lease()
    monkeypatch.setattr(
        workers_api,
        "lease_configured_primary_worker_manager",
        lambda *_args, **_kwargs: lease,
    )
    api_app = cast("FastAPI", test_client.app)
    bind_test_embedded_runtime(api_app, script_worker_keepalive=lambda manager: actions.append(("touch", manager)))
    on_event_loop = calls_on_event_loop(workers_api, "maintain_workers")

    response = test_client.post("/api/workers/cleanup")

    assert response.status_code == 200
    assert actions == [("touch", worker_manager), ("cleanup", worker_manager)]
    assert on_event_loop == [False]
    assert lease.released is True
    assert response.json()["idle_timeout_seconds"] == 60.0
    assert response.json()["cleaned_workers"][0]["status"] == "idle"


def test_load_config_serves_a_config_whose_authored_form_is_empty(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """A config file that sets nothing is a valid config with nothing authored, not a missing one."""
    temp_config_file.write_text("{}\n", encoding="utf-8")
    assert config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app).loaded

    response = test_client.post("/api/config/load")

    assert response.status_code == 200, response.text
    assert response.json() == {}


def test_load_config(test_client: TestClient) -> None:
    """Test loading configuration."""
    response = test_client.post("/api/config/load")
    assert response.status_code == 200

    config = response.json()
    assert "agents" in config
    assert "models" in config
    assert "test_agent" in config["agents"]


def test_get_personal_agents_lists_active_pending_and_removed_template_entries(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Every roster entry is listed with its account, status, and whether its template still exists.

    Dave is onboarded after the config was loaded, so the loaded config has no agent for him yet.
    Alice's account was registered under an earlier namespace on the real server name, which the listing keeps.
    """
    runtime_paths = main._app_runtime_paths(main.app)
    for entry in (
        personal_agent_entry(),
        personal_agent_entry(
            "@bob:localhost",
            agent_name="mind_bob",
            matrix_username="bob.mind",
            user_display_name="Bob",
            pending=True,
        ),
        personal_agent_entry("@carol:localhost", template="old", agent_name="old_carol"),
    ):
        _append_personal_agent_roster_entry(runtime_paths, entry)
    config_data = yaml.safe_load(temp_config_file.read_text())
    config_data["personal_agents"] = {"mind": {"agent": {"display_name": "{user_display_name}'s Mind"}}}
    temp_config_file.write_text(yaml.dump(config_data))
    assert config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded
    _append_personal_agent_roster_entry(
        runtime_paths,
        personal_agent_entry("@dave:localhost", agent_name="mind_dave", user_display_name="Dave"),
    )
    matrix_state = MatrixState.load(runtime_paths=runtime_paths)
    matrix_state.add_account("agent_mind_alice_smith", "mindroom_mind_alice_smith_old", None, domain="example.org")
    save_matrix_state(matrix_state, runtime_paths)

    response = test_client.get("/api/config/personal-agents")

    assert response.status_code == 200
    assert response.json() == [
        {
            "template": "mind",
            "user_id": "@Alice.Smith:example.org",
            "agent_name": "mind_alice_smith",
            "matrix_user_id": "@mindroom_mind_alice_smith_old:example.org",
            "display_name": "Alice's Mind",
            "pending": False,
            "template_exists": True,
        },
        {
            "template": "mind",
            "user_id": "@bob:localhost",
            "agent_name": "mind_bob",
            "matrix_user_id": "@bob.mind:localhost",
            "display_name": "Bob's Mind",
            "pending": True,
            "template_exists": True,
        },
        {
            "template": "old",
            "user_id": "@carol:localhost",
            "agent_name": "old_carol",
            "matrix_user_id": "@mindroom_old_carol:localhost",
            "display_name": None,
            "pending": False,
            "template_exists": False,
        },
        {
            "template": "mind",
            "user_id": "@dave:localhost",
            "agent_name": "mind_dave",
            "matrix_user_id": "@mindroom_mind_dave:localhost",
            "display_name": "Dave's Mind",
            "pending": False,
            "template_exists": True,
        },
    ]


def test_the_dashboard_list_shows_an_edited_personal_name(test_client: TestClient, temp_config_file: Path) -> None:
    """An owner's display name edit replaces the template's name in the listing."""
    runtime_paths = main._app_runtime_paths(main.app)
    _append_personal_agent_roster_entry(runtime_paths, personal_agent_entry(edits={"display_name": "Scout"}))
    config_data = yaml.safe_load(temp_config_file.read_text())
    config_data["personal_agents"] = {"mind": {"agent": {"display_name": "{user_display_name}'s Mind"}}}
    temp_config_file.write_text(yaml.dump(config_data))
    assert config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded

    response = test_client.get("/api/config/personal-agents")

    assert response.status_code == 200
    assert [agent["display_name"] for agent in response.json()] == ["Scout"]


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

    # Presets and control-plane tools reject defer/initial, so the dashboard hides lazy loading for them.
    lazy_loading = {tool["name"]: tool["lazy_loading_supported"] for tool in data["tools"]}
    assert lazy_loading["calculator"] is True
    assert lazy_loading["dynamic_tools"] is False
    assert lazy_loading["openclaw_compat"] is False

    shell_tool = next(tool for tool in data["tools"] if tool["name"] == "shell")
    assert shell_tool["agent_override_fields"] == [
        {
            "authored_override": True,
            "default": None,
            "description": "Extra env var names or glob patterns exposed to shell execution for this agent only.",
            "env": [],
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
            "env": [],
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


@pytest.mark.asyncio
async def test_non_oauth_auth_provider_uses_required_credential_fields(tmp_path: Path) -> None:
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
        agent_name=None,
        status_authoritative=True,
        credentials_manager=credentials_manager,
        worker_target=None,
        allowed_shared_services=None,
        oauth_providers={},
        runtime_paths=runtime_paths,
        tools=builtin_tool_registry(),
    )
    tools = [
        {
            "name": "custom_shared_api",
            "status": "requires_config",
            "auth_provider": "my_shared_creds",
            "config_fields": [{"name": "api_key", "required": True}],
        },
    ]

    await tools_api._update_tools_statuses(tools, context)

    assert tools[0]["status"] == "available"


def test_get_tools_marks_shared_only_integrations_unsupported_for_isolating_worker_scope(
    test_client: TestClient,
) -> None:
    """Shared-only integrations should stay visible but be marked unsupported."""
    config = _config_with_worker_scope("user")
    runtime_paths = main._app_runtime_paths(main.app)

    with patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert tools_by_name["homeassistant"]["execution_scope_supported"] is False
    assert tools_by_name["spotify"]["execution_scope_supported"] is False
    assert tools_by_name["gmail"]["execution_scope_supported"] is True
    assert tools_by_name["google_calendar"]["execution_scope_supported"] is True
    assert tools_by_name["google_docs"]["execution_scope_supported"] is True
    assert tools_by_name["google_sheets"]["execution_scope_supported"] is True
    assert tools_by_name["google_tasks"]["execution_scope_supported"] is True
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
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
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
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general&execution_scope=unscoped")

    assert response.status_code == 200
    assert response.json()["status_authoritative"] is False
    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert "homeassistant" in tools_by_name
    assert "calculator" in tools_by_name
    assert tools_by_name["homeassistant"]["dashboard_configuration_supported"] is False


def test_get_tools_unknown_agent_previews_draft_scope(test_client: TestClient) -> None:
    """Tool preview should serve unsaved draft agents as a non-authoritative scope preview."""
    config = _config_with_worker_scope("shared")
    runtime_paths = main._app_runtime_paths(main.app)

    with patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)):
        response = test_client.get("/api/tools/?agent_name=draft_agent&execution_scope=user")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status_authoritative"] is False
    tools_by_name = {tool["name"]: tool for tool in payload["tools"]}
    assert "calculator" in tools_by_name
    assert tools_by_name["homeassistant"]["execution_scope_supported"] is False
    assert tools_by_name["calculator"]["execution_scope_supported"] is True


@pytest.mark.parametrize("agent_name", ["draft_agent", "general"], ids=["unknown-agent", "scope-override"])
@pytest.mark.parametrize(("granted", "expected"), [(False, "requires_config"), (True, "available")])
def test_get_tools_draft_scope_preview_reads_what_the_scope_reads(
    test_client: TestClient,
    agent_name: str,
    granted: bool,
    expected: str,
) -> None:
    """A draft scope previews what that scope would read: a global key makes a tool available only when granted."""
    config = _config_with_worker_scope("shared", worker_grantable_credentials=["weather"] if granted else None)
    runtime_paths = main._app_runtime_paths(main.app)
    get_runtime_credentials_manager(runtime_paths).save_credentials(
        "weather",
        {"WEATHER_API_KEY": "k", "_source": "ui"},
    )
    tools = [
        {
            "name": "weather",
            "display_name": "Weather",
            "description": "Weather lookup",
            "category": "information",
            "status": "requires_config",
            "setup_type": "api_key",
            "config_fields": [{"name": "WEATHER_API_KEY", "required": True}],
        },
    ]

    with (
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get(f"/api/tools/?agent_name={agent_name}&execution_scope=user")

    assert response.status_code == 200
    assert response.json()["status_authoritative"] is False
    assert response.json()["tools"][0]["status"] == expected


def test_get_tools_requires_agent_reply_permission_for_agent_scoped_status(test_client: TestClient) -> None:
    """Agent-scoped tool availability should not expose credential-backed state to unauthorized users."""
    runtime_paths = use_trusted_upstream_runtime(main.app)
    config = _config_with_worker_scope(
        "shared",
        allowed_users=["@alice:example.org"],
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
    bob_headers = trusted_upstream_headers(
        user_id="bob",
        email="bob@example.org",
        matrix_user_id="@bob:example.org",
    )

    with (
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
        patch("mindroom.api.tools.load_scoped_credentials") as mock_load_scoped_credentials,
    ):
        response = test_client.get("/api/tools/?agent_name=general", headers=bob_headers)

    assert response.status_code == 403
    mock_load_scoped_credentials.assert_not_called()


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
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
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
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
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
            "instance_url": "http://127.0.0.1:8123",
            "access_token": "ha-token",
            "allow_private_url": True,
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
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
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
        process_env={"MINDROOM_OWNER_USER_ID": "@owner:example.org"},
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "google_drive_oauth_client",
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "_source": "ui",
        },
    )
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
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    tool = response.json()["tools"][0]
    assert tool["name"] == "google_drive"
    assert tool["status"] == "requires_config"

    worker_target = resolve_worker_target("shared", "general", execution_identity=identity)
    credential_context = resolve_oauth_credential_context(
        google_oauth_provider("google_drive"),
        runtime_paths,
        manager,
        worker_target,
    )

    async def publish_oauth_credentials() -> None:
        async with oauth_credential_transaction(credential_context) as transaction:
            await transaction.publish(
                {
                    "token": "drive-token",
                    "refresh_token": "drive-refresh-token",
                    "client_id": "client-id",
                    "scopes": [
                        "openid",
                        "https://www.googleapis.com/auth/userinfo.email",
                        "https://www.googleapis.com/auth/userinfo.profile",
                        "https://www.googleapis.com/auth/drive",
                    ],
                    "_source": "oauth",
                },
                advance_connection_generation=True,
            )
            await transaction.commit()

    asyncio.run(publish_oauth_credentials())
    with (
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        connected_response = test_client.get("/api/tools/?agent_name=general")

    assert connected_response.status_code == 200
    connected_tool = connected_response.json()["tools"][0]
    assert connected_tool["status"] == "available"


def test_get_tools_non_requester_oauth_keeps_non_authoritative_shared_preview(
    test_client: TestClient,
) -> None:
    """Existing OAuth providers must not expose requester stores through non-authoritative status."""
    runtime_paths = use_trusted_upstream_runtime(main.app)
    config = _config_with_worker_scope(
        "user",
        allowed_users=["@alice:example.org"],
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "google_drive_oauth_client",
        {"client_id": "client-id", "client_secret": "client-secret"},
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    oauth_target = resolve_worker_target("user", "general", execution_identity=identity)
    save_scoped_credentials(
        "google_drive_oauth",
        {
            "token": "drive-token",
            "refresh_token": "drive-refresh-token",
            "client_id": "client-id",
            "scopes": [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
                "https://www.googleapis.com/auth/drive",
            ],
            "_source": "oauth",
            "_oauth_provider": "google_drive",
        },
        credentials_manager=manager,
        worker_target=oauth_target,
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
    headers = trusted_upstream_headers(
        user_id="alice",
        email="alice@example.org",
        matrix_user_id="@alice:example.org",
    )

    with (
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general", headers=headers)

    assert response.status_code == 200
    assert response.json()["status_authoritative"] is False
    assert response.json()["tools"][0]["status"] == "requires_config"


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
            "MINDROOM_OWNER_USER_ID": "@owner:example.org",
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
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    tool = response.json()["tools"][0]
    assert tool["name"] == "google_drive"
    assert tool["status"] == "available"


def test_get_tools_does_not_treat_scoped_credentials_as_dashboard_truth(
    test_client: TestClient,
) -> None:
    """Scoped credentials must not flip isolated dashboard status to available."""
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
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
        patch("mindroom.api.tools.load_scoped_credentials", return_value=None) as mock_load_scoped_credentials,
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    body = response.json()
    assert body["status_authoritative"] is False
    tool = body["tools"][0]
    assert tool["name"] == "weather"
    assert tool["status"] == "requires_config"
    assert tool["dashboard_configuration_supported"] is False
    # The preview reads what the scope holds for no requester, never a requester's own store.
    targets = [call.kwargs["worker_target"] for call in mock_load_scoped_credentials.call_args_list]
    assert targets
    assert all(target.execution_identity is None and target.worker_key is None for target in targets)


def test_get_tools_reports_requester_scoped_github_manual_fallback(test_client: TestClient) -> None:
    """GitHub manual auth status should use the authenticated requester's persisted agent target."""
    runtime_paths = use_trusted_upstream_runtime(main.app)
    config = _config_with_worker_scope(
        "user_agent",
        allowed_users=["@alice:example.org", "@bob:example.org"],
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    alice_target = resolve_worker_target("user_agent", "general", execution_identity=alice_identity)
    manual_secret = "github-manual-secret"  # noqa: S105
    save_scoped_credentials(
        "github",
        {"access_token": manual_secret, "base_url": "https://api.github.com"},
        credentials_manager=manager,
        worker_target=alice_target,
        primary_built_tool=True,
    )
    tools = [
        {
            "name": "github",
            "display_name": "GitHub",
            "description": "GitHub access",
            "category": "development",
            "status": "requires_config",
            "setup_type": "oauth",
            "auth_provider": "github",
            "config_fields": [
                {"name": "access_token", "required": False, "env": ["GITHUB_ACCESS_TOKEN"]},
                {"name": "base_url", "required": False, "env": []},
            ],
            "oauth_fallback_fields": ["access_token"],
        },
    ]
    alice_headers = trusted_upstream_headers(
        user_id="alice",
        email="alice@example.org",
        matrix_user_id="@alice:example.org",
    )
    bob_headers = trusted_upstream_headers(
        user_id="bob",
        email="bob@example.org",
        matrix_user_id="@bob:example.org",
    )

    with (
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", side_effect=lambda *_: copy.deepcopy(tools)),
    ):
        alice_response = test_client.get("/api/tools/?agent_name=general", headers=alice_headers)
        bob_response = test_client.get("/api/tools/?agent_name=general", headers=bob_headers)

    assert alice_response.status_code == 200
    assert alice_response.json()["status_authoritative"] is False
    alice_tool = alice_response.json()["tools"][0]
    assert alice_tool["status"] == "available"
    assert alice_tool["manual_auth_configured"] is True
    assert manual_secret not in alice_response.text

    assert bob_response.status_code == 200
    bob_tool = bob_response.json()["tools"][0]
    assert bob_tool["status"] == "requires_config"
    assert bob_tool["manual_auth_configured"] is False


def test_get_tools_reports_requester_scoped_github_oauth_for_unscoped_agent(test_client: TestClient) -> None:
    """An unscoped agent should inspect the authenticated requester's GitHub OAuth store."""
    runtime_paths = use_trusted_upstream_runtime(main.app)
    config = _config_with_worker_scope(
        None,
        allowed_users=["@alice:example.org"],
    )
    manager = get_runtime_credentials_manager(runtime_paths)
    manager.save_credentials(
        "github_oauth_client",
        {"client_id": "github-client-id", "client_secret": "github-client-secret"},
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    oauth_target = resolve_worker_target("user", "general", execution_identity=identity)
    oauth_secret = "github-oauth-secret"  # noqa: S105
    publish_oauth_credentials(
        github_oauth_provider(),
        {
            "token": oauth_secret,
            "refresh_token": "github-refresh-secret",
            "client_id": "github-client-id",
            "scopes": [],
            "expires_at": 4_102_444_800.0,
            "_source": "oauth",
            "_oauth_provider": "github",
        },
        credentials_manager=manager,
        worker_target=oauth_target,
    )
    tools = [
        {
            "name": "github",
            "display_name": "GitHub",
            "description": "GitHub access",
            "category": "development",
            "status": "requires_config",
            "setup_type": "oauth",
            "auth_provider": "github",
            "config_fields": [{"name": "access_token", "required": False, "env": ["GITHUB_ACCESS_TOKEN"]}],
            "oauth_fallback_fields": ["access_token"],
        },
    ]

    with (
        patch("mindroom.api.config_lifecycle.read_committed_runtime_config", return_value=(config, runtime_paths)),
        patch("mindroom.api.tools.export_tools_metadata", return_value=tools),
    ):
        response = test_client.get(
            "/api/tools/?agent_name=general",
            headers=trusted_upstream_headers(
                user_id="alice",
                email="alice@example.org",
                matrix_user_id="@alice:example.org",
            ),
        )

    assert response.status_code == 200
    assert response.json()["tools"][0]["status"] == "available"
    assert oauth_secret not in response.text


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

    def _read_committed_runtime_config(_request: object) -> tuple[Config, constants.RuntimePaths]:
        main.initialize_api_app(main.app, second_runtime)
        config_lifecycle.load_config_into_app(second_runtime, main.app)
        return _config_with_worker_scope("shared"), first_runtime

    def _resolve_tool_availability_context(
        _request: object,
        *,
        runtime_paths: constants.RuntimePaths,
        config: Config,
        plugins: ConfigPlugins,
        agent_name: str | None,
        execution_scope_override_provided: bool,
        execution_scope_override: str | None,
    ) -> tools_api._ResolvedToolAvailabilityContext:
        _ = (config, agent_name, execution_scope_override_provided, execution_scope_override)
        captured_runtime_paths.append(runtime_paths)
        return tools_api._ResolvedToolAvailabilityContext(
            execution_scope=None,
            agent_name=None,
            status_authoritative=True,
            credentials_manager=MagicMock(),
            worker_target=None,
            allowed_shared_services=None,
            oauth_providers={},
            runtime_paths=runtime_paths,
            tools=plugins.tools,
        )

    with (
        patch(
            "mindroom.api.config_lifecycle.read_committed_runtime_config",
            side_effect=_read_committed_runtime_config,
        ),
        patch(
            "mindroom.api.tools.config_plugins",
            side_effect=lambda config, runtime_paths: (
                captured_runtime_paths.append(runtime_paths),
                config_plugins(config, runtime_paths),
            )[1],
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


def _sign_in_with_shared_scope_config(client: TestClient) -> constants.RuntimePaths:
    """Sign `client` in with the API key and publish a config whose `general` agent uses the shared worker scope."""
    runtime_paths = main._app_runtime_paths(client.app)
    login_with_api_key(client)
    _publish_committed_runtime_config(client.app, runtime_paths, _config_with_worker_scope("shared").model_dump())
    return runtime_paths


def _connect_homeassistant_oauth_behind_proxy(
    tmp_path: Path,
    env: dict[str, str],
    *,
    host: str = "mindroom.example.com",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Start Home Assistant OAuth through a TLS-terminating proxy, which forwards plain HTTP with the browser's Host."""
    main.initialize_api_app(main.app, _runtime_paths(tmp_path, process_env=env))
    bind_test_embedded_runtime(main.app)
    return TestClient(main.app, base_url=f"http://{host}").post(
        "/api/homeassistant/connect/oauth",
        headers=headers or {"Authorization": "Bearer test-key"},
        json={"instance_url": "127.0.0.1:8123", "allow_private_url": True},
    )


def test_homeassistant_connect_oauth_identifies_mindroom_by_its_public_origin(tmp_path: Path) -> None:
    """Behind a proxy that terminates TLS, the Client ID and redirect URI share the public origin, not the request's."""
    response = _connect_homeassistant_oauth_behind_proxy(
        tmp_path,
        {"MINDROOM_API_KEY": "test-key", "MINDROOM_PUBLIC_URL": "https://mindroom.example.com/"},
    )

    assert response.status_code == 200, response.text
    params = parse_qs(urlparse(response.json()["auth_url"]).query)
    assert params["client_id"] == ["https://mindroom.example.com"]
    assert params["redirect_uri"] == ["https://mindroom.example.com/api/homeassistant/callback"]


def test_homeassistant_connect_oauth_refuses_an_invalid_public_url(tmp_path: Path) -> None:
    """A `MINDROOM_PUBLIC_URL` without an origin gives Home Assistant no Client ID to accept."""
    response = _connect_homeassistant_oauth_behind_proxy(
        tmp_path,
        {"MINDROOM_API_KEY": "test-key", "MINDROOM_PUBLIC_URL": "mindroom.example.com"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "MINDROOM_PUBLIC_URL is not a valid URL"


def test_homeassistant_connect_oauth_from_the_dashboard_host_returns_there(tmp_path: Path) -> None:
    """A dashboard on its own host gets the callback there, because the public host may send the path to another app."""
    env = {
        **trusted_upstream_env(),
        "MINDROOM_PUBLIC_URL": "https://chat.example.org",
        "MINDROOM_DASHBOARD_URL": "https://admin.example.org",
    }
    response = _connect_homeassistant_oauth_behind_proxy(
        tmp_path,
        env,
        host="admin.example.org",
        headers={**trusted_upstream_headers(), "Origin": "https://admin.example.org"},
    )

    assert response.status_code == 200, response.text
    params = parse_qs(urlparse(response.json()["auth_url"]).query)
    assert params["client_id"] == ["https://admin.example.org"]
    assert params["redirect_uri"] == ["https://admin.example.org/api/homeassistant/callback"]


def test_homeassistant_oauth_callback_uses_pending_payload_not_live_credentials(
    api_key_client: TestClient,
) -> None:
    """Home Assistant OAuth carries its settings in pending state, not the callback URL, and saves only the token payload."""
    runtime_paths = _sign_in_with_shared_scope_config(api_key_client)
    token_response = MagicMock()
    token_response.status_code = 200
    token_response.json.return_value = {
        "access_token": "ha-access",
        "refresh_token": "ha-refresh",
        "expires_in": 3600,
    }
    async_client = MagicMock()
    async_client.__aenter__.return_value.post.return_value = token_response

    with patch("mindroom.api.homeassistant_integration.httpx.AsyncClient", return_value=async_client):
        connect_response = api_key_client.post(
            "/api/homeassistant/connect/oauth?agent_name=general",
            json={
                "instance_url": "127.0.0.1:8123",
                "allow_private_url": True,
            },
        )
        assert connect_response.status_code == 200
        auth_url = urlparse(connect_response.json()["auth_url"])
        params = parse_qs(auth_url.query)

        callback_response = api_key_client.get(
            f"/api/homeassistant/callback?code=test-code&state={params['state'][0]}",
            follow_redirects=False,
        )

    assert f"{auth_url.scheme}://{auth_url.netloc}{auth_url.path}" == "http://127.0.0.1:8123/auth/authorize"
    assert params["state"][0] != "general"
    assert params["client_id"] == ["http://localhost"]
    assert params["redirect_uri"] == ["http://localhost/api/homeassistant/callback"]
    assert callback_response.status_code in {302, 307}
    # Relative, so the browser stays on the origin Home Assistant returned it to.
    assert callback_response.headers["location"] == "/?homeassistant=connected"
    async_client.__aenter__.return_value.post.assert_called_once_with(
        "http://127.0.0.1:8123/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": "test-code",
            "client_id": "http://localhost",
        },
        timeout=10.0,
    )
    assert get_runtime_credentials_manager(runtime_paths).shared_manager().load_credentials("homeassistant") == {
        "instance_url": "http://127.0.0.1:8123",
        "client_id": "http://localhost",
        "access_token": "ha-access",
        "refresh_token": "ha-refresh",
        "expires_in": 3600,
        "allow_private_url": True,
        "_source": "ui",
    }
    assert not list((runtime_paths.storage_root / "workers").rglob("homeassistant_credentials.json"))


def test_homeassistant_shared_scope_token_connect_uses_store_the_toolkit_reads(api_key_client: TestClient) -> None:
    """Shared-scope Home Assistant tokens must stay out of the worker store, where only worker code would see them."""
    runtime_paths = _sign_in_with_shared_scope_config(api_key_client)
    toolkit = HomeAssistantTools(
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=resolve_worker_target("shared", "general", execution_identity=None),
    )

    with patch(
        "mindroom.api.homeassistant_integration._test_connection",
        new_callable=AsyncMock,
        return_value={"version": "2026.9"},
    ):
        connect_response = api_key_client.post(
            "/api/homeassistant/connect/token?agent_name=general",
            json={"instance_url": "http://93.184.216.34:8123", "long_lived_token": "ha-token"},
        )
        status_response = api_key_client.get("/api/homeassistant/status?agent_name=general")
    stored_config = toolkit._load_config()
    assert not list((runtime_paths.storage_root / "workers").rglob("homeassistant_credentials.json"))
    disconnect_response = api_key_client.post("/api/homeassistant/disconnect?agent_name=general")

    assert connect_response.status_code == 200
    assert status_response.json()["connected"] is True
    assert stored_config == {
        "instance_url": "http://93.184.216.34:8123",
        "long_lived_token": "ha-token",
        "allow_private_url": False,
        "_source": "ui",
    }
    assert disconnect_response.status_code == 200
    assert toolkit._load_config() is None


def test_homeassistant_status_reports_stored_settings_without_a_token(api_key_client: TestClient) -> None:
    """Stored settings without a token read as not connected, naming what is missing, without calling the instance."""
    runtime_paths = _sign_in_with_shared_scope_config(api_key_client)
    save_scoped_credentials(
        "homeassistant",
        {"instance_url": "http://93.184.216.34:8123", "_source": "ui"},
        credentials_manager=get_runtime_credentials_manager(runtime_paths),
        worker_target=resolve_worker_target("shared", "general", execution_identity=None),
        primary_built_tool=True,
    )

    with patch("mindroom.api.homeassistant_integration._test_connection", new_callable=AsyncMock) as test_connection:
        response = api_key_client.get("/api/homeassistant/status?agent_name=general")

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert response.json()["has_credentials"] is True
    assert response.json()["error"] == "Missing instance URL or token"
    test_connection.assert_not_awaited()


def test_homeassistant_status_renews_an_expired_oauth_token(api_key_client: TestClient) -> None:
    """The status probe renews a rejected OAuth access token, saves it, and reports the connection."""
    runtime_paths = _sign_in_with_shared_scope_config(api_key_client)
    shared_manager = get_runtime_credentials_manager(runtime_paths).shared_manager()
    oauth_config = {
        "instance_url": "http://93.184.216.34:8123",
        "client_id": "http://dashboard.test",
        "access_token": "expired-token",
        "refresh_token": "ha-refresh",
        "allow_private_url": False,
        "_source": "ui",
    }
    shared_manager.save_credentials("homeassistant", oauth_config)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/token":
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["refresh_token"],
                "refresh_token": ["ha-refresh"],
                "client_id": ["http://dashboard.test"],
            }
            return httpx.Response(200, json={"access_token": "renewed-token", "expires_in": 1800})
        if request.headers["authorization"] != "Bearer renewed-token":
            return httpx.Response(401)
        responses = {"/api/": {"message": "API running"}, "/api/config": {"version": "2026.10"}, "/api/states": []}
        return httpx.Response(200, json=responses[request.url.path])

    with patch(
        "mindroom.api.homeassistant_integration.ServerFetchAsyncHTTPTransport",
        lambda **_kwargs: httpx.MockTransport(handler),
    ):
        status_response = api_key_client.get("/api/homeassistant/status?agent_name=general")

    assert status_response.json()["connected"] is True
    assert status_response.json()["version"] == "2026.10"
    assert shared_manager.load_credentials("homeassistant") == {**oauth_config, "access_token": "renewed-token"}


def test_homeassistant_token_connect_rejects_private_url_without_opt_in(api_key_client: TestClient) -> None:
    """Home Assistant token setup should not probe private URLs unless the user opts in."""
    _sign_in_with_shared_scope_config(api_key_client)

    with patch(
        "mindroom.api.homeassistant_integration._test_connection",
        new_callable=AsyncMock,
    ) as test_connection:
        response = api_key_client.post(
            "/api/homeassistant/connect/token?agent_name=general",
            json={
                "instance_url": "http://127.0.0.1:8123",
                "long_lived_token": "ha-token",
            },
        )

    assert response.status_code == 400
    assert "Private Home Assistant URLs require explicit opt-in" in response.json()["detail"]
    test_connection.assert_not_awaited()


def test_homeassistant_url_validation_maps_malformed_hosts_to_http_error() -> None:
    """Malformed bracketed Home Assistant hosts should return a controlled 400."""
    with pytest.raises(HTTPException) as exc_info:
        homeassistant_integration._validate_homeassistant_instance_url(
            "http://[not-ip]/",
            allow_private_url=False,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Home Assistant instance URL is not allowed for server-side fetching."


def test_homeassistant_token_connect_allows_private_url_with_opt_in(api_key_client: TestClient) -> None:
    """Home Assistant token setup can deliberately allow a self-hosted private URL."""
    _sign_in_with_shared_scope_config(api_key_client)

    with patch(
        "mindroom.api.homeassistant_integration._test_connection",
        new_callable=AsyncMock,
        return_value={"message": "API running"},
    ) as test_connection:
        response = api_key_client.post(
            "/api/homeassistant/connect/token?agent_name=general",
            json={
                "instance_url": "http://127.0.0.1:8123",
                "long_lived_token": "ha-token",
                "allow_private_url": True,
            },
        )

    assert response.status_code == 200
    assert test_connection.await_args.args[0] == {
        "instance_url": "http://127.0.0.1:8123",
        "long_lived_token": "ha-token",
        "allow_private_url": True,
    }


@pytest.mark.asyncio
async def test_homeassistant_connection_failure_does_not_return_response_body() -> None:
    """Failed Home Assistant probes should not expose response bodies in API errors."""
    provider_body = "provider response body contents"
    async_client = MagicMock()
    api_response = MagicMock()
    api_response.status_code = 500
    api_response.text = provider_body
    async_client.__aenter__.return_value.request.return_value = api_response

    with (
        patch("mindroom.api.homeassistant_integration.httpx.AsyncClient", return_value=async_client),
        pytest.raises(HTTPException) as exc_info,
    ):
        await homeassistant_integration._test_connection(
            {"instance_url": "http://93.184.216.34:8123", "long_lived_token": "ha-token"},
            MagicMock(spec=RequestCredentialsTarget),
        )

    assert exc_info.value.status_code == 500
    assert provider_body not in str(exc_info.value.detail)


def test_homeassistant_connect_rejects_draft_execution_scope_override(
    api_key_client: TestClient,
) -> None:
    """Home Assistant connect must reject draft-only execution-scope overrides."""
    config = _config_with_worker_scope("user")
    login_with_api_key(api_key_client)
    with patch(
        "mindroom.api.config_lifecycle.read_committed_runtime_config",
        return_value=(config, main._app_runtime_paths(api_key_client.app)),
    ):
        connect_response = api_key_client.post(
            "/api/homeassistant/connect/oauth?agent_name=general&execution_scope=shared",
            json={"instance_url": "http://93.184.216.34:8123"},
        )
    assert connect_response.status_code == 409
    assert "Save the configuration before managing credentials" in connect_response.json()["detail"]
    assert "execution_scope=shared" in connect_response.json()["detail"]


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


def test_save_config(test_client: TestClient, temp_config_file: Path) -> None:
    """Test saving entire configuration."""
    new_config = {
        "memory": {
            "embedder": {
                "provider": "ollama",
                "config": {"model": "nomic-embed-text", "host": "http://localhost:11434"},
            },
        },
        "models": {
            "default": {"provider": "openai", "id": "test-model-2"},
            "ollama": {"provider": "ollama", "id": "test-model"},
        },
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


def test_save_config_writes_keys_in_alphabetical_order(test_client: TestClient, temp_config_file: Path) -> None:
    """A save writes config.yaml with every mapping's keys sorted, whatever order the draft has them in."""
    agent = {"display_name": "Agent", "role": "Role", "tools": [], "instructions": [], "rooms": []}
    new_config = {
        "router": {"model": "default"},
        "models": {"default": {"provider": "openai", "id": "test-model"}},
        "agents": {"zeta": agent, "alpha": agent},
    }

    response = test_client.put("/api/config/save", json=new_config)
    assert response.status_code == 200, response.text

    saved_config = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
    assert list(saved_config) == sorted(saved_config)
    assert list(saved_config["agents"]) == ["alpha", "zeta"]
    assert list(saved_config["agents"]["alpha"]) == sorted(saved_config["agents"]["alpha"])


def test_save_config_persists_unassigned_configured_rooms(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Dashboard-created rooms should survive save even before responders are assigned."""
    new_config = {
        "models": {"default": {"provider": "openai", "id": "test-model"}},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Helpful assistant",
                "rooms": [],
            },
        },
        "rooms": {
            "project_room": {
                "display_name": "Project Room",
                "description": "Planning space",
            },
        },
        "defaults": {},
        "router": {"model": "default"},
    }

    response = test_client.put("/api/config/save", json=new_config)
    assert response.status_code == 200

    saved_config = yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))
    assert saved_config["rooms"] == {
        "project_room": {
            "display_name": "Project Room",
            "description": "Planning space",
        },
    }

    response = test_client.post("/api/config/load")
    assert response.status_code == 200
    assert "project_room" in response.json()["rooms"]


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


def test_save_config_rejects_runtime_sensitive_invalid_payload(tmp_path: Path) -> None:
    """API save should validate against the request runtime before writing to disk."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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
    matrix_state = MatrixState.load(runtime_paths=runtime_paths)
    matrix_state.add_account("agent_assistant", "mindroom_assistant_prod1", "pw", domain="localhost")
    save_matrix_state(matrix_state, runtime_paths)
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)

    response = TestClient(main.app, base_url="http://localhost").put(
        "/api/config/save",
        json={
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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


def test_save_config_validation_error_omits_submitted_api_keys(test_client: TestClient) -> None:
    """Validation errors must describe the problem without echoing secrets from the payload."""
    model = {"provider": "openai", "id": "model", "api_key": "sk-secret1", "extra_kwargs": {"api_key": "sk-secret2"}}
    response = test_client.put(
        "/api/config/save",
        json={"models": {"default": model}, "router": {"model": "default"}, "agents": {}},
    )

    assert response.status_code == 422
    assert "either api_key or extra_kwargs.api_key" in response.text
    assert "sk-secret" not in response.text


def test_structured_and_raw_saves_report_config_errors_like_every_config_surface(test_client: TestClient) -> None:
    """Both dashboard saves report the shared config error messages, at locations the editors can match."""
    config = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "agents": {"assistant": {"display_name": "Assistant", "role": "test", "rooms": [7], "learnng": False}},
    }
    with pytest.raises(ValidationError) as info:
        Config.model_validate(config)
    messages = [message for _, message in iter_config_validation_messages(info.value)]

    structured = test_client.put("/api/config/save", json=config)
    raw = test_client.put("/api/config/raw", json={"source": yaml.safe_dump(config)})

    expected = [
        {"loc": ["agents", "assistant", "rooms", 0], "msg": messages[0], "type": "value_error"},
        {"loc": ["agents", "assistant", "learnng"], "msg": messages[1], "type": "value_error"},
    ]
    assert (structured.status_code, structured.json()["detail"]) == (422, expected)
    assert (raw.status_code, raw.json()["detail"]) == (422, expected)


def test_save_config_can_recover_from_invalid_reload(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Full config replacement should recover from an invalid on-disk config."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    temp_config_file.write_text("agents:\n  broken: [\n", encoding="utf-8")
    assert not config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded

    valid_config = {
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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
    assert main._app_context(main.app).load_error is None


def test_get_raw_config_source_returns_current_invalid_file(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Raw config source should remain readable even when structured load fails."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    invalid_source = "agents:\n  broken: [\n"
    temp_config_file.write_text(invalid_source, encoding="utf-8")

    assert not config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded

    response = test_client.get("/api/config/raw")

    assert response.status_code == 200
    assert response.json() == {"source": invalid_source, "uses_includes": False}


def test_get_config_schema_returns_annotated_config_schema(test_client: TestClient) -> None:
    """Dashboard editors render forms from the configuration JSON schema."""
    response = test_client.get("/api/config/schema")

    assert response.status_code == 200
    assert response.json() == dashboard_config_schema()


def test_get_raw_config_source_returns_replacement_text_for_non_utf8_invalid_file(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Raw recovery should stay usable even when config.yaml contains unreadable bytes."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    temp_config_file.write_bytes(b"agents:\n  broken: \xff\n")

    assert not config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded

    response = test_client.get("/api/config/raw")

    assert response.status_code == 200
    assert response.json() == {"source": "agents:\n  broken: \ufffd\n", "uses_includes": False}


def test_save_raw_config_source_can_recover_from_invalid_reload(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Raw config recovery should replace an invalid file and republish the structured cache."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    temp_config_file.write_text("agents:\n  broken: [\n", encoding="utf-8")

    assert not config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded

    valid_source = yaml.safe_dump(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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
    assert main._app_context(main.app).load_error is None

    load_response = test_client.post("/api/config/load")
    assert load_response.status_code == 200
    assert load_response.json()["agents"]["recovered_agent"]["display_name"] == "Recovered Agent"


@pytest.mark.parametrize("endpoint", ["save", "raw"])
def test_config_saves_validate_off_the_event_loop(
    test_client: TestClient,
    calls_on_event_loop: Callable[[object, str], list[bool]],
    endpoint: str,
) -> None:
    """Validation loads plugins and can take seconds, so neither save endpoint commits, which validates, on the event loop."""
    config = test_client.post("/api/config/load").json()
    on_event_loop = calls_on_event_loop(config_lifecycle, "commit_dashboard_config")
    if endpoint == "save":
        response = test_client.put("/api/config/save", json=config)
    else:
        response = test_client.put("/api/config/raw", json={"source": yaml.safe_dump(config)})

    assert response.status_code == 200, response.text
    assert on_event_loop == [False]


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


def test_config_reload_after_api_save_keeps_returned_generation(test_client: TestClient) -> None:
    """The file watcher should not make an API save response immediately stale."""
    initial_load = test_client.post("/api/config/load")
    assert initial_load.status_code == 200
    initial_generation = int(initial_load.headers[config_lifecycle.CONFIG_GENERATION_HEADER])

    first_save_response = test_client.put(
        "/api/config/save",
        headers={config_lifecycle.CONFIG_GENERATION_HEADER: str(initial_generation)},
        json=_authored_config_payload("first"),
    )
    assert first_save_response.status_code == 200
    first_save_generation = int(first_save_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER])

    assert config_lifecycle.load_config_into_app(main._app_runtime_paths(test_client.app), main.app).loaded

    reloaded_config_response = test_client.post("/api/config/load")
    assert reloaded_config_response.status_code == 200
    assert int(reloaded_config_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER]) == first_save_generation

    second_save_response = test_client.put(
        "/api/config/save",
        headers={config_lifecycle.CONFIG_GENERATION_HEADER: str(first_save_generation)},
        json=_authored_config_payload("second"),
    )
    assert second_save_response.status_code == 200
    assert int(second_save_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER]) > first_save_generation


def test_external_raw_config_reload_advances_generation_for_same_authored_config(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """External raw-only edits should still make older raw editor drafts stale."""
    initial_raw_response = test_client.get("/api/config/raw")
    assert initial_raw_response.status_code == 200
    initial_generation = int(initial_raw_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER])
    initial_source = initial_raw_response.json()["source"]

    externally_edited_source = f"# external operator note\n{initial_source}"
    temp_config_file.write_text(externally_edited_source, encoding="utf-8")

    assert config_lifecycle.load_config_into_app(main._app_runtime_paths(test_client.app), main.app).loaded

    reloaded_raw_response = test_client.get("/api/config/raw")
    assert reloaded_raw_response.status_code == 200
    assert int(reloaded_raw_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER]) > initial_generation
    assert reloaded_raw_response.json() == {"source": externally_edited_source, "uses_includes": False}

    stale_save_response = test_client.put(
        "/api/config/raw",
        headers={config_lifecycle.CONFIG_GENERATION_HEADER: str(initial_generation)},
        json={"source": initial_source},
    )
    assert stale_save_response.status_code == 409
    assert temp_config_file.read_text(encoding="utf-8") == externally_edited_source


def test_config_reload_uses_source_fingerprint_from_validated_source(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """A file edit during reload should not publish new config under the old source identity."""
    initial_response = test_client.post("/api/config/load")
    assert initial_response.status_code == 200
    initial_generation = int(initial_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER])
    initial_agents = initial_response.json()["agents"]

    interleaved_source = yaml.safe_dump(_authored_config_payload("interleaved"), sort_keys=True)
    original_read_bytes = Path.read_bytes
    mutated = False

    def _read_then_mutate(path: Path) -> bytes:
        nonlocal mutated
        source = original_read_bytes(path)
        if path == temp_config_file and not mutated:
            mutated = True
            temp_config_file.write_text(interleaved_source, encoding="utf-8")
        return source

    with patch.object(Path, "read_bytes", autospec=True, side_effect=_read_then_mutate):
        assert config_lifecycle.load_config_into_app(main._app_runtime_paths(test_client.app), main.app).loaded

    reloaded_response = test_client.post("/api/config/load")
    assert reloaded_response.status_code == 200
    assert reloaded_response.json()["agents"] == initial_agents
    assert int(reloaded_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER]) == initial_generation

    assert config_lifecycle.load_config_into_app(main._app_runtime_paths(test_client.app), main.app).loaded
    interleaved_response = test_client.post("/api/config/load")
    assert interleaved_response.status_code == 200
    assert list(interleaved_response.json()["agents"]) == ["interleaved"]
    assert int(interleaved_response.headers[config_lifecycle.CONFIG_GENERATION_HEADER]) > initial_generation

    stale_save_response = test_client.put(
        "/api/config/save",
        headers={config_lifecycle.CONFIG_GENERATION_HEADER: str(initial_generation)},
        json=_authored_config_payload("stale"),
    )
    assert stale_save_response.status_code == 409
    assert yaml.safe_load(temp_config_file.read_text(encoding="utf-8"))["agents"] == {
        "interleaved": {"display_name": "Interleaved", "role": "valid", "rooms": []},
    }


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


def test_api_config_load_accepts_missing_plugin_path_in_degraded_mode(temp_config_file: Path) -> None:
    """API config loads should mirror runtime degraded mode for missing plugins."""
    temp_config_file.write_text(
        yaml.safe_dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
                "router": {"model": "default"},
                "agents": {"assistant": {"display_name": "Assistant", "role": "test"}},
                "plugins": ["./plugins/missing"],
            },
        ),
        encoding="utf-8",
    )
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=temp_config_file, process_env={})
    main.initialize_api_app(main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded
    client = TestClient(main.app, base_url="http://localhost")

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
    client = TestClient(main.app, base_url="http://localhost")

    response = client.post("/api/config/load")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["config"]
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]


def test_api_config_load_does_not_serve_stale_cache_after_invalid_reload(temp_config_file: Path) -> None:
    """API config loads should return the latest parse error instead of stale last-known-good data."""
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=temp_config_file, process_env={})
    main.initialize_api_app(main.app, runtime_paths)
    config_lifecycle.load_config_into_app(runtime_paths, main.app)
    client = TestClient(main.app, base_url="http://localhost")

    initial_response = client.post("/api/config/load")
    assert initial_response.status_code == 200
    assert "test_agent" in initial_response.json()["agents"]

    temp_config_file.write_text("agents:\n  broken: [\n", encoding="utf-8")

    assert not config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded

    response = client.post("/api/config/load")

    assert response.status_code == 422
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]
    assert "test_agent" in main._app_context(main.app).runtime_config.authored_model_dump()["agents"]


def test_api_cached_read_endpoints_refuse_stale_config_after_invalid_reload(
    api_key_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Config-backed API reads should return the current parse error instead of stale cache."""
    runtime_paths = main._app_runtime_paths(api_key_client.app)
    temp_config_file.write_text("agents:\n  broken: [\n", encoding="utf-8")

    assert not config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded

    response = api_key_client.get("/api/config/personal-agents", headers={"Authorization": "Bearer test-key"})

    assert response.status_code == 422
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]


def test_api_key_raw_endpoints_recover_from_invalid_reload(
    api_key_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Protected raw config endpoints should stay usable after the structured cache becomes invalid."""
    runtime_paths = main._app_runtime_paths(api_key_client.app)
    invalid_source = "agents:\n  broken: [\n"
    temp_config_file.write_text(invalid_source, encoding="utf-8")

    assert not config_lifecycle.load_config_into_app(runtime_paths, main.app).loaded

    raw_response = api_key_client.get(
        "/api/config/raw",
        headers={"Authorization": "Bearer test-key"},
    )
    assert raw_response.status_code == 200
    assert raw_response.json() == {"source": invalid_source, "uses_includes": False}

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
        "    id: gpt-6-astra\n"
        "agents: {}\n"
        "teams: null\n"
        "plugins: null\n"
        "router:\n"
        "  model: default\n",
        encoding="utf-8",
    )
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=config_path, process_env={})

    config_lifecycle.load_config_into_app(runtime_paths, main.app)

    config_data = main._app_context(main.app).runtime_config.authored_model_dump()
    assert "teams" not in config_data
    assert "plugins" not in config_data


def _dashboard_cors_test_client(runtime_paths: constants.RuntimePaths) -> TestClient:
    api_app = FastAPI()

    @api_app.get("/api/health")
    async def _health_check() -> dict[str, str]:
        return {"status": "healthy"}

    main._add_dashboard_cors_middleware(api_app, runtime_paths)
    return TestClient(api_app)


def test_cors_rejects_unknown_origin_by_default(tmp_path: Path) -> None:
    """Default dashboard CORS should not allow arbitrary browser origins."""
    test_client = _dashboard_cors_test_client(_runtime_paths(tmp_path, process_env={}))

    response = test_client.options(
        "/api/health",
        headers={
            "Origin": "https://dashboard.example.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 400
    assert response.headers.get("access-control-allow-origin") is None


def test_cors_allows_local_frontend_origin_by_default(tmp_path: Path) -> None:
    """Default dashboard CORS should keep the local frontend dev server working."""
    test_client = _dashboard_cors_test_client(_runtime_paths(tmp_path, process_env={}))

    response = test_client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cors_exposes_config_generation_header_for_credentialed_origins(tmp_path: Path) -> None:
    """Credentialed dashboard CORS should expose headers the frontend reads."""
    test_client = _dashboard_cors_test_client(_runtime_paths(tmp_path, process_env={}))

    response = test_client.get("/api/health", headers={"Origin": "http://localhost:5173"})

    assert response.status_code == 200
    exposed = {header.strip() for header in response.headers["access-control-expose-headers"].split(",")}
    assert exposed == {
        config_lifecycle.CONFIG_GENERATION_HEADER,
        config_lifecycle.CONFIG_USES_INCLUDES_HEADER,
        config_lifecycle.CONFIG_PENDING_RESTART_HEADER,
    }


def test_cors_wildcard_opt_in_disables_credentials(tmp_path: Path) -> None:
    """Explicit wildcard CORS must not be combined with credentialed requests."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={"MINDROOM_DASHBOARD_CORS_ALLOW_ALL_ORIGINS": "true"},
    )

    settings = main._dashboard_cors_settings(runtime_paths)
    test_client = _dashboard_cors_test_client(runtime_paths)
    response = test_client.options(
        "/api/health",
        headers={
            "Origin": "https://dashboard.example.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert settings.allow_origins == ("*",)
    assert settings.allow_credentials is False
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers.get("access-control-allow-credentials") is None


def test_cors_empty_allowed_origins_env_uses_default_origins(tmp_path: Path) -> None:
    """Blank configured CORS origins should keep safe local development defaults."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={"MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS": ""},
    )
    settings = main._dashboard_cors_settings(runtime_paths)
    test_client = _dashboard_cors_test_client(runtime_paths)

    response = test_client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "http://localhost:5173" in settings.allow_origins
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_allowed_origins_env_replaces_default_origins(tmp_path: Path) -> None:
    """Configured CORS origins should be explicit and credential-capable."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS": ("https://dashboard.example.test, http://localhost:3003"),
        },
    )
    test_client = _dashboard_cors_test_client(runtime_paths)

    allowed_response = test_client.options(
        "/api/health",
        headers={
            "Origin": "https://dashboard.example.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    default_origin_response = test_client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert allowed_response.status_code == 200
    assert allowed_response.headers["access-control-allow-origin"] == "https://dashboard.example.test"
    assert allowed_response.headers["access-control-allow-credentials"] == "true"
    assert default_origin_response.status_code == 400
    assert default_origin_response.headers.get("access-control-allow-origin") is None


def test_exported_app_cors_uses_reinitialized_runtime(tmp_path: Path) -> None:
    """The exported API app should derive CORS from its current runtime paths."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={"MINDROOM_DASHBOARD_CORS_ALLOWED_ORIGINS": "https://dashboard.example.test"},
    )
    main.initialize_api_app(main.app, runtime_paths)

    test_client = TestClient(main.app)
    allowed_response = test_client.options(
        "/api/health",
        headers={
            "Origin": "https://dashboard.example.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    default_origin_response = test_client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert allowed_response.status_code == 200
    assert allowed_response.headers["access-control-allow-origin"] == "https://dashboard.example.test"
    assert default_origin_response.status_code == 400
    assert default_origin_response.headers.get("access-control-allow-origin") is None


@pytest.fixture
def frontend_dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a bundled dashboard index from a temporary frontend dist directory."""
    frontend_dir = tmp_path / "frontend-dist"
    frontend_dir.mkdir()
    (frontend_dir / "index.html").write_text("<html><body>MindRoom Dashboard</body></html>")
    monkeypatch.setattr(frontend, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)


@pytest.mark.usefixtures("frontend_dist")
def test_frontend_root_serves_index(test_client: TestClient) -> None:
    """Root path should serve the bundled dashboard index when assets are available."""
    response = test_client.get("/")
    assert response.status_code == 200
    assert "MindRoom Dashboard" in response.text


@pytest.mark.usefixtures("frontend_dist")
def test_frontend_spa_routes_fall_back_to_index(test_client: TestClient) -> None:
    """Unknown non-API paths should return index.html for client-side routing."""
    response = test_client.get("/agents")
    assert response.status_code == 200
    assert "MindRoom Dashboard" in response.text


@pytest.mark.usefixtures("frontend_dist")
def test_frontend_does_not_shadow_unknown_api_routes(test_client: TestClient) -> None:
    """Unknown API paths should remain 404 instead of falling back to the SPA."""
    response = test_client.get("/api/not-real")
    assert response.status_code == 404


@pytest.mark.usefixtures("frontend_dist")
def test_frontend_blocks_path_traversal(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Path traversal attempts must not leak files outside the frontend directory."""
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-leak")

    # Starlette normalizes bare `..` segments, so percent-encoded traversal
    # is the real attack vector that _resolve_frontend_asset must block.
    for traversal_path in ["assets/..%2F..%2Fsecret.txt", "..%2Fsecret.txt"]:
        response = test_client.get(f"/{traversal_path}")
        assert response.status_code == 404, f"Path traversal not blocked for {traversal_path}"
        assert "do-not-leak" not in response.text


@pytest.mark.usefixtures("frontend_dist")
def test_frontend_redirects_to_login_when_api_key_auth_is_enabled(api_key_client: TestClient) -> None:
    """Protected standalone dashboards should send unauthenticated users to the login page."""
    response = api_key_client.get("/", follow_redirects=False)
    assert response.status_code == 307
    location = urlparse(response.headers["location"])
    assert location.path == "/login"
    assert parse_qs(location.query) == {"next": ["/"]}

    response = api_key_client.get("/%09/evil.com", follow_redirects=False)
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


def test_frontend_login_page_drops_tab_bearing_next_path(api_key_client: TestClient) -> None:
    """Browsers strip ASCII tabs before parsing, so a tab-bearing target must not survive."""
    response = api_key_client.get("/login?next=/%09/evil.com")

    assert response.status_code == 200
    next_path_line = next(line for line in response.text.splitlines() if "const nextPath =" in line)
    assert next_path_line.strip() == 'const nextPath = "/";'
    assert 'target.origin === window.location.origin ? target.href : "/"' in response.text


@pytest.mark.parametrize(
    ("next_path", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("agents", "/"),
        ("/", "/"),
        ("/agents", "/agents"),
        (
            "/api/oauth/test_drive/authorize?agent_name=general&execution_scope=user",
            "/api/oauth/test_drive/authorize?agent_name=general&execution_scope=user",
        ),
        ("//example.com", "/"),
        ("/\\example.com", "/"),
        ("/%2Fexample.com", "/"),
        ("/%5Cexample.com", "/"),
        ("/%5c/example.com", "/"),
        ("/%255Cexample.com", "/"),
        ("/%252Fexample.com", "/"),
        ("/agents/%5Cprofile", "/agents/%5Cprofile"),
        ("/\t/evil.com", "/"),
        ("/\n/evil.com", "/"),
        ("/\r/evil.com", "/"),
        ("/%09/evil.com", "/"),
        ("/%0a/evil.com", "/"),
        ("/%0d/evil.com", "/"),
        ("/%2509/evil.com", "/"),
        ("/agents\t", "/"),
    ],
)
def test_sanitize_next_path_blocks_protocol_relative_variants(
    next_path: str | None,
    expected: str,
) -> None:
    """Standalone login redirects must stay on same-origin dashboard paths."""
    assert auth.sanitize_next_path(next_path) == expected


def test_frontend_login_propagates_trusted_upstream_auth_misconfiguration(
    api_key_client: TestClient,
) -> None:
    """Trusted-upstream setup errors should stay visible on frontend auth checks."""
    runtime_paths = main._app_runtime_paths(api_key_client.app)
    config_lifecycle.app_state(api_key_client.app).api_auth_state = auth.ApiAuthState(
        runtime_paths=runtime_paths,
        settings=auth._ApiAuthSettings(
            supabase_url=None,
            supabase_anon_key=None,
            account_id=None,
            mindroom_api_key="test-key",
            trusted_upstream=auth._TrustedUpstreamAuthSettings(enabled=True),
        ),
        supabase_auth=None,
    )

    response = api_key_client.get("/login?next=/agents")

    assert response.status_code == 500
    assert "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER" in response.json()["detail"]


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "https://other.example.org"}, 403),
        ({"Origin": "http://localhost", "Sec-Fetch-Site": "cross-site"}, 403),
        ({"Origin": "http://localhost"}, 200),
        ({"Authorization": "Bearer "}, 403),
        ({"Authorization": "Basic test-key"}, 403),
        ({"Authorization": "Bearer test-key"}, 200),
    ],
)
def test_cookie_mutations_require_browser_origin(
    api_key_client: TestClient,
    headers: dict[str, str],
    expected: int,
) -> None:
    """Only a validated bearer credential can bypass the browser mutation guard."""
    api_key_client.cookies.set("mindroom_api_key", "test-key")
    response = api_key_client.post("/api/config/load", headers=headers)
    assert response.status_code == expected, response.text


@pytest.mark.parametrize(
    ("public_url", "origin", "expected"),
    [
        ("https://public.example.org/dashboard", "https://public.example.org", 200),
        ("https://public.example.org", "http://localhost", 403),
        ("missing-scheme", "://", 403),
    ],
)
def test_cookie_mutations_use_configured_public_origin(
    api_key_client: TestClient,
    public_url: str,
    origin: str,
    expected: int,
) -> None:
    """A configured origin overrides the request host and must fail closed when invalid."""
    app_state = config_lifecycle.app_state(api_key_client.app)
    auth_state = auth._app_auth_state(api_key_client.app, main._app_runtime_paths(api_key_client.app))
    app_state.api_auth_state = replace(auth_state, settings=replace(auth_state.settings, public_url=public_url))
    api_key_client.cookies.set("mindroom_api_key", "test-key")
    response = api_key_client.post("/api/config/load", headers={"Origin": origin})
    assert response.status_code == expected, response.text


def test_api_key_cookie_auth_allows_protected_requests(api_key_client: TestClient) -> None:
    """A valid standalone auth session cookie should work without bearer headers."""
    response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert response.status_code == 200
    assert response.cookies.get("mindroom_api_key") == "test-key"

    response = api_key_client.post("/api/config/load", headers={"Origin": "http://localhost"})
    assert response.status_code == 200


@pytest.mark.usefixtures("frontend_dist")
def test_frontend_serves_after_api_key_login(api_key_client: TestClient) -> None:
    """Authenticated standalone users should receive the bundled dashboard."""
    login_with_api_key(api_key_client)

    response = api_key_client.get("/")
    assert response.status_code == 200
    assert "MindRoom Dashboard" in response.text


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
                "private_root": None,
            },
            "leader": {
                "agent_name": "leader",
                "is_private": False,
                "effective_execution_scope": None,
                "scope_label": "unscoped",
                "scope_source": "unscoped",
                "dashboard_credentials_supported": True,
                "team_eligibility_reason": "Delegates to private agent 'mind', so it cannot participate in teams.",
                "private_knowledge_base_id": None,
                "private_workspace_enabled": False,
                "private_agent_knowledge_enabled": False,
                "private_root": None,
            },
            "mind": {
                "agent_name": "mind",
                "is_private": True,
                "effective_execution_scope": "user",
                "scope_label": "private.per=user",
                "scope_source": "private.per",
                "dashboard_credentials_supported": False,
                "team_eligibility_reason": "Private agents cannot be configured as team members.",
                "private_knowledge_base_id": None,
                "private_workspace_enabled": True,
                "private_agent_knowledge_enabled": False,
                "private_root": None,
            },
        },
    }


def test_agent_policies_endpoint_reports_draft_root_scope_and_team_eligibility(test_client: TestClient) -> None:
    """Drafts keep their private root and scope fall-through, and each agent says why it cannot join a team."""
    response = test_client.post(
        "/api/config/agent-policies",
        json={
            "defaults": {"worker_scope": "user"},
            "agents": {
                "fanout": {"display_name": "Fanout", "delegate_to": ["mind", "notes", "ghost"]},
                "caller": {"display_name": "Caller", "delegate_to": ["ghost"]},
                "mind": {"display_name": "Mind", "private": {"per": "user_agent", "root": " workspace/mind "}},
                "notes": {"display_name": "Notes", "private": {"knowledge": {"path": "kb"}}},
            },
        },
    )

    assert response.status_code == 200
    policies = response.json()["agent_policies"]
    assert {name: policy["team_eligibility_reason"] for name, policy in policies.items()} == {
        "fanout": "Delegates to private agents 'mind', 'notes', so it cannot participate in teams.",
        "caller": None,
        "mind": "Private agents cannot be configured as team members.",
        "notes": "Private agents cannot be configured as team members.",
    }
    assert policies["mind"]["private_root"] == "workspace/mind"
    assert policies["notes"]["scope_source"] == "defaults.worker_scope"
    assert policies["notes"]["effective_execution_scope"] == "user"
    assert policies["notes"]["private_knowledge_base_id"] == "__agent_private__:notes"


# ---------------------------------------------------------------------------
# MINDROOM_API_KEY authentication tests
# ---------------------------------------------------------------------------


@pytest.fixture
def api_key_client(temp_config_file: Path) -> TestClient:
    """Create a test client with MINDROOM_API_KEY enabled."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        process_env={"MINDROOM_OWNER_USER_ID": "@owner:example.org", "MINDROOM_API_KEY": "test-key"},
    )
    main.initialize_api_app(main.app, runtime_paths)
    bind_test_embedded_runtime(main.app)
    config_lifecycle.load_config_into_app(main._app_runtime_paths(main.app), main.app)
    return TestClient(main.app, base_url="http://localhost")


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
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
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
        "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
        "router": {"model": "default"},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "new",
                "rooms": ["new-room"],
            },
        },
    }
    original_read = config_lifecycle.read_committed_runtime_config

    def _swap_then_read(request: Request) -> object:
        _publish_committed_runtime_config(main.app, runtime_b, payload_b)
        return original_read(request)

    client = TestClient(main.app)
    with patch.object(config_lifecycle, "read_committed_runtime_config", side_effect=_swap_then_read):
        _publish_committed_runtime_config(main.app, runtime_a, payload_a)
        response = client.post(
            "/api/config/load",
            headers={"Authorization": "Bearer key-a"},
        )

    assert response.status_code == 200
    assert response.json()["agents"]["assistant"]["rooms"] == ["old-room"]


@pytest.mark.parametrize(
    ("path", "body"),
    [
        pytest.param("/api/config/save", _authored_config_payload("written"), id="save"),
        pytest.param(
            "/api/config/raw",
            {"source": yaml.safe_dump(_authored_config_payload("written"), sort_keys=True)},
            id="raw",
        ),
    ],
)
def test_protected_write_rejects_runtime_swap_after_auth(tmp_path: Path, path: str, body: dict[str, Any]) -> None:
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
    source_b = yaml.safe_dump(payload_b, sort_keys=True)
    runtime_b.config_path.write_text(source_b, encoding="utf-8")
    original_commit = config_lifecycle.commit_dashboard_config

    def _swap_then_commit(*args: object, **kwargs: object) -> object:
        _publish_committed_runtime_config(main.app, runtime_b, payload_b)
        return original_commit(*args, **kwargs)

    client = TestClient(main.app)
    with patch.object(config_lifecycle, "commit_dashboard_config", side_effect=_swap_then_commit):
        _publish_committed_runtime_config(main.app, runtime_a, payload_a)
        response = client.put(path, headers={"Authorization": "Bearer key-a"}, json=body)

    assert response.status_code == 409
    assert runtime_b.config_path.read_text(encoding="utf-8") == source_b


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

    client = TestClient(main.app)
    with patch.object(config_lifecycle, "read_raw_config_source", side_effect=_swap_then_read):
        _publish_committed_runtime_config(main.app, runtime_a, payload_a)
        response = client.get(
            "/api/config/raw",
            headers={"Authorization": "Bearer key-a"},
        )

    assert response.status_code == 200
    assert response.json() == {"source": source_a, "uses_includes": False}


def test_api_key_missing_header_rejects(api_key_client: TestClient) -> None:
    """Missing Authorization header should return 401 when API key is set."""
    response = api_key_client.post("/api/config/load")
    assert response.status_code == 401


def test_api_key_still_required_after_config_publish(api_key_client: TestClient) -> None:
    """The API key stays required after a config publish."""
    runtime_paths = main._app_runtime_paths(api_key_client.app)
    _publish_committed_runtime_config(api_key_client.app, runtime_paths, _authored_config_payload("general"))

    assert api_key_client.post("/api/config/load").status_code == 401
    assert api_key_client.post("/api/config/load", headers={"Authorization": "Bearer test-key"}).status_code == 200


def test_api_key_wrong_key_rejects(api_key_client: TestClient) -> None:
    """Wrong Bearer token should return 401."""
    response = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


def _trusted_auth_test_app(runtime_paths: constants.RuntimePaths) -> FastAPI:
    """Create a minimal app that returns the authenticated API user."""
    api_app = FastAPI()
    main.initialize_api_app(api_app, runtime_paths)

    @api_app.get("/whoami")
    async def _whoami(auth_user: Annotated[AuthUser, Depends(auth.verify_user)]) -> dict[str, Any]:
        return asdict(auth_user)

    return api_app


def test_dashboard_url_origin_may_change_the_dashboard(tmp_path: Path) -> None:
    """A dashboard on its own host may change things from that host's origin, and only there."""
    env = {
        **trusted_upstream_env(),
        "MINDROOM_PUBLIC_URL": "https://chat.example.org",
        "MINDROOM_DASHBOARD_URL": "https://admin.example.org",
    }
    api_app = FastAPI()
    main.initialize_api_app(api_app, _runtime_paths(tmp_path, process_env=env))

    @api_app.post("/change")
    async def _change(_auth_user: Annotated[AuthUser, Depends(auth.verify_user)]) -> dict[str, bool]:
        return {"changed": True}

    signed = {"X-Trusted-Jwt": trusted_upstream_headers()["X-Trusted-Jwt"]}
    for host, origin, expected in (
        ("https://admin.example.org", "https://admin.example.org", 200),
        ("https://admin.example.org", "https://chat.example.org", 403),
        ("https://admin.example.org", "https://evil.example.org", 403),
        ("https://chat.example.org", "https://chat.example.org", 200),
        ("https://chat.example.org", "https://admin.example.org", 403),
    ):
        with TestClient(api_app, base_url=host) as client:
            response = client.post("/change", headers={**signed, "Origin": origin})
        assert response.status_code == expected, (host, origin, response.text)


def test_dashboard_url_without_trusted_upstream_auth_fails_startup(tmp_path: Path) -> None:
    """Only a gateway's signed identity reaches both hosts, so the setting needs trusted upstream auth."""
    with pytest.raises(ValueError, match="MINDROOM_DASHBOARD_URL requires trusted upstream auth"):
        auth._build_auth_settings(
            _runtime_paths(tmp_path, process_env={"MINDROOM_DASHBOARD_URL": "https://admin.example.org"}),
        )


@pytest.mark.parametrize("public_url", [None, "https://admin.example.org", "https://admin.example.org/mindroom"])
def test_dashboard_url_on_the_public_host_fails_startup(tmp_path: Path, public_url: str | None) -> None:
    """The dashboard host must be another host or port than a set `MINDROOM_PUBLIC_URL`."""
    env = {**trusted_upstream_env(), "MINDROOM_DASHBOARD_URL": "https://admin.example.org"}
    if public_url is not None:
        env["MINDROOM_PUBLIC_URL"] = public_url
    with pytest.raises(ValueError, match="must name another host or port than MINDROOM_PUBLIC_URL"):
        auth._build_auth_settings(_runtime_paths(tmp_path, process_env=env))


def test_retired_base_url_fails_startup(tmp_path: Path) -> None:
    """A deployment that still sets `MINDROOM_BASE_URL` is told to set `MINDROOM_PUBLIC_URL` instead."""
    with pytest.raises(ValueError, match="MINDROOM_BASE_URL was removed; set MINDROOM_PUBLIC_URL"):
        auth._build_auth_settings(
            _runtime_paths(tmp_path, process_env={"MINDROOM_BASE_URL": "https://mindroom.example.org"}),
        )


def test_invalid_dashboard_url_fails_startup(tmp_path: Path) -> None:
    """A dashboard URL that is not an HTTPS origin, or HTTP on a local address, is a setup error."""
    with pytest.raises(ValueError, match="Browser origin must be explicit HTTPS"):
        auth._build_auth_settings(
            _runtime_paths(tmp_path, process_env={"MINDROOM_DASHBOARD_URL": "http://admin.example.org"}),
        )


def test_trusted_upstream_assertion_ignored_when_disabled(tmp_path: Path) -> None:
    """A signed upstream assertion must do nothing unless trusted upstream auth is enabled."""
    api_app = _trusted_auth_test_app(_runtime_paths(tmp_path))

    with TestClient(api_app, base_url="http://localhost") as client:
        response = client.get("/whoami", headers={"X-Trusted-Jwt": trusted_upstream_headers()["X-Trusted-Jwt"]})

    assert response.status_code == 200
    assert response.json() == {"user_id": "standalone", "email": None, "source": "standalone", "matrix_user_id": None}


def _signed_whoami(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    **claims: Any,  # noqa: ANN401
) -> httpx.Response:
    """Return `/whoami` for a request whose assertion carries `claims`, signed by the key the JWKS serves."""
    private_key = trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: trusted_upstream_jwks(private_key))
    token = trusted_upstream_jwt(private_key, **claims)
    api_app = _trusted_auth_test_app(
        _runtime_paths(tmp_path, process_env=env or trusted_upstream_env(matrix_user_id_claim=None)),
    )
    with TestClient(api_app) as client:
        return client.get("/whoami", headers={**(headers or {}), "X-Trusted-Jwt": token})


def test_trusted_upstream_jwt_accepts_valid_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Trusted upstream auth should take the user from the signed upstream assertion."""
    response = _signed_whoami(monkeypatch, tmp_path)

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_123",
        "email": "alice@example.com",
        "source": "trusted_upstream",
        "matrix_user_id": None,
    }


def test_trusted_upstream_jwt_accepts_non_ascii_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Trusted upstream auth should handle non-ASCII identifier values."""
    response = _signed_whoami(monkeypatch, tmp_path, user_id="üser_123", email="álîçé@example.com")

    assert response.status_code == 200
    assert response.json()["user_id"] == "üser_123"
    assert response.json()["email"] == "álîçé@example.com"


def test_trusted_upstream_jwt_accepts_email_claim_as_user_id_without_user_id_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The email claim is the user identity when no user ID claim is configured."""
    response = _signed_whoami(
        monkeypatch,
        tmp_path,
        env=trusted_upstream_env(user_id_claim=None, matrix_user_id_claim=None),
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "alice@example.com",
        "email": "alice@example.com",
        "source": "trusted_upstream",
        "matrix_user_id": None,
    }


def test_trusted_upstream_jwt_ignores_identity_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unsigned identity headers next to the assertion must not change who the user is."""
    response = _signed_whoami(
        monkeypatch,
        tmp_path,
        headers={
            "X-Trusted-User": "bob",
            "X-Trusted-Email": "bob@example.com",
            "X-Trusted-Matrix-User": "@bob:example.org",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_123",
        "email": "alice@example.com",
        "source": "trusted_upstream",
        "matrix_user_id": None,
    }


def test_trusted_upstream_jwt_accepts_signed_matrix_claim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A configured Matrix claim names the user's Matrix identity."""
    response = _signed_whoami(
        monkeypatch,
        tmp_path,
        env=trusted_upstream_env(),
        matrix_user_id="@alice:example.org",
    )

    assert response.status_code == 200
    assert response.json() == {
        "user_id": "user_123",
        "email": "alice@example.com",
        "source": "trusted_upstream",
        "matrix_user_id": "@alice:example.org",
    }


def test_trusted_upstream_jwt_signed_matrix_claim_ignores_email_template_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A signed Matrix claim should not be blocked by unrelated email-template config."""
    env = trusted_upstream_env()
    env["MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE"] = "@static:example.org"
    response = _signed_whoami(monkeypatch, tmp_path, env=env, matrix_user_id="@alice:example.org")

    assert response.status_code == 200
    assert response.json()["matrix_user_id"] == "@alice:example.org"


@pytest.mark.parametrize("matrix_user_id", ["@Alice:example.org", "@:example.org"])
def test_trusted_upstream_jwt_accepts_historical_matrix_user_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    matrix_user_id: str,
) -> None:
    """Trusted Matrix identities may use historical Matrix localparts."""
    response = _signed_whoami(
        monkeypatch,
        tmp_path,
        env=trusted_upstream_env(),
        matrix_user_id=matrix_user_id,
    )

    assert response.status_code == 200
    assert response.json()["matrix_user_id"] == matrix_user_id


@pytest.mark.parametrize(
    "matrix_user_id",
    [
        "@alice:example.org extra",
        "@alice:",
        "@alice:[::::]",
        "@alice:example.org.",
    ],
)
def test_trusted_upstream_jwt_rejects_invalid_matrix_user_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    matrix_user_id: str,
) -> None:
    """Signed Matrix IDs must still be concrete Matrix user IDs."""
    response = _signed_whoami(
        monkeypatch,
        tmp_path,
        env=trusted_upstream_env(),
        matrix_user_id=matrix_user_id,
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid trusted upstream Matrix user id"


@pytest.mark.parametrize(
    ("email_domain", "email", "expected"),
    [
        ("example.com", "alice@example.com", 200),
        ("example.com", "alice@EXAMPLE.COM", 200),
        ("example.com", "alice@another.example.com", 401),
        ("example.com", "alice@other.example", 401),
        ("", "alice@example.com", 500),
    ],
)
def test_trusted_upstream_jwt_derives_matrix_user_id_from_email_localpart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    email_domain: str,
    email: str,
    expected: int,
) -> None:
    """Without a Matrix claim, an email template may name the Matrix identity from the signed email."""
    env = trusted_upstream_env(matrix_user_id_claim=None)
    env["MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE"] = "@{localpart}:example.org"
    env["MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN"] = email_domain
    response = _signed_whoami(monkeypatch, tmp_path, env=env, email=email)

    assert response.status_code == expected, response.text
    if expected == 200:
        assert response.json()["matrix_user_id"] == "@alice:example.org"


@pytest.mark.parametrize(
    "template",
    ["@alice:example.org", "@{localpart}-{localpart}:example.org", "@{localpart}:example.org."],
)
def test_trusted_upstream_jwt_rejects_invalid_email_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    template: str,
) -> None:
    """Constant, ambiguous, or unparsable email-to-Matrix templates are a setup error."""
    env = trusted_upstream_env(matrix_user_id_claim=None)
    env["MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE"] = template
    env["MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN"] = "example.com"
    response = _signed_whoami(monkeypatch, tmp_path, env=env)

    assert response.status_code == 500
    assert response.json()["detail"] == (
        "Trusted upstream email mapping requires a valid template and MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN"
    )


@pytest.mark.parametrize(
    "missing",
    [
        "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER",
        "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE",
        "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER",
    ],
)
def test_trusted_upstream_jwt_fails_closed_when_a_setting_is_missing(tmp_path: Path, missing: str) -> None:
    """Enabling trusted upstream auth without its full JWT setup refuses every request."""
    env = trusted_upstream_env(matrix_user_id_claim=None)
    env.pop(missing)
    api_app = _trusted_auth_test_app(_runtime_paths(tmp_path, process_env=env))

    with TestClient(api_app) as client:
        response = client.get("/whoami", headers=trusted_upstream_headers())

    assert response.status_code == 500
    assert response.json()["detail"] == f"Trusted upstream auth is enabled but {missing} is not set"


def test_trusted_upstream_jwt_rejects_missing_token(tmp_path: Path) -> None:
    """Trusted upstream auth should not accept identity headers alone."""
    api_app = _trusted_auth_test_app(
        _runtime_paths(tmp_path, process_env=trusted_upstream_env(matrix_user_id_claim=None)),
    )

    with TestClient(api_app) as client:
        response = client.get("/whoami", headers={"X-Trusted-User": "user_123", "X-Trusted-Email": "alice@example.com"})

    assert response.status_code == 401
    assert "trusted upstream JWT header" in response.json()["detail"]


def test_trusted_upstream_jwt_rejects_invalid_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Trusted upstream auth should reject a token signed by another key."""
    trusted_key = trusted_upstream_jwt_key()
    signing_key = trusted_upstream_jwt_key()
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _client: trusted_upstream_jwks(trusted_key))
    token = trusted_upstream_jwt(signing_key)
    api_app = _trusted_auth_test_app(
        _runtime_paths(tmp_path, process_env=trusted_upstream_env(matrix_user_id_claim=None)),
    )

    with TestClient(api_app) as client:
        response = client.get("/whoami", headers={"X-Trusted-Jwt": token})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid trusted upstream JWT"


@pytest.mark.parametrize(
    "claims",
    [{"audience": "other-audience"}, {"expires_at": datetime.now(UTC) - timedelta(minutes=1)}, {"email": " "}],
    ids=["wrong-audience", "expired", "blank-email"],
)
def test_trusted_upstream_jwt_rejects_invalid_claims(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    claims: dict[str, Any],
) -> None:
    """Trusted upstream auth should reject assertions for another audience, expired ones, and blank identities."""
    response = _signed_whoami(monkeypatch, tmp_path, **claims)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid trusted upstream JWT"


def test_trusted_upstream_jwt_resolves_jwks_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """JWKS resolution should not block the async request event loop."""
    original_get_signing_key = jwt.PyJWKClient.get_signing_key_from_jwt
    running_loops: list[bool] = []

    def wrapped_get_signing_key(client: jwt.PyJWKClient, token: str | bytes) -> jwt.PyJWK:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            running_loops.append(False)
        else:
            running_loops.append(True)
        return original_get_signing_key(client, token)

    monkeypatch.setattr(jwt.PyJWKClient, "get_signing_key_from_jwt", wrapped_get_signing_key)
    response = _signed_whoami(monkeypatch, tmp_path)

    assert response.status_code == 200
    assert running_loops == [False]


@pytest.mark.parametrize(
    "path",
    [
        "/api/homeassistant/callback?code=test-code&state=missing",
        "/api/oauth/google_drive/callback?code=test-code&state=missing",
    ],
)
def test_api_key_login_reaches_oauth_callback_state_checks(
    api_key_client: TestClient,
    path: str,
) -> None:
    """A logged-in browser reaches each callback's own state check, which refuses a state it never issued."""
    login_with_api_key(api_key_client)

    response = api_key_client.get(path)
    assert response.status_code == 400
    # The generic OAuth callback answers the popup with a page; the Home Assistant callback answers JSON.
    body = response.text if path.startswith("/api/oauth/") else response.json()["detail"]
    assert "OAuth state is invalid or expired" in body


def test_homeassistant_callback_refuses_an_unauthenticated_request(api_key_client: TestClient) -> None:
    """The router's authentication is the callback's only login check, so it must refuse before any state check."""
    api_key_client.cookies.clear()

    assert api_key_client.get("/api/homeassistant/callback?code=test-code&state=missing").status_code == 401


_OTHER_INSTANCE_SSO_SECRET = "another-instance-platform-sso-secret-0002"  # noqa: S105


def _platform_sso_token(
    token_type: str,
    *,
    audience: str = "http://localhost",
    subject: str = "user-123",
    secret: str = PLATFORM_SSO_SECRET,
    lifetime_seconds: int = 60,
) -> str:
    """Sign a platform login ticket or session the way the platform and runtime do."""
    now = int(time.time())
    return jwt.encode(
        {
            "typ": token_type,
            "aud": audience,
            "sub": subject,
            "email": "user@example.com",
            "iat": now,
            "exp": now + lifetime_seconds,
            "jti": uuid4().hex,
        },
        secret,
        algorithm="HS256",
    )


def _platform_ticket(**kwargs: Any) -> str:  # noqa: ANN401
    return _platform_sso_token(auth._PLATFORM_SSO_TICKET_TYPE, **kwargs)


def _platform_session(**kwargs: Any) -> str:  # noqa: ANN401
    return _platform_sso_token(auth._PLATFORM_SESSION_TYPE, **kwargs)


def _use_platform_session(test_client: TestClient, session_token: str) -> None:
    test_client.cookies.clear()
    test_client.cookies.set(auth._PLATFORM_SESSION_COOKIE_NAME, session_token)


def test_platform_sso_ticket_exchange_sets_host_only_session(test_client: TestClient) -> None:
    """A valid ticket becomes a host-only session cookie that authenticates dashboard calls."""
    set_platform_auth(valid_tokens=set(), account_id="user-123")

    response = test_client.get(
        "/api/auth/platform-sso",
        params={"ticket": _platform_ticket(), "next": "/agents?tab=1"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/agents?tab=1"
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{auth._PLATFORM_SESSION_COOKIE_NAME}=")
    set_cookie_lower = set_cookie.lower()
    assert "domain=" not in set_cookie_lower
    assert "path=/" in set_cookie_lower
    assert "secure" in set_cookie_lower
    assert "httponly" in set_cookie_lower
    assert "samesite=lax" in set_cookie_lower

    session_token = set_cookie.split(";", 1)[0].split("=", 1)[1]
    _use_platform_session(test_client, session_token)
    assert test_client.post("/api/config/load", headers={"Origin": "http://localhost"}).status_code == 200


def test_platform_sso_ticket_is_single_use(test_client: TestClient) -> None:
    """A captured ticket cannot be exchanged a second time."""
    set_platform_auth(valid_tokens=set())
    ticket = _platform_ticket()

    first = test_client.get("/api/auth/platform-sso", params={"ticket": ticket}, follow_redirects=False)
    second = test_client.get("/api/auth/platform-sso", params={"ticket": ticket}, follow_redirects=False)

    assert first.status_code == 307
    assert second.status_code == 401


def test_platform_sso_prunes_expired_ticket_ids_and_keeps_redirects_in_app(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The replay cache drops expired IDs, and a ticket exchange never redirects off the instance."""
    used_ticket_ids = {"expired-ticket": int(time.time()) - 3600}
    monkeypatch.setattr(auth, "_used_platform_sso_ticket_ids", used_ticket_ids)
    set_platform_auth(valid_tokens=set())

    response = test_client.get(
        "/api/auth/platform-sso",
        params={"ticket": _platform_ticket(), "next": "//evil.example.test/steal"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/"
    assert "expired-ticket" not in used_ticket_ids
    assert len(used_ticket_ids) == 1


@pytest.mark.parametrize(
    "ticket_kwargs",
    [
        {"secret": _OTHER_INSTANCE_SSO_SECRET},
        {"audience": "https://other-tenant.example.test"},
        {"lifetime_seconds": -60},
    ],
    ids=["other-instance-key", "other-instance-audience", "expired"],
)
def test_platform_sso_rejects_tickets_for_other_instances(
    test_client: TestClient,
    ticket_kwargs: dict[str, Any],
) -> None:
    """Tickets signed for another instance, naming another instance, or expired are rejected."""
    set_platform_auth(valid_tokens=set())

    response = test_client.get(
        "/api/auth/platform-sso",
        params={"ticket": _platform_ticket(**ticket_kwargs)},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "set-cookie" not in response.headers


def test_platform_sso_tickets_and_sessions_are_not_interchangeable(test_client: TestClient) -> None:
    """A ticket is not a session, and a session is not a ticket."""
    set_platform_auth(valid_tokens=set())

    _use_platform_session(test_client, _platform_ticket())
    assert test_client.post("/api/config/load", headers={"Origin": "http://localhost"}).status_code == 401

    test_client.cookies.clear()
    response = test_client.get(
        "/api/auth/platform-sso",
        params={"ticket": _platform_session()},
        follow_redirects=False,
    )
    assert response.status_code == 401


def test_platform_sso_rejects_other_account_ticket(test_client: TestClient) -> None:
    """The instance only admits its configured account."""
    set_platform_auth(valid_tokens=set(), account_id="account-owner")

    response = test_client.get(
        "/api/auth/platform-sso",
        params={"ticket": _platform_ticket(subject="other-account")},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "set-cookie" not in response.headers


def test_platform_sso_is_disabled_without_instance_secret(test_client: TestClient) -> None:
    """Without the instance SSO key the runtime neither exchanges tickets nor trusts sessions."""
    set_platform_auth(valid_tokens=set(), platform_sso_secret=None)

    response = test_client.get(
        "/api/auth/platform-sso",
        params={"ticket": _platform_ticket()},
        follow_redirects=False,
    )
    assert response.status_code == 404

    _use_platform_session(test_client, _platform_session())
    assert test_client.post("/api/config/load", headers={"Origin": "http://localhost"}).status_code == 401


def test_platform_runtime_ignores_platform_api_cookie(test_client: TestClient) -> None:
    """Tenant runtimes must not treat the platform's Supabase cookie as a dashboard credential."""
    supabase_token = "valid-supabase-token"  # noqa: S105
    set_platform_auth(valid_tokens={supabase_token})
    test_client.cookies.set("mindroom_jwt", supabase_token)

    response = test_client.post("/api/config/load", headers={"Origin": "http://localhost"})

    assert response.status_code == 401


def test_platform_session_mutations_require_same_origin(test_client: TestClient) -> None:
    """Cookie sessions remain subject to the browser mutation origin check."""
    set_platform_auth(valid_tokens=set())
    _use_platform_session(test_client, _platform_session())

    response = test_client.post("/api/config/load", headers={"Origin": "https://evil.example.test"})

    assert response.status_code == 403


def test_platform_auth_settings_read_sso_environment(tmp_path: Path) -> None:
    """The runtime reads the SSO endpoint and instance key that the instance chart provides."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_PLATFORM_SSO_URL": "https://api.example.test/instance-sso/authorize",
            "MINDROOM_PLATFORM_SSO_SECRET": PLATFORM_SSO_SECRET,
        },
    )

    settings = auth._build_auth_settings(runtime_paths)

    assert settings.platform_sso_url == "https://api.example.test/instance-sso/authorize"
    assert settings.platform_sso_secret == PLATFORM_SSO_SECRET


@pytest.mark.usefixtures("frontend_dist")
def test_platform_frontend_redirects_to_login_when_cookie_missing(test_client: TestClient) -> None:
    """Platform deployments should redirect unauthenticated dashboard requests to platform SSO."""
    set_platform_auth(
        valid_tokens=set(),
        platform_sso_url="https://api.example.com/instance-sso/authorize",
    )

    response = test_client.get("/agents", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://api.example.com/instance-sso/authorize?redirect_to=")


@pytest.mark.usefixtures("frontend_dist")
def test_platform_frontend_skips_sso_redirect_without_instance_secret(test_client: TestClient) -> None:
    """Without the instance key, a platform redirect would only end in an unusable ticket."""
    set_platform_auth(
        valid_tokens=set(),
        platform_sso_url="https://api.example.com/instance-sso/authorize",
        platform_sso_secret=None,
    )

    response = test_client.get("/agents", follow_redirects=False)

    assert response.status_code == 401
    assert "location" not in response.headers


@pytest.mark.usefixtures("frontend_dist")
def test_platform_frontend_redirect_uses_public_url_for_redirect_to(test_client: TestClient) -> None:
    """Hosted redirects must use the public dashboard URL, not ingress-internal HTTP."""
    set_platform_auth(
        valid_tokens=set(),
        platform_sso_url="https://api.example.com/instance-sso/authorize",
        public_url="https://tenant42.example.test",
    )

    response = test_client.get("/agents", follow_redirects=False)
    assert response.status_code == 307

    location = response.headers["location"]
    parsed = urlparse(location)
    redirect_to = parse_qs(parsed.query)["redirect_to"][0]
    assert redirect_to == "https://tenant42.example.test/agents"


def test_platform_runtime_disables_generated_api_docs(tmp_path: Path) -> None:
    """Hosted instances should not expose generated FastAPI docs on public hosts."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_PLATFORM_LOGIN_URL": "https://app.example.com/auth/login",
        },
    )

    assert main._api_docs_kwargs(runtime_paths) == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }


def test_platform_runtime_restricts_cors_origins(tmp_path: Path) -> None:
    """Hosted instances should not allow credentialed CORS from every origin."""
    runtime_paths = _runtime_paths(
        tmp_path,
        process_env={
            "MINDROOM_PUBLIC_URL": "https://tenant42.example.test",
            "MINDROOM_PLATFORM_LOGIN_URL": "https://app.example.test/auth/login",
        },
    )

    assert auth._hosted_browser_origins(runtime_paths) == [
        "https://tenant42.example.test",
        "https://app.example.test",
    ]


@pytest.mark.usefixtures("frontend_dist")
def test_platform_frontend_redirects_to_login_when_cookie_invalid(test_client: TestClient) -> None:
    """Invalid platform sessions must redirect to login instead of serving the SPA shell."""
    set_platform_auth(
        valid_tokens=set(),
        platform_sso_url="https://api.example.com/instance-sso/authorize",
    )
    _use_platform_session(test_client, _platform_session(secret=_OTHER_INSTANCE_SSO_SECRET))

    response = test_client.get(
        "/agents",
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://api.example.com/instance-sso/authorize?redirect_to=")


@pytest.mark.usefixtures("frontend_dist")
def test_platform_frontend_serves_dashboard_with_valid_cookie(test_client: TestClient) -> None:
    """Valid platform sessions should grant access to the bundled dashboard."""
    set_platform_auth(
        valid_tokens=set(),
        platform_sso_url="https://api.example.com/instance-sso/authorize",
    )
    _use_platform_session(test_client, _platform_session())

    response = test_client.get("/")
    assert response.status_code == 200
    assert "MindRoom Dashboard" in response.text


@pytest.mark.usefixtures("frontend_dist")
def test_platform_frontend_redirects_when_cookie_account_mismatches(test_client: TestClient) -> None:
    """Platform frontend access must enforce the instance account id."""
    set_platform_auth(
        valid_tokens=set(),
        platform_sso_url="https://api.example.com/instance-sso/authorize",
        account_id="account-owner",
    )
    _use_platform_session(test_client, _platform_session(subject="other-account"))

    response = test_client.get(
        "/",
        follow_redirects=False,
    )
    assert response.status_code == 307
    assert response.headers["location"].startswith("https://api.example.com/instance-sso/authorize?redirect_to=")


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
        "e2ee": e2ee_stats().as_dict(),
        "stale_sync_entities": ["router"],
    }
    assert _matrix_sync_state["router"].loop_started_time == first_start_time

    reset_matrix_sync_health()
    reset_runtime_state()


def test_response_activity_probe_stays_open(api_key_client: TestClient) -> None:
    """Aggregate activity has the same unauthenticated probe access as readiness."""
    reset_runtime_state()
    response = api_key_client.get("/api/responses/activity")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_response_activity_probe_needs_no_trusted_proxy_identity(test_client: TestClient) -> None:
    """In-container checks must not need proxy identity headers."""
    use_trusted_upstream_runtime(main.app)
    reset_runtime_state()
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
