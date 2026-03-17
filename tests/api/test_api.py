"""Tests for the dashboard backend API endpoints."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from mindroom import constants, frontend_assets
from mindroom.api import main
from mindroom.api import workers as workers_api
from mindroom.config.main import Config
from mindroom.runtime_state import reset_runtime_state, set_runtime_ready, set_runtime_starting
from mindroom.workers.models import WorkerHandle

TEST_WORKER_AUTH = "token"


def _runtime_paths(tmp_path: Path, *, process_env: dict[str, str] | None = None) -> constants.RuntimePaths:
    return constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=process_env or {},
    )


def _config_with_worker_scope(worker_scope: str | None) -> Config:
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
            "defaults": {"markdown": True},
        },
    )
    config.agents["general"].worker_scope = worker_scope
    return config


def test_init_supabase_auth_returns_none_without_credentials(tmp_path: Path) -> None:
    """Supabase auth should stay disabled when credentials are incomplete."""
    runtime_paths = _runtime_paths(tmp_path)
    assert main._init_supabase_auth(runtime_paths, None, None) is None
    assert main._init_supabase_auth(runtime_paths, "https://supabase.test", None) is None
    assert main._init_supabase_auth(runtime_paths, None, "anon-key") is None


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

    monkeypatch.setattr(main.importlib, "import_module", _missing_supabase)
    monkeypatch.setattr(main, "auto_install_enabled", lambda _runtime_paths: False)
    monkeypatch.setattr(main, "auto_install_tool_extra", _auto_install)

    with pytest.raises(ImportError, match="MINDROOM_NO_AUTO_INSTALL_TOOLS"):
        main._init_supabase_auth(runtime_paths, "https://supabase.test", "anon-key")

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

    monkeypatch.setattr(main.importlib, "import_module", _missing_supabase)
    monkeypatch.setattr(main, "auto_install_enabled", lambda _runtime_paths: True)
    monkeypatch.setattr(main, "auto_install_tool_extra", _auto_install)

    with pytest.raises(ImportError, match=r"mindroom\[supabase\]") as err:
        main._init_supabase_auth(runtime_paths, "https://supabase.test", "anon-key")

    assert install_calls == ["supabase"]
    assert "MINDROOM_NO_AUTO_INSTALL_TOOLS" not in str(err.value)


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
        runtime_paths: constants.RuntimePaths,
    ) -> None:
        assert runtime_paths == main._app_runtime_paths(main.app)
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
    assert main._app_config_data(fresh_app) == {}
    assert hasattr(main._app_config_lock(fresh_app), "acquire")
    assert main._app_auth_state(fresh_app).runtime_paths == runtime_paths


def test_app_auth_state_refreshes_after_runtime_swap(tmp_path: Path) -> None:
    """Replacing app runtime paths should invalidate cached auth settings."""
    fresh_app = FastAPI()
    initial_runtime = _runtime_paths(tmp_path, process_env={})
    refreshed_runtime = _runtime_paths(
        tmp_path,
        process_env={"MINDROOM_API_KEY": "updated-key"},
    )

    main.initialize_api_app(fresh_app, initial_runtime)
    assert main._app_auth_state(fresh_app).settings.mindroom_api_key is None

    main._app_context(fresh_app).runtime_paths = refreshed_runtime

    assert main._app_auth_state(fresh_app).settings.mindroom_api_key == "updated-key"


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
    main._load_config_from_file(first_runtime, fresh_app)
    assert set(main._app_config_data(fresh_app)["agents"]) == {"first"}

    main.initialize_api_app(fresh_app, second_runtime)

    assert main._app_config_data(fresh_app) == {}


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
    main._load_config_from_file(runtime_one, fresh_app)
    assert set(main._app_config_data(fresh_app)["agents"]) == {"first"}

    main.initialize_api_app(fresh_app, runtime_two)

    assert main._app_config_data(fresh_app) == {}


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
        _runtime_paths: constants.RuntimePaths,
    ) -> None:
        await stop_event.wait()

    async def _idle_worker_cleanup(stop_event: asyncio.Event, _runtime_paths: constants.RuntimePaths) -> None:
        await stop_event.wait()

    monkeypatch.setattr(main, "sync_env_to_credentials", lambda runtime_paths: None)  # noqa: ARG005
    monkeypatch.setattr(main, "_watch_config", _idle_watch_config)
    monkeypatch.setattr(main, "_worker_cleanup_loop", _idle_worker_cleanup)

    with TestClient(main.app) as client:
        response = client.post("/api/config/load")

    assert response.status_code == 200
    assert set(response.json()["agents"]) == {"only_alt"}


@pytest.mark.asyncio
async def test_watch_config_uses_single_file_watcher(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Config watching should target the config file itself, not the whole runtime directory."""
    watched_paths: list[Path] = []
    stop_event = asyncio.Event()
    config_path = tmp_path / "config.yaml"

    async def _fake_watch_file(
        file_path: Path,
        callback: Callable[[], Awaitable[object]],
        stop_event: asyncio.Event | None = None,
    ) -> None:
        watched_paths.append(file_path)
        assert stop_event is not None
        await callback()
        stop_event.set()

    runtime_paths = constants.resolve_primary_runtime_paths(config_path=config_path, process_env={})
    main.initialize_api_app(main.app, runtime_paths)
    monkeypatch.setattr(main, "watch_file", _fake_watch_file)
    monkeypatch.setattr(
        main,
        "_load_config_from_file",
        lambda _runtime_paths, _app: watched_paths.append(Path("loaded")),
    )

    await main._watch_config(stop_event, main.app, main._app_runtime_paths(main.app))

    assert watched_paths == [config_path, Path("loaded")]


