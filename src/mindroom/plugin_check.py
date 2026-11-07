"""Isolated compatibility checks for one external MindRoom plugin."""

from __future__ import annotations

import ast
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

# The plugin API, as docs/plugins.md documents it: the modules a plugin may import whole, and the names it may import
# from other modules. Every other `mindroom` module can change in any release.
_PLUGIN_API = frozenset(
    {
        "mindroom.custom_tools.google_service.GoogleCloudToolkit",
        "mindroom.memory.refresh_agent_memory_search",
        "mindroom.memory.write_scope_markdown_file",
        "mindroom.plugin_api",
        "mindroom.plugin_http",
    },
)


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
                if plugin.routes_error is not None:
                    raise plugin.routes_error
                tool_names = tuple(sorted(plugin.tools))
                for tool_name in tool_names:
                    factory = plugin.tools[tool_name].factory
                    # Every plugin tool registration sets its factory.
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


def imports_outside_plugin_api(plugin_root: Path) -> list[str]:
    """Statically list each import from ``mindroom`` outside the plugin API in the plugin's Python files, as ``path:line: name``."""
    found: list[tuple[Path, int, str]] = []
    for directory, subdirectories, file_names in plugin_root.walk():
        # Installed packages, such as a virtual environment's copy of MindRoom, and hidden directories are not plugin code.
        subdirectories[:] = [name for name in subdirectories if name != "site-packages" and not name.startswith(".")]
        for path in (directory / name for name in file_names if name.endswith(".py")):
            try:
                tree = ast.parse(path.read_bytes(), filename=str(path))
            except (OSError, SyntaxError):
                # A file Python cannot read or parse runs no imports, and `check_plugin` fails on any such module the plugin loads.
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [(alias.lineno, alias.name) for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [(alias.lineno, f"{node.module}.{alias.name}") for alias in node.names]
                else:
                    continue
                found.extend(
                    (path.relative_to(plugin_root), line, name)
                    for line, name in names
                    if f"{name}.".startswith("mindroom.")
                    and not any(f"{name}.".startswith(f"{public}.") for public in _PLUGIN_API)
                )
    return [f"{path}:{line}: {name}" for path, line, name in sorted(found)]
