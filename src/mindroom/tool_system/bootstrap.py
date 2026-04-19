"""Tool-system bootstrap orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def ensure_tool_registry_loaded(
    runtime_paths: RuntimePaths,
    config: Config | None = None,
) -> None:
    """Ensure core, plugin, and MCP tool registrations are loaded for one runtime."""
    import mindroom.tools  # noqa: F401, PLC0415  # import here to avoid tools_metadata cycle

    if config is None:
        return

    from mindroom.tool_system.plugins import load_plugins  # noqa: PLC0415

    load_plugins(config, runtime_paths, set_skill_roots=False)

    from mindroom.mcp.registry import sync_mcp_tool_registry  # noqa: PLC0415

    sync_mcp_tool_registry(config)
