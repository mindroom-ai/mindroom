"""OpenClaw compatibility toolkit configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.openclaw_compat import OpenClawCompatTools


@register_tool_with_metadata(
    name="openclaw_compat",
    display_name="OpenClaw Compat",
    description="OpenClaw-style tool surface for session and orchestration compatibility",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Workflow",
    icon_color="text-orange-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def openclaw_compat_tools() -> type[OpenClawCompatTools]:
    """Return OpenClaw-compatible tools."""
    from mindroom.custom_tools.openclaw_compat import OpenClawCompatTools

    return OpenClawCompatTools
