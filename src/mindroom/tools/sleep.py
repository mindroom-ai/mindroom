"""Sleep tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.sleep import SleepTools


@register_tool_with_metadata(
    name="sleep",
    display_name="Sleep",
    description="Sleep utility for introducing delays and pauses in execution",
    category=ToolCategory.DEVELOPMENT,  # Local utility tool
    status=ToolStatus.AVAILABLE,  # No config needed
    setup_type=SetupType.NONE,  # No authentication required
    icon="Clock",  # React icon name
    icon_color="text-purple-500",  # Tailwind color class
    config_fields=[
        # SleepTools has no explicit parameters, only **kwargs
        # The tool inherits all base Toolkit configuration through **kwargs
    ],
    dependencies=["agno"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/local/sleep",
)
def sleep_tools() -> type[SleepTools]:
    """Return sleep tools for introducing delays and pauses in execution."""
    from agno.tools.sleep import SleepTools

    return SleepTools
