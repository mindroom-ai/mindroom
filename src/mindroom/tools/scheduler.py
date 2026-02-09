"""Scheduler tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.scheduler import SchedulerTools


@register_tool_with_metadata(
    name="scheduler",
    display_name="Scheduler",
    description="Schedule tasks and reminders using the same backend as !schedule",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Calendar",
    icon_color="text-emerald-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
)
def scheduler_tools() -> type[SchedulerTools]:
    """Return scheduler tools for scheduling tasks from agent tool calls."""
    from mindroom.custom_tools.scheduler import SchedulerTools

    return SchedulerTools
