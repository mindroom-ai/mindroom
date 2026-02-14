"""Sandbox workspace tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.sandbox import SandboxTools


@register_tool_with_metadata(
    name="sandbox",
    display_name="Sandbox Workspace",
    description="Inspect and reset the persistent in-container sandbox workspace",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Shield",
    icon_color="text-amber-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def sandbox_tools() -> type[SandboxTools]:
    """Return sandbox workspace tools."""
    from mindroom.custom_tools.sandbox import SandboxTools

    return SandboxTools
