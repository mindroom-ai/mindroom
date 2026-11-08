"""A plugin's own settings, state directory, and config, for its HTTP routes and tools."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from mindroom.tool_system.plugin_identity import plugin_state_root
from mindroom.tool_system.plugins import PluginInactiveError, config_plugins
from mindroom.tool_system.runtime_context import get_detached_requester_context, get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.config.plugin import PluginEntryConfig
    from mindroom.constants import RuntimePaths


@dataclass(frozen=True, eq=False)
class PluginContext:
    """One plugin's view of the config a route or tool call runs under.

    ``settings``, ``config``, and ``state_root`` raise ``PluginInactiveError`` once the current config no longer loads
    the plugin, so a context held across a reload that disables or removes it fails closed.
    """

    plugin_name: str
    runtime_paths: RuntimePaths
    _config: Config = field(repr=False)
    _settings: Mapping[str, Any] = field(repr=False)
    # The config the runtime serves now for ``runtime_paths``, or None when it serves none for them.
    _current_config: Callable[[], Config | None] = field(repr=False)

    @property
    def settings(self) -> Mapping[str, Any]:
        """A read-only mapping of the plugin entry's ``settings`` in ``config``; nested values are this context's copy."""
        self._require_active()
        return self._settings

    @property
    def config(self) -> Config:
        """The config this context was made under."""
        self._require_active()
        return self._config

    @property
    def state_root(self) -> Path:
        """The plugin's own state directory under the storage root, created at mode 0700 on first use."""
        self._require_active()
        return plugin_state_root(self.runtime_paths.storage_root, self.plugin_name)

    def is_active(self) -> bool:
        """Return whether the current config still loads this plugin."""
        return _entry(self.plugin_name, self._current_config(), self.runtime_paths) is not None

    def _require_active(self) -> None:
        if not self.is_active():
            raise PluginInactiveError(self.plugin_name)


def _entry(plugin_name: str, config: Config | None, runtime_paths: RuntimePaths) -> PluginEntryConfig | None:
    return None if config is None else config_plugins(config, runtime_paths).plugin_entry(plugin_name)


def build_plugin_context(
    plugin_name: str,
    config: Config | None,
    runtime_paths: RuntimePaths,
    current_config: Callable[[], Config | None],
) -> PluginContext:
    """Return ``plugin_name``'s context under ``config``, raising ``PluginInactiveError`` when it does not load it."""
    entry = _entry(plugin_name, config, runtime_paths)
    if config is None or entry is None:
        raise PluginInactiveError(plugin_name)
    # A copy, so a write to a nested value reaches neither another context nor the config.
    settings = MappingProxyType(copy.deepcopy(entry.settings))
    return PluginContext(plugin_name, runtime_paths, config, settings, current_config)


def current_plugin_context(plugin_name: str) -> PluginContext:
    """Return ``plugin_name``'s context under the config of the current conversation or OpenAI-compatible API request."""
    if (runtime := get_tool_runtime_context()) is not None:
        return build_plugin_context(plugin_name, runtime.config, runtime.runtime_paths, lambda: runtime.current_config)
    if (detached := get_detached_requester_context()) is not None:
        return build_plugin_context(plugin_name, detached.config, detached.runtime_paths, detached.config_provider)
    msg = "current_plugin_context() needs a tool call in a Matrix conversation or an OpenAI-compatible API request"
    raise RuntimeError(msg)