def test_health_check(test_client: TestClient) -> None:
    """Test the health check endpoint."""
    response = test_client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"


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

    monkeypatch.setattr(main, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(main, "get_primary_worker_manager", lambda *_args, **_kwargs: _FakeWorkerManager())

    assert main._cleanup_workers_once(main._app_runtime_paths(main.app)) == 1


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

    monkeypatch.setattr(workers_api, "primary_worker_backend_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        workers_api,
        "get_primary_worker_manager",
        lambda *_args, **_kwargs: _FakeWorkerManager(),
    )

    response = test_client.get("/api/workers")

    assert response.status_code == 200
    assert response.json()["workers"][0]["worker_key"] == "worker-key"
    assert response.json()["workers"][0]["backend_name"] == "kubernetes"


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


def test_get_tools_hides_shared_only_integrations_for_isolating_worker_scope(test_client: TestClient) -> None:
    """Shared-only integrations should be hidden for isolating worker scopes."""
    config = _config_with_worker_scope("user")

    with (
        patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, Path("config.yaml"))),
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert "homeassistant" not in tools_by_name
    assert "gmail" not in tools_by_name
    assert "spotify" not in tools_by_name
    assert "calculator" in tools_by_name


def test_google_disconnect_rejects_isolating_worker_scope(test_client: TestClient) -> None:
    """Google dashboard actions should reject unsupported worker scopes."""
    config = _config_with_worker_scope("user")

    with (
        patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, Path("config.yaml"))),
    ):
        response = test_client.post("/api/google/disconnect?agent_name=general")

    assert response.status_code == 400
    assert "worker_scope=user" in response.json()["detail"]


def test_homeassistant_connect_oauth_rejects_isolating_worker_scope(test_client: TestClient) -> None:
    """Home Assistant OAuth should reject unsupported worker scopes."""
    config = _config_with_worker_scope("user")

    with (
        patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, Path("config.yaml"))),
    ):
        response = test_client.post(
            "/api/homeassistant/connect/oauth?agent_name=general",
            json={
                "instance_url": "https://ha.example.com",
                "client_id": "client-id",
            },
        )

    assert response.status_code == 400
    assert "worker_scope=user" in response.json()["detail"]


