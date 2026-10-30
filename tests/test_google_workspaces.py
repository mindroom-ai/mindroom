"""Behavior of additional Google tools alongside existing connected accounts."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx
import pytest
from authlib.integrations import httpx_client as authlib_httpx_client

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.oauth.credential_lifecycle import (
    load_oauth_credentials_snapshot_sync,
    refresh_oauth_credentials,
    reset_oauth_credentials,
    resolve_oauth_credential_context,
)
from mindroom.oauth.google_gmail import google_gmail_oauth_provider
from mindroom.oauth.providers import OAuthClaimValidationError, OAuthTokenResult
from mindroom.oauth.registry import oauth_provider_registry
from mindroom.tool_system.agno_toolkits import toolkit_function_names
from mindroom.tool_system.google_workspaces import GoogleWorkspaceConfig, google_workspace_oauth_providers
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import config_plugins, prepare_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from tests.agent_tool_helpers import toolkit_offers
from tests.conftest import publish_test_plugins
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _plugin(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    plugin = tmp_path / "workspace_plugin"
    plugin.mkdir()
    (plugin / "mindroom.plugin.json").write_text(
        json.dumps(
            {
                "name": "workspace-example",
                "tools_module": "tools.py",
                "oauth_module": "oauth.py",
            },
        ),
    )
    (plugin / "workspace.py").write_text(
        "from mindroom.tool_system.google_workspaces import GoogleWorkspaceConfig\n"
        "WORKSPACE = GoogleWorkspaceConfig(name='secondary', display_name='Secondary', "
        "client_config_service='secondary_google_oauth_client', "
        "allowed_hosted_domains=('secondary.example',))\n",
    )
    (plugin / "tools.py").write_text(
        "from mindroom.tool_system.google_workspaces import register_google_workspace_tools\n"
        "from .workspace import WORKSPACE\n"
        "register_google_workspace_tools(WORKSPACE)\n",
    )
    (plugin / "oauth.py").write_text(
        "from mindroom.tool_system.google_workspaces import google_workspace_oauth_providers\n"
        "from .workspace import WORKSPACE\n"
        "def register_oauth_providers(settings, runtime_paths):\n"
        "    return google_workspace_oauth_providers(WORKSPACE)\n",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n")
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://chat.example",
            "MATRIX_HOMESERVER": "https://matrix.example",
        },
    )
    return Config(plugins=[str(plugin)]), paths


def test_workspace_tool_uses_its_own_chat_login_link(tmp_path: Path) -> None:
    """An additional tool must never reuse the default account's login or functions."""
    config, paths = _plugin(tmp_path)
    plugins = publish_test_plugins(config, paths)
    tool = get_tool_by_name("secondary_gmail", paths, worker_target=None, runs_in_worker=False, tools=plugins.tools)
    existing = get_tool_by_name("gmail", paths, worker_target=None, runs_in_worker=False, tools=plugins.tools)
    assert "secondary_get_latest_emails" in tool.functions
    assert "get_latest_emails" not in tool.functions
    assert "get_latest_emails" in existing.functions
    result = json.loads(tool.functions["secondary_get_latest_emails"].entrypoint())
    assert result["provider"] == "secondary_google_gmail"
    assert "/api/oauth/secondary_google_gmail/authorize" in result["connect_url"]


def test_combined_workspace_module_retains_plugin_ownership(tmp_path: Path) -> None:
    """One module serving as a plugin's tools and OAuth module registers plugin tools, never built-ins."""
    config, paths = _plugin(tmp_path)
    plugin = tmp_path / "workspace_plugin"
    manifest_path = plugin / "mindroom.plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["oauth_module"] = "tools.py"
    manifest_path.write_text(json.dumps(manifest))
    tools = plugin / "tools.py"
    tools.write_text(tools.read_text() + (plugin / "oauth.py").read_text())

    plugins = publish_test_plugins(config, paths)
    assert "secondary_google_gmail" in oauth_provider_registry(plugins, skip_broken_plugins=False)
    assert "secondary_gmail" in plugins.tools
    assert "secondary_gmail" not in builtin_tool_registry()
    assert "secondary_gmail" not in config_plugins(Config(), paths).tools


def test_failed_workspace_oauth_import_rolls_back_tool_registration(tmp_path: Path) -> None:
    """A combined module failing after tool registration skips the plugin and registers no built-in tool."""
    config, paths = _plugin(tmp_path)
    plugin = tmp_path / "workspace_plugin"
    oauth = plugin / "oauth.py"
    original = oauth.read_text()
    oauth.write_text((plugin / "tools.py").read_text() + original + "raise RuntimeError('broken plugin')\n")

    generation = prepare_plugins(config, paths)

    assert "broken plugin" in str(generation.skipped[0].error)
    assert generation.plugins == {}
    assert "secondary_gmail" not in builtin_tool_registry()


