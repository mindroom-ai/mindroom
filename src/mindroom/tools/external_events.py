"""External event delivery tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.external_events import ExternalEventsTools


@register_tool_with_metadata(
    name="external_events",
    display_name="External Events",
    description="Deliver external messages to this agent with durable event and conversation identity",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_room_context=True,
    icon="Webhook",
    icon_color="text-emerald-500",
    dependencies=["agno"],
    function_names=("deliver_event",),
)
def external_events_tools() -> type[ExternalEventsTools]:
    """Return the primary-runtime external event toolkit."""
    from mindroom.custom_tools.external_events import ExternalEventsTools

    return ExternalEventsTools
