"""Cal.com tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.calcom import CalComTools


@register_tool_with_metadata(
    name="cal_com",
    display_name="Cal.com",
    description="Calendar scheduling and booking management",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaCalendarAlt",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="event_type_id",
            label="Event Type ID",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="user_timezone",
            label="User Timezone",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_get_available_slots",
            label="Enable Get Available Slots",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_create_booking",
            label="Enable Create Booking",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_upcoming_bookings",
            label="Enable Get Upcoming Bookings",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_reschedule_booking",
            label="Enable Reschedule Booking",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_cancel_booking",
            label="Enable Cancel Booking",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["requests", "pytz"],
    docs_url="https://docs.agno.com/tools/toolkits/others/calcom",
)
def cal_com_tools() -> type[CalComTools]:
    """Return Cal.com tools for calendar scheduling and booking management."""
    from agno.tools.calcom import CalComTools

    return CalComTools
