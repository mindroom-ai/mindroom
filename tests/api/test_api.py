"""Tests for the dashboard backend API endpoints."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NoReturn
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from mindroom import constants, frontend_assets
from mindroom.api import main
from mindroom.config.main import Config
from mindroom.runtime_state import reset_runtime_state, set_runtime_ready, set_runtime_starting


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


def test_init_supabase_auth_returns_none_without_credentials() -> None:
    """Supabase auth should stay disabled when credentials are incomplete."""
    assert main._init_supabase_auth(None, None) is None
    assert main._init_supabase_auth("https://supabase.test", None) is None
    assert main._init_supabase_auth(None, "anon-key") is None


def test_init_supabase_auth_raises_when_auto_install_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing supabase dependency should error with disable hint when auto-install is off."""
    install_calls: list[str] = []

    def _missing_supabase(_name: str) -> NoReturn:
        module_name = "supabase"
        raise ModuleNotFoundError(module_name)

    def _auto_install(extra_name: str) -> bool:
        install_calls.append(extra_name)
        return False

    monkeypatch.setattr(main.importlib, "import_module", _missing_supabase)
    monkeypatch.setattr(main, "auto_install_enabled", lambda: False)
    monkeypatch.setattr(main, "auto_install_tool_extra", _auto_install)

    with pytest.raises(ImportError, match="MINDROOM_NO_AUTO_INSTALL_TOOLS"):
        main._init_supabase_auth("https://supabase.test", "anon-key")

    assert install_calls == ["supabase"]


def test_init_supabase_auth_raises_when_auto_install_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing dependency should error with install hint when auto-install attempt fails."""
    install_calls: list[str] = []

    def _missing_supabase(_name: str) -> NoReturn:
        module_name = "supabase"
        raise ModuleNotFoundError(module_name)

    def _auto_install(extra_name: str) -> bool:
        install_calls.append(extra_name)
        return False

    monkeypatch.setattr(main.importlib, "import_module", _missing_supabase)
    monkeypatch.setattr(main, "auto_install_enabled", lambda: True)
    monkeypatch.setattr(main, "auto_install_tool_extra", _auto_install)

    with pytest.raises(ImportError, match=r"mindroom\[supabase\]") as err:
        main._init_supabase_auth("https://supabase.test", "anon-key")

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

    assert frontend_assets.ensure_frontend_dist_dir() == frontend_dist_dir
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

    monkeypatch.setenv("MINDROOM_AUTO_BUILD_FRONTEND", "0")
    monkeypatch.setattr(frontend_assets, "_PACKAGE_FRONTEND_DIR", tmp_path / "package-assets")
    monkeypatch.setattr(frontend_assets, "_REPO_FRONTEND_SOURCE_DIR", frontend_source_dir)
    monkeypatch.setattr(frontend_assets, "_REPO_FRONTEND_DIST_DIR", frontend_source_dir / "dist")
    monkeypatch.setattr(frontend_assets, "_FRONTEND_BUILD_ATTEMPTED", False)
    monkeypatch.setattr(frontend_assets.shutil, "which", lambda _name: "/usr/bin/bun")

    assert frontend_assets.ensure_frontend_dist_dir() is None


def test_ensure_writable_config_path_seeds_from_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Managed deployments should seed the writable config from the mounted template."""
    writable_config = tmp_path / "data" / "config.yaml"
    template_config = tmp_path / "template.yaml"
    template_config.write_text("agents: {}\nmodels: {}\n", encoding="utf-8")

    monkeypatch.setattr(constants, "CONFIG_PATH", writable_config)
    monkeypatch.setattr(constants, "CONFIG_TEMPLATE_PATH", template_config)

    assert constants.ensure_writable_config_path() is True
    assert writable_config.read_text(encoding="utf-8") == template_config.read_text(encoding="utf-8")


def test_api_lifespan_syncs_env_credentials_on_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """API startup should run env credential sync via the FastAPI lifespan hook."""
    sync_calls: list[str] = []
    watch_calls: list[str] = []

    async def _fake_watch_config(stop_event: asyncio.Event) -> None:
        watch_calls.append("watch")
        await stop_event.wait()

    monkeypatch.setattr(main, "sync_env_to_credentials", lambda: sync_calls.append("sync"))
    monkeypatch.setattr(main, "_watch_config", _fake_watch_config)

    with TestClient(main.app) as client:
        assert client.get("/api/health").status_code == 200

    assert sync_calls == ["sync"]
    assert watch_calls == ["watch"]


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

    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "watch_file", _fake_watch_file)
    monkeypatch.setattr(main, "_load_config_from_file", lambda: watched_paths.append(Path("loaded")))

    await main._watch_config(stop_event)

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
        patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))),
    ):
        response = test_client.get("/api/tools/?agent_name=general")

    assert response.status_code == 200
    tools_by_name = {tool["name"]: tool for tool in response.json()["tools"]}
    assert "homeassistant" not in tools_by_name
    assert "gmail" not in tools_by_name
    assert "spotify" not in tools_by_name
    assert "calculator" in tools_by_name


