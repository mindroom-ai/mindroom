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
        # Authentication/Connection parameters
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="cal_live_...",
            description="Cal.com API key (can also be set via CALCOM_API_KEY env var)",
        ),
        ConfigField(
            name="event_type_id",
            label="Event Type ID",
            type="number",
            required=False,
            placeholder="123456",
            description="Default event type ID for bookings (can also be set via CALCOM_EVENT_TYPE_ID env var)",
        ),
        ConfigField(
            name="user_timezone",
            label="User Timezone",
            type="text",
            required=False,
            default="America/New_York",
            placeholder="America/New_York",
            description="User's timezone in IANA format (e.g., 'America/New_York', 'Europe/London')",
        ),
        # Booking management features
        ConfigField(
            name="get_available_slots",
            label="Get Available Slots",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting available time slots for booking",
        ),
        ConfigField(
            name="create_booking",
            label="Create Booking",
            type="boolean",
            required=False,
            default=True,
            description="Enable creating new bookings",
        ),
        ConfigField(
            name="get_upcoming_bookings",
            label="Get Upcoming Bookings",
            type="boolean",
            required=False,
            default=True,
            description="Enable getting upcoming bookings",
        ),
        ConfigField(
            name="reschedule_booking",
            label="Reschedule Booking",
            type="boolean",
            required=False,
            default=True,
            description="Enable rescheduling existing bookings",
        ),
        ConfigField(
            name="cancel_booking",
            label="Cancel Booking",
            type="boolean",
            required=False,
            default=True,
            description="Enable canceling existing bookings",
        ),
    ],
    dependencies=["requests", "pytz"],
    docs_url="https://docs.agno.com/tools/toolkits/others/calcom",
)
def cal_com_tools() -> type[CalComTools]:
    """Return Cal.com tools for calendar scheduling and booking management."""
    from agno.tools.calcom import CalComTools

    return CalComTools
