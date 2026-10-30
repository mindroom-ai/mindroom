"""Isolated compatibility checks for one external MindRoom plugin."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import get_hook_metadata
from mindroom.oauth.registry import oauth_provider_registry
from mindroom.tool_system.agno_toolkits import adapt_callable
from mindroom.tool_system.authoring import Toolkit, tool_attributes
from mindroom.tool_system.catalog import load_toolkit_class
from mindroom.tool_system.plugins import ConfigPlugins, dispose_plugins, staged_plugins


@dataclass(frozen=True, slots=True)
class _PluginCheckResult:
    """Successful plugin compatibility-check summary."""

    name: str
    tool_names: tuple[str, ...]
    hook_names: tuple[str, ...]
    automation_names: tuple[str, ...]
    skill_directories: tuple[str, ...]


def _derive_owned_functions(tool_name: str, tool_class: type) -> None:
    """Derive each ``@tool`` function of an owned toolkit class as an agent build does, without constructing the class.

    A function that does not derive, such as one whose annotation names a type imported only for type checking, leaves
    every agent without the toolkit. An Agno toolkit names its functions only once constructed, so its build checks them.
    """
    if not issubclass(tool_class, Toolkit):
        return
    for attribute in tool_attributes(tool_class):
        try:
            # Bound as an instance binds it, to a stand-in no derivation reads, so the schema leaves out ``self``.
            adapt_callable(inspect.getattr_static(tool_class, attribute).__get__(object(), tool_class))
        except Exception as error:
            msg = f"Tool {tool_name!r} cannot offer its function {attribute!r}: {error}"
            raise ValueError(msg) from error


def check_plugin(plugin_path: Path) -> _PluginCheckResult:
    """Strictly load one plugin through every runtime registration surface, leaving the process's plugins as they were."""
    plugin_root = plugin_path.expanduser().resolve()
    with TemporaryDirectory(prefix="mindroom-plugin-check-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        runtime_paths = resolve_runtime_paths(
            config_path=temporary_root / "config.yaml",
            storage_path=temporary_root / "data",
            process_env={},
        )
        config = Config.model_validate(
            {"plugins": [{"path": str(plugin_root)}]},
            context={"runtime_paths": runtime_paths},
        )

        # The checks run while the plugin's modules stay importable, as they are once it is published.
        with staged_plugins(config, runtime_paths) as generation:
            try:
                if generation.skipped:
                    raise generation.skipped[0].error
                plugins = ConfigPlugins.of(generation, config, runtime_paths)
                automations = plugins.hooks.automations
                if automations.collisions:
                    msg = f"Automation names already taken: {', '.join(automations.collisions)}"
                    raise ValueError(msg)
                oauth_provider_registry(plugins, skip_broken_plugins=False)

                if len(plugins.loaded_plugins) != 1:
                    msg = f"Expected one loaded plugin, got {len(plugins.loaded_plugins)}"
                    raise ValueError(msg)
                [plugin] = plugins.loaded_plugins
                tool_names = tuple(sorted(plugin.tools))
                for tool_name in tool_names:
                    factory = plugin.tools[tool_name].factory
                    # The registration decorator gives every plugin tool its factory.
                    assert factory is not None
                    try:
                        tool_class = load_toolkit_class(tool_name, factory)
                    except ImportError:
                        # A factory may import a package the user installs after the plugin; its build checks it then.
                        continue
                    _derive_owned_functions(tool_name, tool_class)
                hook_names = tuple(
                    sorted(
                        metadata.hook_name
                        for callback in plugin.discovered_hooks
                        if (metadata := get_hook_metadata(callback)) is not None
                    ),
                )
                return _PluginCheckResult(
                    name=plugin.name,
                    tool_names=tool_names,
                    hook_names=hook_names,
                    automation_names=tuple(sorted(automations.plugin_definitions)),
                    skill_directories=tuple(
                        sorted(str(skill_directory.relative_to(plugin.root)) for skill_directory in plugin.skill_dirs),
                    ),
                )
            finally:
                dispose_plugins(generation)
