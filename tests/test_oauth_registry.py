"""Tests for OAuth provider registry loading."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.config.main import Config, RuntimeConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.oauth import registry as oauth_registry
from mindroom.oauth.providers import OAuthProvider
from mindroom.tool_system.declarations import ToolCategory, ToolMetadata
from mindroom.tool_system.registry_state import TOOL_METADATA
from tests.config_test_utils import runtime_config_from_data

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
    builtin_provider = _provider("builtin_provider")
    plugin_provider = _provider("plugin_provider")
    config = RuntimeConfig.from_authored(
        Config(),
        runtime_paths,
        plugin_oauth_providers=(plugin_provider,),
    )

    monkeypatch.setattr(oauth_registry, "_builtin_oauth_providers", lambda: (builtin_provider,))

    cache_key = ("config", id(config), runtime_paths)
    oauth_registry.clear_oauth_provider_cache()
    try:
        providers = oauth_registry._load_oauth_provider_registry(
            config,
            runtime_paths,
            cache_key,
        )
        cached_providers = oauth_registry._load_oauth_provider_registry(
            config,
            runtime_paths,
            cache_key,
        )
    finally:
        oauth_registry.clear_oauth_provider_cache()

    assert providers is cached_providers
    assert providers == {
        "builtin_provider": builtin_provider,
        "plugin_provider": plugin_provider,
    }


def test_load_oauth_providers_uses_config_cache_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Direct config loading should cache by the exact runtime config object."""
    runtime_paths = _runtime_paths(tmp_path)
    config = runtime_config_from_data({}, runtime_paths)
    expected_providers = {"provider": _provider("provider")}
    calls: list[tuple[RuntimeConfig, RuntimePaths, tuple[object, ...]]] = []

    def load_registry(
        received_config: RuntimeConfig,
        received_runtime_paths: RuntimePaths,
        cache_key: tuple[object, ...],
    ) -> dict[str, OAuthProvider]:
        calls.append((received_config, received_runtime_paths, cache_key))
        return expected_providers

    monkeypatch.setattr(oauth_registry, "_load_oauth_provider_registry", load_registry)

    providers = oauth_registry.load_oauth_providers(config, runtime_paths)

    assert providers is expected_providers
    assert calls == [(config, runtime_paths, ("config", id(config), runtime_paths))]


def test_load_oauth_providers_for_snapshot_uses_runtime_config_and_cache_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Snapshot loading should pass the snapshot's runtime config and cache key shape through."""
    runtime_paths = _runtime_paths(tmp_path)
    runtime_config = runtime_config_from_data({"agents": {}}, runtime_paths)
    snapshot = ApiSnapshot(
        generation=7,
        runtime_paths=runtime_paths,
        config_data={"agents": {}},
        runtime_config=runtime_config,
    )
    expected_providers = {"provider": _provider("provider")}
    calls: list[tuple[RuntimeConfig, RuntimePaths, tuple[object, ...]]] = []

    def load_registry(
        received_config: RuntimeConfig,
        received_runtime_paths: RuntimePaths,
        cache_key: tuple[object, ...],
    ) -> dict[str, OAuthProvider]:
        calls.append((received_config, received_runtime_paths, cache_key))
        return expected_providers

    monkeypatch.setattr(oauth_registry, "_load_oauth_provider_registry", load_registry)

    providers = oauth_registry.load_oauth_providers_for_snapshot(snapshot)

    assert providers is expected_providers
    assert calls == [(runtime_config, runtime_paths, ("snapshot", 7, id(snapshot), runtime_paths))]


def test_load_oauth_providers_for_pre_load_snapshot_falls_back_to_empty_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Snapshots published before the first config load should use an empty config."""
    runtime_paths = _runtime_paths(tmp_path)
    snapshot = ApiSnapshot(
        generation=0,
        runtime_paths=runtime_paths,
        config_data={},
        runtime_config=None,
    )
    expected_providers = {"provider": _provider("provider")}
    calls: list[RuntimeConfig] = []

    def load_registry(
        received_config: RuntimeConfig,
        received_runtime_paths: RuntimePaths,
        cache_key: tuple[object, ...],
    ) -> dict[str, OAuthProvider]:
        del received_runtime_paths, cache_key
        calls.append(received_config)
        return expected_providers

    monkeypatch.setattr(oauth_registry, "_load_oauth_provider_registry", load_registry)

    providers = oauth_registry.load_oauth_providers_for_snapshot(snapshot)

    assert providers is expected_providers
    assert len(calls) == 1
    assert calls[0].authored_model_dump() == {}


def test_pinned_oauth_provider_validation_uses_snapshot_tool_metadata(tmp_path: Path) -> None:
    """A newer global tool generation must not invalidate an older runtime snapshot."""
    runtime_paths = _runtime_paths(tmp_path)
    provider = _provider("pinned_plugin_service")
    runtime_config = RuntimeConfig.from_authored(
        Config(),
        runtime_paths,
        plugin_oauth_providers=(provider,),
    )
    previous_metadata = TOOL_METADATA.get(provider.credential_service)
    TOOL_METADATA[provider.credential_service] = ToolMetadata(
        name=provider.credential_service,
        display_name="Newer colliding tool",
        description="Only present in the newer live generation.",
        category=ToolCategory.INTEGRATIONS,
    )
    oauth_registry.clear_oauth_provider_cache()
    try:
        providers = oauth_registry.load_oauth_providers(runtime_config, runtime_paths)
    finally:
        oauth_registry.clear_oauth_provider_cache()
        if previous_metadata is None:
            TOOL_METADATA.pop(provider.credential_service, None)
        else:
            TOOL_METADATA[provider.credential_service] = previous_metadata

    assert providers[provider.id] is provider
