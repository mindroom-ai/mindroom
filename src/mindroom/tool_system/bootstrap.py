"""Tool-system bootstrap orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def activate_tool_registry(runtime_paths: RuntimePaths, config: Config) -> None:
    """Explicitly switch the process-global registry to one committed config."""
    import mindroom.tools  # noqa: F401, PLC0415
    from mindroom.tool_system.plugins import reload_plugins  # noqa: PLC0415

    reload_plugins(config, runtime_paths, skip_broken_plugins=True)


def ensure_tool_registry_loaded(
    runtime_paths: RuntimePaths,
    config: Config | None = None,
    *,
    load_plugin_tools: bool = True,
) -> None:
    """Load core tools, then sync MCP and optional plugin tools when config is provided."""
    import mindroom.tools  # noqa: F401, PLC0415  # Explicit built-in manifest loads on demand.

    if config is None:
        return

    if load_plugin_tools:
        from mindroom.tool_system.plugins import (  # noqa: PLC0415
            active_plugin_hook_registry,
            reload_plugins,
        )

        if active_plugin_hook_registry(config, runtime_paths) is None:
            reload_plugins(config, runtime_paths, skip_broken_plugins=True)

    from mindroom.mcp.registry import sync_mcp_tool_registry  # noqa: PLC0415

    sync_mcp_tool_registry(config)
