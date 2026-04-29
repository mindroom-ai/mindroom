"""OAuth provider registry built from core and plugin configuration."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger
from mindroom.oauth.google_drive import google_drive_oauth_provider
from mindroom.oauth.providers import OAuthProvider
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.plugins import _load_plugin_module

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


def _builtin_oauth_providers() -> tuple[OAuthProvider, ...]:
    return (google_drive_oauth_provider(),)


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
    for provider in providers:
        if provider.id in registry:
            duplicate_ids.add(provider.id)
        registry[provider.id] = provider
    if duplicate_ids:
        duplicate_list = ", ".join(sorted(duplicate_ids))
        msg = f"Duplicate OAuth provider id(s): {duplicate_list}"
        raise plugin_imports.PluginValidationError(msg)
    return registry


def load_oauth_providers(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    skip_broken_plugins: bool = True,
) -> dict[str, OAuthProvider]:
    """Return all OAuth providers available for one runtime config."""
    plugin_providers = _load_plugin_oauth_providers(
        config,
        runtime_paths,
        skip_broken_plugins=skip_broken_plugins,
    )
    providers = (*_builtin_oauth_providers(), *plugin_providers)
    registry = _provider_registry(providers)
    logger.debug("Loaded OAuth providers", providers=sorted(registry))
    return registry
