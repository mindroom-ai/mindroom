"""OAuth provider registry built from core and plugin configuration."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.config.main import Config
from mindroom.logging_config import get_logger
from mindroom.oauth.google_calendar import google_calendar_oauth_provider
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.oauth.google_gmail import google_gmail_oauth_provider
from mindroom.oauth.google_sheets import google_sheets_oauth_provider
from mindroom.oauth.providers import OAuthProvider
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.plugins import _load_plugin_module

if TYPE_CHECKING:
    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_provider_cache_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class _ProviderCacheEntry:
    key: tuple[object, ...]
    providers: dict[str, OAuthProvider]


_provider_cache: _ProviderCacheEntry | None = None


def clear_oauth_provider_cache() -> None:
    """Drop cached OAuth provider registries after plugin runtime changes."""
    global _provider_cache
    with _provider_cache_lock:
        _provider_cache = None


def _builtin_oauth_providers() -> tuple[OAuthProvider, ...]:
    return (
        google_calendar_oauth_provider(),
        google_drive_oauth_provider(),
        google_gmail_oauth_provider(),
        google_sheets_oauth_provider(),
    )


def _module_oauth_provider_callback(module: Any) -> Any:  # noqa: ANN401
    callback = vars(module).get("register_oauth_providers")
    if not callable(callback):
        msg = "OAuth plugin module must define callable register_oauth_providers(settings, runtime_paths)"
        raise plugin_imports.PluginValidationError(msg)
    return callback


def _coerce_oauth_providers(registered: Any) -> list[OAuthProvider]:  # noqa: ANN401
    if registered is None:
        return []
    if not isinstance(registered, Iterable):
        msg = "register_oauth_providers() must return an iterable of OAuthProvider objects"
        raise plugin_imports.PluginValidationError(msg)
    providers: list[OAuthProvider] = []
    for provider in registered:
        if not isinstance(provider, OAuthProvider):
            msg = "register_oauth_providers() returned a non-OAuthProvider value"
            raise plugin_imports.PluginValidationError(msg)
        providers.append(provider)
    return providers


def _load_plugin_oauth_providers(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool,
) -> list[OAuthProvider]:
    providers: list[OAuthProvider] = []
    plugin_bases = plugin_imports._collect_plugin_bases(
        config.plugins,
        runtime_paths,
        skip_broken_plugins=skip_broken_plugins,
    )
    plugin_imports._reject_duplicate_plugin_manifest_names(plugin_bases)
    for plugin_base, plugin_entry, _plugin_order in plugin_bases:
        if plugin_base.oauth_module_path is None:
            continue
        try:
            module = _load_plugin_module(
                plugin_base.name,
                plugin_base.root,
                plugin_base.oauth_module_path,
                kind="oauth",
            )
            if module is None:
                continue
            callback = _module_oauth_provider_callback(module)
            registered = callback(plugin_entry.settings, runtime_paths)
            providers.extend(_coerce_oauth_providers(registered))
        except Exception as exc:
            if not skip_broken_plugins:
                raise
            plugin_imports._log_skipped_plugin_entry(plugin_entry.path, plugin_base.root, exc)
    return providers


def _provider_registry(providers: Iterable[OAuthProvider]) -> dict[str, OAuthProvider]:
    registry: dict[str, OAuthProvider] = {}
    duplicate_ids: set[str] = set()
    service_owners: dict[str, tuple[str, str]] = {}
    duplicate_services: list[str] = []
    for provider in providers:
        if provider.id in registry:
            duplicate_ids.add(provider.id)
        registry[provider.id] = provider
        provider_services = [
            ("credential_service", provider.credential_service),
            ("tool_config_service", provider.tool_config_service),
            *[("client_config_service", service) for service in provider.all_client_config_services],
        ]
        for role, service_name in provider_services:
            if service_name is None:
                continue
            owner = service_owners.get(service_name)
            if owner is None:
                service_owners[service_name] = (provider.id, role)
                continue
            owner_provider_id, owner_role = owner
            if owner_role == "client_config_service" and role == "client_config_service":
                continue
            duplicate_services.append(
                f"{service_name} ({owner_provider_id}.{owner_role}, {provider.id}.{role})",
            )
    if duplicate_ids:
        duplicate_list = ", ".join(sorted(duplicate_ids))
        msg = f"Duplicate OAuth provider id(s): {duplicate_list}"
        raise plugin_imports.PluginValidationError(msg)
    if duplicate_services:
        duplicate_list = ", ".join(sorted(duplicate_services))
        msg = f"Duplicate OAuth provider service name(s): {duplicate_list}"
        raise plugin_imports.PluginValidationError(msg)
    _reject_tool_service_collisions(registry.values())
    return registry


def _registered_tool_service_auth_providers() -> dict[str, str | None]:
    import mindroom.tools as _mindroom_tools  # noqa: F401, PLC0415

    return {tool_name: metadata.auth_provider for tool_name, metadata in TOOL_METADATA.items()}


def _reject_tool_service_collisions(providers: Iterable[OAuthProvider]) -> None:
    tool_auth_providers = _registered_tool_service_auth_providers()
    collisions: list[str] = []
    for provider in providers:
        if provider.credential_service in tool_auth_providers:
            collisions.append(f"{provider.credential_service} ({provider.id}.credential_service, tool service)")
        if provider.tool_config_service is None:
            continue
        if provider.tool_config_service not in tool_auth_providers:
            continue
        tool_auth_provider = tool_auth_providers[provider.tool_config_service]
        if tool_auth_provider != provider.id:
            collisions.append(f"{provider.tool_config_service} ({provider.id}.tool_config_service, tool service)")
    if collisions:
        collision_list = ", ".join(sorted(collisions))
        msg = f"OAuth provider service name(s) overlap existing tool service(s): {collision_list}"
        raise plugin_imports.PluginValidationError(msg)


def load_oauth_providers(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool = True,
) -> dict[str, OAuthProvider]:
    """Return all OAuth providers available for one runtime config."""
    global _provider_cache
    cache_key = ("config", id(config), runtime_paths, skip_broken_plugins)
    with _provider_cache_lock:
        if _provider_cache is not None and _provider_cache.key == cache_key:
            return _provider_cache.providers
    plugin_providers = _load_plugin_oauth_providers(
        config,
        runtime_paths,
        skip_broken_plugins=skip_broken_plugins,
    )
    providers = (*_builtin_oauth_providers(), *plugin_providers)
    registry = _provider_registry(providers)
    with _provider_cache_lock:
        _provider_cache = _ProviderCacheEntry(cache_key, registry)
    logger.debug("Loaded OAuth providers", providers=sorted(registry))
    return registry


def load_oauth_providers_for_snapshot(
    snapshot: ApiSnapshot,
    *,
    skip_broken_plugins: bool = True,
) -> dict[str, OAuthProvider]:
    """Return OAuth providers cached by one API config snapshot."""
    global _provider_cache

    cache_key = ("snapshot", snapshot.generation, id(snapshot), snapshot.runtime_paths, skip_broken_plugins)
    with _provider_cache_lock:
        if _provider_cache is not None and _provider_cache.key == cache_key:
            return _provider_cache.providers
    if snapshot.runtime_config is not None:
        config = snapshot.runtime_config
    else:
        config = Config.model_validate(snapshot.config_data or {}, context={"runtime_paths": snapshot.runtime_paths})
    plugin_providers = _load_plugin_oauth_providers(
        config,
        snapshot.runtime_paths,
        skip_broken_plugins=skip_broken_plugins,
    )
    providers = (*_builtin_oauth_providers(), *plugin_providers)
    registry = _provider_registry(providers)
    with _provider_cache_lock:
        _provider_cache = _ProviderCacheEntry(cache_key, registry)
    logger.debug("Loaded OAuth providers", providers=sorted(registry))
    return registry
