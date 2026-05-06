"""Tests for OAuth provider registry loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.oauth import registry as oauth_registry
from mindroom.oauth.providers import OAuthProvider

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
        credential_service=provider_id,
        client_config_services=(f"{provider_id}_oauth_client",),
    )


def test_load_oauth_provider_registry_caches_loaded_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The shared loader should own cache reads, provider merge, validation, and cache writes."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config()
    builtin_provider = _provider("builtin_provider")
    plugin_provider = _provider("plugin_provider")
    load_calls: list[tuple[Config, RuntimePaths, bool]] = []

    def load_plugin_providers(
        received_config: Config,
        received_runtime_paths: RuntimePaths,
        *,
        skip_broken_plugins: bool,
    ) -> list[OAuthProvider]:
        load_calls.append((received_config, received_runtime_paths, skip_broken_plugins))
        return [plugin_provider]

    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (builtin_provider,))
    monkeypatch.setattr(oauth_registry, "_load_plugin_oauth_providers", load_plugin_providers)
    monkeypatch.setattr(oauth_registry, "_reject_tool_service_collisions", lambda _providers: None)

    cache_key = ("config", id(config), runtime_paths, True)
    oauth_registry.clear_oauth_provider_cache()
    try:
        providers = oauth_registry._load_oauth_provider_registry(
            config,
            runtime_paths,
            cache_key,
            skip_broken_plugins=True,
        )
        cached_providers = oauth_registry._load_oauth_provider_registry(
            config,
            runtime_paths,
            cache_key,
            skip_broken_plugins=True,
        )
    finally:
        oauth_registry.clear_oauth_provider_cache()

    assert providers is cached_providers
    assert providers == {
        "builtin_provider": builtin_provider,
        "plugin_provider": plugin_provider,
    }
    assert load_calls == [(config, runtime_paths, True)]


def test_load_oauth_providers_uses_config_cache_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct config loading should preserve the existing cache key shape."""
    runtime_paths = _runtime_paths(tmp_path)
    config = Config()
    expected_providers = {"provider": _provider("provider")}
    calls: list[tuple[Config, RuntimePaths, tuple[object, ...], bool]] = []

    def load_registry(
        received_config: Config,
        received_runtime_paths: RuntimePaths,
        cache_key: tuple[object, ...],
        *,
        skip_broken_plugins: bool,
    ) -> dict[str, OAuthProvider]:
        calls.append((received_config, received_runtime_paths, cache_key, skip_broken_plugins))
        return expected_providers

    monkeypatch.setattr(oauth_registry, "_load_oauth_provider_registry", load_registry)

    providers = oauth_registry.load_oauth_providers(config, runtime_paths, skip_broken_plugins=False)

    assert providers is expected_providers
    assert calls == [(config, runtime_paths, ("config", id(config), runtime_paths, False), False)]


def test_load_oauth_providers_for_snapshot_hydrates_config_before_shared_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Snapshot loading should preserve hydration and snapshot-specific cache key shape."""
    runtime_paths = _runtime_paths(tmp_path)
    snapshot = ApiSnapshot(
        generation=7,
        runtime_paths=runtime_paths,
        config_data={"agents": {}},
        runtime_config=None,
    )
    expected_providers = {"provider": _provider("provider")}
    calls: list[tuple[Config, RuntimePaths, tuple[object, ...], bool]] = []

    def load_registry(
        received_config: Config,
        received_runtime_paths: RuntimePaths,
        cache_key: tuple[object, ...],
        *,
        skip_broken_plugins: bool,
    ) -> dict[str, OAuthProvider]:
        calls.append((received_config, received_runtime_paths, cache_key, skip_broken_plugins))
        return expected_providers

    monkeypatch.setattr(oauth_registry, "_load_oauth_provider_registry", load_registry)

    providers = oauth_registry.load_oauth_providers_for_snapshot(snapshot, skip_broken_plugins=False)

    assert providers is expected_providers
    assert len(calls) == 1
    received_config, received_runtime_paths, cache_key, skip_broken_plugins = calls[0]
    assert isinstance(received_config, Config)
    assert received_config.authored_model_dump() == {"agents": {}}
    assert received_runtime_paths == runtime_paths
    assert cache_key == ("snapshot", 7, id(snapshot), runtime_paths, False)
    assert skip_broken_plugins is False
