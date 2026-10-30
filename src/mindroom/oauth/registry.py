"""OAuth provider registry built from core and plugin configuration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from mindroom.config.main import Config
from mindroom.logging_config import get_logger
from mindroom.mcp.oauth import mcp_oauth_providers_for_config
from mindroom.oauth.atlassian import atlassian_oauth_provider
from mindroom.oauth.github import github_oauth_provider
from mindroom.oauth.google import google_oauth_providers
from mindroom.oauth.microsoft import microsoft_oauth_providers
from mindroom.oauth.providers import OAuthProvider
from mindroom.slack.oauth import slack_oauth_provider
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.plugins import config_plugins

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.api.config_lifecycle import ApiSnapshot
    from mindroom.tool_system.plugins import ConfigPlugins
    from mindroom.tool_system.registry import ToolRegistry

logger = get_logger(__name__)


def _builtin_oauth_providers() -> tuple[OAuthProvider, ...]:
    return (
        atlassian_oauth_provider(),
        github_oauth_provider(),
        *google_oauth_providers(),
        *microsoft_oauth_providers(),
        slack_oauth_provider(),
    )


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


def _plugin_oauth_providers(plugins: ConfigPlugins, *, skip_broken_plugins: bool) -> list[OAuthProvider]:
    """Call each loaded plugin's OAuth callback with the config's settings for it."""
    providers: list[OAuthProvider] = []
    for plugin_root, plugin_entry, callback in plugins.plugin_oauth_callbacks():
        try:
            providers.extend(_coerce_oauth_providers(callback(plugin_entry.settings, plugins.runtime_paths)))
        except (Exception, SystemExit) as exc:
            if not skip_broken_plugins:
                if isinstance(exc, SystemExit):
                    msg = f"Plugin OAuth provider registration failed for {plugin_root}: {exc}"
                    raise plugin_imports.PluginValidationError(msg) from exc
                raise
            plugin_imports._log_skipped_plugin_entry(plugin_entry.path, plugin_root, exc)
    return providers


def _provider_registry(providers: Iterable[OAuthProvider], tools: ToolRegistry) -> dict[str, OAuthProvider]:
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
            *[("client_config_service", service) for service in provider.client_config_services],
            *[("shared_client_config_service", service) for service in provider.shared_client_config_services],
        ]
        for role, service_name in provider_services:
            if service_name is None:
                continue
            owner = service_owners.get(service_name)
            if owner is None:
                service_owners[service_name] = (provider.id, role)
                continue
            owner_provider_id, owner_role = owner
            if owner_role == "shared_client_config_service" and role == "shared_client_config_service":
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
    _reject_tool_service_collisions(registry.values(), tools)
    return registry


def _reject_tool_service_collisions(providers: Iterable[OAuthProvider], tools: ToolRegistry) -> None:
    tool_auth_providers = {tool_name: metadata.auth_provider for tool_name, metadata in tools.entries.items()}
    collisions: list[str] = []
    for provider in providers:
        provider_services = [
            ("credential_service", provider.credential_service),
            *[("client_config_service", service) for service in provider.all_client_config_services],
        ]
        for role, service_name in provider_services:
            if service_name in tool_auth_providers:
                collisions.append(f"{service_name} ({provider.id}.{role}, tool service)")
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


def oauth_provider_registry(plugins: ConfigPlugins, *, skip_broken_plugins: bool = True) -> dict[str, OAuthProvider]:
    """Return the built-in, MCP, and plugin OAuth providers of one config's plugin view; no plugin code is loaded."""
    plugin_providers = _plugin_oauth_providers(plugins, skip_broken_plugins=skip_broken_plugins)
    mcp_providers = tuple(mcp_oauth_providers_for_config(plugins.mcp_servers))
    registry = _provider_registry((*_builtin_oauth_providers(), *mcp_providers, *plugin_providers), plugins.tools)
    logger.debug("Loaded OAuth providers", providers=sorted(registry))
    return registry


def snapshot_oauth_providers(snapshot: ApiSnapshot) -> Mapping[str, OAuthProvider]:
    """Return the OAuth providers of one API config snapshot."""
    config = snapshot.runtime_config
    if config is None:
        # Only pre-first-load snapshots lack a runtime config; they expose built-in providers only.
        config = Config.model_validate({}, context={"runtime_paths": snapshot.runtime_paths})
    return config_plugins(config, snapshot.runtime_paths).oauth_providers