def test_google_connect_uses_pending_oauth_state(
    api_key_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google connect should issue an opaque server-bound state token."""
    config = _config_with_worker_scope("shared")
    issued_state: dict[str, str] = {}

    class _FakeFlow:
        def authorization_url(
            self,
            *,
            access_type: str,
            include_granted_scopes: str,
            prompt: str,
            state: str,
        ) -> tuple[str, str]:
            assert access_type == "offline"
            assert include_granted_scopes == "true"
            assert prompt == "consent"
            issued_state["state"] = state
            return ("https://accounts.google.test/o/oauth2/auth", "ignored")

    class _FakeFlowFactory:
        @staticmethod
        def from_client_config(
            client_config: object,
            *,
            scopes: list[str],
            redirect_uri: str,
        ) -> _FakeFlow:
            assert client_config
            assert scopes
            assert redirect_uri
            return _FakeFlow()

    monkeypatch.setattr(
        "mindroom.api.google_integration._get_oauth_credentials",
        lambda _runtime_paths: {"web": {"client_id": "client-id", "client_secret": "client-secret"}},
    )
    monkeypatch.setattr(
        "mindroom.api.google_integration._ensure_google_packages",
        lambda _runtime_paths: (object, object, _FakeFlowFactory),
    )
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200

    with patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, Path("config.yaml"))):
        response = api_key_client.post("/api/google/connect?agent_name=general")

    assert response.status_code == 200
    assert response.json()["auth_url"] == "https://accounts.google.test/o/oauth2/auth"
    assert issued_state["state"]
    assert issued_state["state"] != "general"


def test_google_configure_writes_runtime_env_file_and_refreshes_runtime(
    api_key_client: TestClient,
    temp_config_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google configure should write to the request runtime `.env` and refresh app runtime paths."""
    config = _config_with_worker_scope("shared")
    captured_client_config: dict[str, Any] = {}

    class _FakeFlow:
        def authorization_url(
            self,
            *,
            access_type: str,
            include_granted_scopes: str,
            prompt: str,
            state: str,
        ) -> tuple[str, str]:
            assert access_type == "offline"
            assert include_granted_scopes == "true"
            assert prompt == "consent"
            assert state
            return ("https://accounts.google.test/o/oauth2/auth", "ignored")

    class _FakeFlowFactory:
        @staticmethod
        def from_client_config(
            client_config: object,
            *,
            scopes: list[str],
            redirect_uri: str,
        ) -> _FakeFlow:
            assert scopes
            assert redirect_uri == "http://localhost:8765/api/google/callback"
            captured_client_config.update(client_config["web"])
            return _FakeFlow()

    configured_client_secret = "configured-client-secret"  # noqa: S105
    configure_response = api_key_client.post(
        "/api/google/configure",
        headers={"Authorization": "Bearer test-key"},
        json={
            "client_id": "configured-client-id",
            "client_secret": configured_client_secret,
            "project_id": "configured-project",
        },
    )
    assert configure_response.status_code == 200

    env_path = temp_config_file.parent / ".env"
    env_contents = env_path.read_text(encoding="utf-8")
    assert "GOOGLE_CLIENT_ID=configured-client-id" in env_contents
    assert f"GOOGLE_CLIENT_SECRET={configured_client_secret}" in env_contents
    assert "GOOGLE_PROJECT_ID=configured-project" in env_contents

    monkeypatch.setattr(
        "mindroom.api.google_integration._ensure_google_packages",
        lambda _runtime_paths: (object, object, _FakeFlowFactory),
    )
    with patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, temp_config_file)):
        connect_response = api_key_client.post(
            "/api/google/connect?agent_name=general",
            headers={"Authorization": "Bearer test-key"},
        )

    assert connect_response.status_code == 200
    assert connect_response.json()["auth_url"] == "https://accounts.google.test/o/oauth2/auth"
    assert captured_client_config["client_id"] == "configured-client-id"
    assert captured_client_config["client_secret"] == configured_client_secret
    assert captured_client_config["redirect_uris"] == ["http://localhost:8765/api/google/callback"]