def test_workspace_oauth_discovery_in_fresh_process(tmp_path: Path) -> None:
    """A fresh process loading the plugin registers the built-ins outside the plugin's ownership."""
    _plugin(tmp_path)
    plugin = tmp_path / "workspace_plugin"
    manifest_path = plugin / "mindroom.plugin.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["oauth_module"] = "tools.py"
    manifest_path.write_text(json.dumps(manifest))
    tools = plugin / "tools.py"
    tools.write_text(tools.read_text() + (plugin / "oauth.py").read_text())
    script = """
import sys
from pathlib import Path
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system import registry
from mindroom.tool_system.plugins import config_plugins, prepare_plugins, publish_plugins

root = Path(sys.argv[1])
paths = resolve_runtime_paths(config_path=root / "config.yaml", storage_path=root / "data", process_env={})
config = Config(plugins=[str(root / "workspace_plugin")])
assert "gmail" not in registry._BUILTIN_TOOLS
generation = prepare_plugins(config, paths)
assert not generation.skipped, generation.skipped
publish_plugins(generation)
plugins = config_plugins(config, paths)
assert "secondary_google_gmail" in plugins.oauth_providers
assert "gmail" in registry._BUILTIN_TOOLS
assert "secondary_gmail" in plugins.tools
assert "secondary_gmail" not in registry._BUILTIN_TOOLS
assert "secondary_gmail" not in config_plugins(Config(), paths).tools
assert "gmail" in config_plugins(Config(), paths).tools
"""
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_workspace_client_does_not_fall_back_to_existing_client(tmp_path: Path) -> None:
    """A missing workspace client must not silently authorize the other organization."""
    config, paths = _plugin(tmp_path)
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials("google_oauth_client", {"client_id": "original", "client_secret": "original-secret"})
    plugins = publish_test_plugins(config, paths)
    providers = plugins.oauth_providers
    provider = providers["secondary_google_gmail"]
    assert provider.client_config(paths) is None
    manager.save_credentials(
        "secondary_google_oauth_client",
        {"client_id": "secondary", "client_secret": "secondary-secret"},
    )
    assert provider.client_config(paths).client_id == "secondary"
    assert providers["google_gmail"].client_config(paths).client_id == "original"


@pytest.mark.parametrize(
    "service",
    ["gmail", "google_calendar", "google_drive", "google_docs", "google_sheets", "google_tasks"],
)
def test_workspace_tool_filters_use_visible_names(tmp_path: Path, service: str) -> None:
    """Function filtering remains usable after adding a workspace prefix."""
    config, paths = _plugin(tmp_path)
    plugins = publish_test_plugins(config, paths)
    name = f"secondary_{service}"
    visible = plugins.tools.entries[name].function_names[0]
    tool = get_tool_by_name(
        name,
        paths,
        worker_target=None,
        runs_in_worker=False,
        tool_config_overrides={"include_tools": [visible]},
        tools=plugins.tools,
    )
    assert set(toolkit_function_names(tool)) == {visible}


@pytest.mark.parametrize(
    "service",
    ["gmail", "google_calendar", "google_drive", "google_docs", "google_sheets", "google_tasks"],
)
def test_workspace_tools_ignore_global_service_account(tmp_path: Path, service: str) -> None:
    """An explicitly connected workspace must not act as a global delegated account."""
    config, paths = _plugin(tmp_path)
    paths = replace(paths, process_env={**paths.process_env, "GOOGLE_SERVICE_ACCOUNT_FILE": "/absent/service.json"})
    plugins = publish_test_plugins(config, paths)
    tool = get_tool_by_name(
        f"secondary_{service}",
        paths,
        worker_target=None,
        runs_in_worker=False,
        tools=plugins.tools,
    )
    assert tool._should_fallback_to_original_auth() is False
    # The real entrypoint must return a connection instruction before touching Google.
    function_name, args = {
        "gmail": ("secondary_get_latest_emails", {}),
        "google_calendar": ("secondary_list_calendars", {}),
        "google_drive": ("secondary_google_drive_list_files", {}),
        "google_docs": ("secondary_google_docs_get_document", {"document_id": "test"}),
        "google_sheets": ("secondary_read_sheet", {"spreadsheet_id": "test", "spreadsheet_range": "A1"}),
        "google_tasks": ("secondary_google_tasks_list_tasks", {}),
    }[service]
    result = json.loads(toolkit_offers(tool, function_name).entrypoint(**args))
    assert result["oauth_connection_required"] is True
    assert result["provider"].startswith("secondary_google_")