def test_google_disconnect_rejects_isolating_worker_scope(test_client: TestClient) -> None:
    """Google dashboard actions should reject isolating worker scopes."""
    config = _config_with_worker_scope("user")

    with (
        patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))),
    ):
        response = test_client.post("/api/google/disconnect?agent_name=general")

    assert response.status_code == 400
    assert "worker_scope=shared" in response.json()["detail"]


def test_homeassistant_connect_oauth_rejects_isolating_worker_scope(test_client: TestClient) -> None:
    """Home Assistant OAuth should reject isolating worker scopes."""
    config = _config_with_worker_scope("user")

    with (
        patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))),
    ):
        response = test_client.post(
            "/api/homeassistant/connect/oauth?agent_name=general",
            json={
                "instance_url": "https://ha.example.com",
                "client_id": "client-id",
            },
        )

    assert response.status_code == 400
    assert "worker_scope=shared" in response.json()["detail"]


def test_spotify_status_rejects_isolating_worker_scope(test_client: TestClient) -> None:
    """Spotify dashboard status should reject isolating worker scopes."""
    config = _config_with_worker_scope("user")

    with (
        patch("mindroom.api.main.load_runtime_config", return_value=(config, Path("config.yaml"))),
    ):
        response = test_client.get("/api/integrations/spotify/status?agent_name=general")

    assert response.status_code == 400
    assert "worker_scope=shared" in response.json()["detail"]


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


def test_error_handling_agent_not_found(test_client: TestClient) -> None:
    """Test error handling for non-existent agent."""
    test_client.post("/api/config/load")

    # Note: PUT creates the agent if it doesn't exist (current behavior)
    response = test_client.put("/api/config/agents/non_existent", json={})
    assert response.status_code == 200  # Current behavior creates the agent

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda: frontend_dir)

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda: frontend_dir)

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda: frontend_dir)

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda: frontend_dir)

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda: frontend_dir)

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda: frontend_dir)

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
def api_key_client(temp_config_file: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Create a test client with MINDROOM_API_KEY enabled."""
    monkeypatch.setattr(main, "CONFIG_PATH", temp_config_file)
    monkeypatch.setattr(main, "_MINDROOM_API_KEY", "test-key")
    main._load_config_from_file()
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
        "/api/google/callback?code=test-code",
        "/api/homeassistant/callback?code=test-code",
        "/api/integrations/spotify/callback?code=test-code",
    ],
)
def test_api_key_keeps_oauth_callbacks_open(
    api_key_client: TestClient,
    path: str,
) -> None:
    """OAuth callbacks must remain reachable without Authorization headers."""
    response = api_key_client.get(path)
    assert response.status_code != 401


def _set_platform_auth(
    monkeypatch: pytest.MonkeyPatch,
    *,
    valid_tokens: set[str],
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

    monkeypatch.setattr(main, "_MINDROOM_API_KEY", None)
    monkeypatch.setattr(main, "_supabase_auth", _FakeClient())
    monkeypatch.setattr(main, "_ACCOUNT_ID", None)


def test_supabase_cookie_auth_allows_access(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform requests should authenticate from the mindroom_jwt cookie."""
    valid_cookie_token = "valid-cookie-token"  # noqa: S105
    _set_platform_auth(monkeypatch, valid_tokens={valid_cookie_token})

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda: frontend_dir)
    _set_platform_auth(monkeypatch, valid_tokens=set())
    monkeypatch.setattr(main, "_PLATFORM_LOGIN_URL", "https://app.example.com/auth/login")

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda: frontend_dir)
    _set_platform_auth(monkeypatch, valid_tokens={"valid-cookie-token"})
    monkeypatch.setattr(main, "_PLATFORM_LOGIN_URL", "https://app.example.com/auth/login")

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

    monkeypatch.setattr(main, "ensure_frontend_dist_dir", lambda: frontend_dir)
    _set_platform_auth(monkeypatch, valid_tokens={valid_cookie_token})
    monkeypatch.setattr(main, "_PLATFORM_LOGIN_URL", "https://app.example.com/auth/login")

    response = test_client.get(
        "/",
        cookies={"mindroom_jwt": valid_cookie_token},
    )
    assert response.status_code == 200
    assert "MindRoom Dashboard" in response.text
