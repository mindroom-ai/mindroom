"""Tests for OAuth provider registry loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.oauth import registry as oauth_registry
from mindroom.oauth.providers import OAuthProvider
from mindroom.tool_system.plugins import config_plugins

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _provider(provider_id: str) -> OAuthProvider:
    return OAuthProvider(
        id=provider_id,
        display_name=provider_id,
        authorization_url="https://auth.example.test/authorize",
        token_url="https://auth.example.test/token",  # noqa: S106
        scopes=("read",),
        credential_service=f"{provider_id}_oauth",
        client_config_services=(f"{provider_id}_oauth_client",),
    )


def test_builtin_oauth_registry_lists_every_builtin_provider_once_in_order() -> None:
    """The built-in registry lists each built-in provider once, in a stable order."""
    assert [provider.id for provider in oauth_registry._builtin_oauth_providers()] == [
        "atlassian",
        "github",
        "google_calendar",
        "google_cloud",
        "google_docs",
        "google_drive",
        "google_gmail",
        "google_sheets",
        "google_tasks",
        "microsoft_files",
        "microsoft_sharepoint",
        "outlook_mail",
        "outlook_calendar",
        "microsoft_todo",
        "microsoft_teams",
        "microsoft_meetings",
        "microsoft_onenote",
        "microsoft_people",
        "slack",
        "spotify",
    ]


def test_builtin_oauth_registry_includes_github() -> None:
    """GitHub should participate in built-in discovery and service-collision checks."""
    providers = {provider.id: provider for provider in oauth_registry._builtin_oauth_providers()}

    assert providers["github"].credential_service == "github_oauth"
    assert providers["github"].tool_config_service == "github"


def test_oauth_provider_rejects_token_suffix_for_tool_config_service() -> None:
    """Dashboard-editable provider settings cannot use the reserved OAuth token suffix."""
    with pytest.raises(ValueError, match=r"tool_config_service.*must not end with '_oauth'"):
        OAuthProvider(
            id="demo",
            display_name="Demo",
            authorization_url="https://auth.example.test/authorize",
            token_url="https://auth.example.test/token",  # noqa: S106
            scopes=("read",),
            credential_service="demo_oauth",
            tool_config_service="demo_settings_oauth",
            client_config_services=("demo_oauth_client",),
        )


def test_a_config_view_keeps_its_providers_and_merges_built_in_and_plugin_ones(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One config's providers are built once from the built-in and plugin providers."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config()
    builtin_provider = _provider("builtin_provider")
    plugin_provider = _provider("plugin_provider")
    load_calls: list[bool] = []

    def plugin_providers(_plugins: object, *, skip_broken_plugins: bool) -> list[OAuthProvider]:
        load_calls.append(skip_broken_plugins)
        return [plugin_provider]

    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (builtin_provider,))
    monkeypatch.setattr(oauth_registry, "_plugin_oauth_providers", plugin_providers)
    plugins = config_plugins(config, runtime_paths)

    providers = plugins.oauth_providers

    assert providers is plugins.oauth_providers
    assert dict(providers) == {
        "builtin_provider": builtin_provider,
        "plugin_provider": plugin_provider,
    }
    assert load_calls == [True]


def test_snapshot_providers_are_the_snapshot_config_s_providers(tmp_path: Path) -> None:
    """A snapshot's providers are its runtime config's providers."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = Config.validate_with_runtime({"agents": {}}, runtime_paths)
    snapshot = ApiSnapshot(
        generation=7,
        runtime_paths=runtime_paths,
        runtime_config=runtime_config,
    )

    providers = oauth_registry.snapshot_oauth_providers(snapshot)

    assert providers is config_plugins(runtime_config, runtime_paths).oauth_providers


def test_a_snapshot_before_the_first_config_load_has_the_built_in_providers(tmp_path: Path) -> None:
    """Snapshots published before the first config load expose built-in providers only."""
    runtime_paths = _runtime_paths(tmp_path)
    snapshot = ApiSnapshot(
        generation=0,
        runtime_paths=runtime_paths,
        runtime_config=None,
    )

    providers = oauth_registry.snapshot_oauth_providers(snapshot)

    assert set(providers) == {provider.id for provider in oauth_registry._builtin_oauth_providers()}