def test_registration_and_second_account_preserve_existing_connection(tmp_path: Path) -> None:
    """Adding a workspace retains the original grant and credential generation exactly."""
    config, paths = _plugin(tmp_path)
    manager = get_runtime_credentials_manager(paths)
    original = google_gmail_oauth_provider()
    original_context = resolve_oauth_credential_context(original, paths, manager, None)
    manager.save_credentials("google_oauth_client", {"client_id": "original", "client_secret": "original-secret"})
    credentials = {
        "token": "original-access",
        "refresh_token": "original-refresh",
        "client_id": "original",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": list(original.scopes),
        "expires_at": 4102444800,
    }
    publish_oauth_credentials(original, credentials, credentials_manager=manager, worker_target=None)
    before = load_oauth_credentials_snapshot_sync(original_context)
    plugins = publish_test_plugins(config, paths)
    other = get_tool_by_name(
        "secondary_gmail",
        paths,
        worker_target=None,
        runs_in_worker=False,
        tools=plugins.tools,
    )
    assert other.creds is None
    provider = plugins.oauth_providers["secondary_google_gmail"]
    publish_oauth_credentials(
        provider,
        {**credentials, "token": "other-access"},
        credentials_manager=manager,
        worker_target=None,
    )
    original_tool = get_tool_by_name("gmail", paths, worker_target=None, runs_in_worker=False, tools=plugins.tools)
    assert original_tool.creds.token == "original-access"  # noqa: S105
    assert load_oauth_credentials_snapshot_sync(original_context) == before


@pytest.mark.asyncio
async def test_workspace_refresh_and_reset_do_not_change_default_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh uses the selected client, and disconnect resets only that account's store."""
    config, paths = _plugin(tmp_path)
    manager = get_runtime_credentials_manager(paths)
    for name, client_id in [("google_oauth_client", "original"), ("secondary_google_oauth_client", "secondary")]:
        manager.save_credentials(name, {"client_id": client_id, "client_secret": f"{client_id}-secret"})
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={"access_token": "refreshed-access", "token_type": "Bearer", "expires_in": 3600},
        )

    real_client = authlib_httpx_client.AsyncOAuth2Client
    monkeypatch.setattr(
        authlib_httpx_client,
        "AsyncOAuth2Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    plugins = publish_test_plugins(config, paths)
    providers = plugins.oauth_providers
    original, secondary = providers["google_gmail"], providers["secondary_google_gmail"]
    contexts = []
    for provider, client_id in [(original, "original"), (secondary, "secondary")]:
        publish_oauth_credentials(
            provider,
            {
                "token": f"{client_id}-access",
                "refresh_token": f"{client_id}-refresh",
                "token_uri": provider.token_url,
                "client_id": client_id,
                "scopes": list(provider.scopes),
                "expires_at": 1,
                "_oauth_claims_verified": True,
                "_oauth_claims": {"sub": client_id, "hd": "secondary.example", "email_verified": True},
            },
            credentials_manager=manager,
            worker_target=None,
        )
        contexts.append(resolve_oauth_credential_context(provider, paths, manager, None))
    before = load_oauth_credentials_snapshot_sync(contexts[0])
    refreshed = await refresh_oauth_credentials(contexts[1])
    assert refreshed["token"] == "refreshed-access"  # noqa: S105
    assert refreshed["refresh_token"] == "secondary-refresh"  # noqa: S105
    assert requests == [
        {
            "grant_type": ["refresh_token"],
            "refresh_token": ["secondary-refresh"],
            "client_id": ["secondary"],
            "client_secret": ["secondary-secret"],
        },
    ]
    await reset_oauth_credentials(contexts[1])
    assert load_oauth_credentials_snapshot_sync(contexts[1]).credentials is None
    assert load_oauth_credentials_snapshot_sync(contexts[0]) == before


@pytest.mark.parametrize("bad_domain", ["primary.example", None])
def test_wrong_workspace_identity_is_rejected(tmp_path: Path, bad_domain: str | None) -> None:
    """The expected domain must come from verified Google identity, not the chosen label."""
    _, paths = _plugin(tmp_path)
    config = GoogleWorkspaceConfig(
        name="secondary",
        display_name="Secondary",
        client_config_service="secondary_oauth_client",
        allowed_hosted_domains=("secondary.example",),
        services=("gmail",),
    )
    provider = google_workspace_oauth_providers(config)[0]
    with pytest.raises(OAuthClaimValidationError, match="hosted domain"):
        provider.validate_claims(
            OAuthTokenResult(token_data={}, claims={"hd": bad_domain}, claims_verified=True),
            paths,
        )