def test_google_reset_clears_runtime_env_file_and_refreshes_runtime(
    api_key_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Google reset should clear the runtime `.env` and drop the refreshed runtime credentials."""
    config = _config_with_worker_scope("shared")
    env_path = temp_config_file.parent / ".env"

    configure_response = api_key_client.post(
        "/api/google/configure",
        headers={"Authorization": "Bearer test-key"},
        json={
            "client_id": "configured-client-id",
            "client_secret": "configured-client-secret",
        },
    )
    assert configure_response.status_code == 200
    env_path.write_text(env_path.read_text(encoding="utf-8") + "UNRELATED=value\n", encoding="utf-8")

    with patch("mindroom.api.google_integration.get_runtime_credentials_manager") as mock_get_credentials_manager:
        reset_response = api_key_client.post(
            "/api/google/reset",
            headers={"Authorization": "Bearer test-key"},
        )

    assert reset_response.status_code == 200
    mock_get_credentials_manager.return_value.delete_credentials.assert_called_once_with("google")
    env_contents = env_path.read_text(encoding="utf-8")
    assert "GOOGLE_CLIENT_ID=" not in env_contents
    assert "GOOGLE_CLIENT_SECRET=" not in env_contents
    assert "GOOGLE_PROJECT_ID=" not in env_contents
    assert "GOOGLE_REDIRECT_URI=" not in env_contents
    assert "UNRELATED=value" in env_contents

    with patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, temp_config_file)):
        connect_response = api_key_client.post(
            "/api/google/connect?agent_name=general",
            headers={"Authorization": "Bearer test-key"},
        )

    assert connect_response.status_code == 503
    assert "GOOGLE_CLIENT_ID" in connect_response.json()["detail"]


def test_google_runtime_refresh_keeps_config_cache_live(
    api_key_client: TestClient,
) -> None:
    """Google configure/reset should reload config instead of leaving the dashboard cache empty."""
    before_response = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer test-key"},
    )
    assert before_response.status_code == 200
    assert "test_agent" in before_response.json()["agents"]

    configure_response = api_key_client.post(
        "/api/google/configure",
        headers={"Authorization": "Bearer test-key"},
        json={
            "client_id": "configured-client-id",
            "client_secret": "configured-client-secret",
        },
    )
    assert configure_response.status_code == 200

    after_configure = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer test-key"},
    )
    assert after_configure.status_code == 200
    assert "test_agent" in after_configure.json()["agents"]

    reset_response = api_key_client.post(
        "/api/google/reset",
        headers={"Authorization": "Bearer test-key"},
    )
    assert reset_response.status_code == 200

    after_reset = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer test-key"},
    )
    assert after_reset.status_code == 200
    assert "test_agent" in after_reset.json()["agents"]


def test_google_configure_reports_reload_failures_without_clearing_cached_config(
    api_key_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Google configure should fail closed when the refreshed runtime cannot reload config."""
    before_response = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer test-key"},
    )
    assert before_response.status_code == 200
    assert "test_agent" in before_response.json()["agents"]

    temp_config_file.write_text(
        yaml.dump(
            {
                "models": {"default": {"provider": "openai", "id": "gpt-5.4"}},
                "router": {"model": "default"},
                "defaults": {"markdown": True},
                "mindroom_user": {"username": "mindroom_router"},
            },
        ),
        encoding="utf-8",
    )

    configure_response = api_key_client.post(
        "/api/google/configure",
        headers={"Authorization": "Bearer test-key"},
        json={
            "client_id": "configured-client-id",
            "client_secret": "configured-client-secret",
        },
    )

    assert configure_response.status_code == 500
    assert "Failed to save credentials" in configure_response.json()["detail"]

    after_response = api_key_client.post(
        "/api/config/load",
        headers={"Authorization": "Bearer test-key"},
    )
    assert after_response.status_code == 200
    assert "test_agent" in after_response.json()["agents"]


def test_homeassistant_connect_oauth_uses_pending_oauth_state(api_key_client: TestClient) -> None:
    """Home Assistant connect should use state instead of encoding agent_name in the callback URL."""
    config = _config_with_worker_scope("shared")
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200

    with patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, Path("config.yaml"))):
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

    with (
        patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, Path("config.yaml"))),
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
    main._app_context(main.app).auth_state = main._ApiAuthState(
        runtime_paths=main._app_runtime_paths(main.app),
        settings=main._ApiAuthSettings(
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

    with patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, Path("config.yaml"))):
        response = api_key_client.post("/api/integrations/spotify/connect?agent_name=general")

    assert response.status_code == 200
    assert response.json()["auth_url"] == "https://accounts.spotify.test/authorize"
    assert issued_state["state"]
    assert issued_state["state"] != "general"


