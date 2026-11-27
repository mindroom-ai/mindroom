"""Sleep tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus, bool_config_field
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.sleep import SleepTools


@register_tool_with_metadata(
    name="sleep",
    file_access=ToolFileAccess.NONE,
    display_name="Sleep",
    description="Sleep utility for introducing delays and pauses in execution",
    category=ToolCategory.DEVELOPMENT,  # Local utility tool
    status=ToolStatus.AVAILABLE,  # No config needed
    setup_type=SetupType.NONE,  # No authentication required
    icon="Clock",  # React icon name
    icon_color="text-purple-500",  # Tailwind color class
    config_fields=[
        bool_config_field("enable_sleep", "Enable Sleep"),
        bool_config_field("all", "All", default=False),
    ],
    docs_url="https://docs.mindroom.chat/tools/execution-and-coding/#sleep",
    function_names=("sleep",),
)
def sleep_tools() -> type[SleepTools]:
    """Return sleep tools for introducing delays and pauses in execution."""
    from mindroom.custom_tools.sleep import SleepTools

    return SleepTools
