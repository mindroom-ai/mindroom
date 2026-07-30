"""Isolated compatibility checks for one external MindRoom plugin."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.hooks import HookRegistry, get_hook_metadata
from mindroom.oauth.registry import load_oauth_providers
from mindroom.tool_system.catalog import TOOL_METADATA, clear_resolved_tool_state_cache, ensure_tool_registry_loaded
from mindroom.tool_system.plugins import isolated_plugin_runtime


@dataclass(frozen=True, slots=True)
class _PluginCheckResult:
    """Successful plugin compatibility-check summary."""

    name: str
    tool_names: tuple[str, ...]
    hook_names: tuple[str, ...]
    skill_directories: tuple[str, ...]


def check_plugin(plugin_path: Path) -> _PluginCheckResult:
    """Strictly load one plugin through every runtime registration surface."""
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

        ensure_tool_registry_loaded(runtime_paths)
        original_tool_names = set(TOOL_METADATA)
        try:
            with isolated_plugin_runtime(
                config,
                runtime_paths,
                skip_broken_plugins=False,
            ) as loaded_plugins:
                HookRegistry.from_plugins(loaded_plugins)
                load_oauth_providers(
                    config,
                    runtime_paths,
                    skip_broken_plugins=False,
                )

                if len(loaded_plugins) != 1:
                    msg = f"Expected one loaded plugin, got {len(loaded_plugins)}"
                    raise ValueError(msg)
                [plugin] = loaded_plugins
                hook_names = tuple(
                    sorted(
                        metadata.hook_name
                        for callback in plugin.discovered_hooks
                        if (metadata := get_hook_metadata(callback)) is not None
                    ),
                )
                return _PluginCheckResult(
                    name=plugin.name,
                    tool_names=tuple(sorted(set(TOOL_METADATA) - original_tool_names)),
                    hook_names=hook_names,
                    skill_directories=tuple(
                        sorted(str(skill_directory.relative_to(plugin.root)) for skill_directory in plugin.skill_dirs),
                    ),
                )
        finally:
            clear_resolved_tool_state_cache()