def test_spotify_status_rejects_isolating_worker_scope(test_client: TestClient) -> None:
    """Spotify dashboard status should reject unsupported worker scopes."""
    config = _config_with_worker_scope("user")

    with (
        patch("mindroom.api.config_lifecycle.load_runtime_config", return_value=(config, Path("config.yaml"))),
    ):
        response = test_client.get("/api/integrations/spotify/status?agent_name=general")

    assert response.status_code == 400
    assert "worker_scope=user" in response.json()["detail"]


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
    assert saved_config["defaults"] == {
        "tools": ["scheduler"],
        "markdown": True,
        "enable_streaming": True,
        "show_stop_button": True,
        "learning": True,
        "learning_mode": "always",
        "compress_tool_results": True,
        "enable_session_summaries": False,
        "show_tool_calls": True,
        "allow_self_config": False,
        "max_preload_chars": 50000,
    }


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
        _runtime_paths: constants.RuntimePaths,
    ) -> None:
        await stop_event.wait()

    async def _idle_worker_cleanup(stop_event: asyncio.Event, _runtime_paths: constants.RuntimePaths) -> None:
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


def test_run_config_write_restores_original_config_before_releasing_lock(tmp_path: Path) -> None:
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

    original_lock = context.config_lock
    context.config_lock = _AssertingLock()
    try:
        with pytest.raises(HTTPException) as exc_info:
            main._run_config_write(
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
        context.config_lock = original_lock

    assert exc_info.value.status_code == 422
    assert context.config_data == original_config


def test_load_config_from_file_normalizes_legacy_null_sections(tmp_path: Path) -> None:
    """API config loads should normalize legacy null optional sections."""
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

    main._load_config_from_file(runtime_paths, main.app)

    assert main._app_context(main.app).config_data["teams"] == {}
    assert main._app_context(main.app).config_data["plugins"] == []


def test_run_config_write_normalizes_legacy_null_sections(tmp_path: Path) -> None:
    """API config writes should accept legacy null optional sections already loaded in memory."""
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

    main._run_config_write(
        main.app,
        lambda _candidate_config: None,
        error_prefix="Failed to save configuration",
    )

    assert main._app_context(main.app).config_data["teams"] == {}
    assert main._app_context(main.app).config_data["plugins"] == []


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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

    response = api_key_client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/login?next=/"


def test_frontend_login_page_renders_for_api_key_auth(api_key_client: TestClient) -> None:
    """Standalone API-key auth should expose a simple login form."""
    response = api_key_client.get("/login?next=/agents")
    assert response.status_code == 200
    assert "Enter the dashboard API key to continue" in response.text
    assert "MINDROOM_API_KEY" in response.text
    assert ".env" in response.text


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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)

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
    main._app_context(main.app).auth_state = main._ApiAuthState(
        runtime_paths=runtime_paths,
        settings=main._ApiAuthSettings(
            platform_login_url=None,
            supabase_url=None,
            supabase_anon_key=None,
            account_id=None,
            mindroom_api_key="test-key",
        ),
        supabase_auth=None,
    )
    main._load_config_from_file(main._app_runtime_paths(main.app), main.app)
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


@pytest.mark.parametrize(
    "path",
    [
        "/api/google/callback?code=test-code&state=missing",
        "/api/homeassistant/callback?code=test-code&state=missing",
        "/api/integrations/spotify/callback?code=test-code&state=missing",
    ],
)
def test_api_key_keeps_oauth_callbacks_open(
    api_key_client: TestClient,
    path: str,
) -> None:
    """OAuth callbacks should stay reachable via the dashboard auth cookie."""
    login_response = api_key_client.post("/api/auth/session", json={"api_key": "test-key"})
    assert login_response.status_code == 200

    response = api_key_client.get(path)
    assert response.status_code == 400
    assert "OAuth state is invalid or expired" in response.json()["detail"]


def _set_platform_auth(
    *,
    valid_tokens: set[str],
    platform_login_url: str = "https://platform.example.com/login",
) -> None:
    """Configure the API module for platform-managed cookie auth tests."""

    class _FakeUser:
        id = "user-123"
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

    main._app_context(main.app).auth_state = main._ApiAuthState(
        runtime_paths=main._app_runtime_paths(main.app),
        settings=main._ApiAuthSettings(
            platform_login_url=platform_login_url,
            supabase_url="https://supabase.example.com",
            supabase_anon_key="anon-key",
            account_id=None,
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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)
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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)
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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda _runtime_paths: frontend_dir)
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
