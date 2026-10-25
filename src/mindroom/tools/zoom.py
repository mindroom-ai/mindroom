"""Zoom tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.zoom import ZoomTools


@register_tool_with_metadata(
    name="zoom",
    file_access=ToolFileAccess.NONE,
    display_name="Zoom",
    description="Video conferencing platform for scheduling and managing meetings",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    icon="SiZoom",
    icon_color="text-blue-500",  # Zoom blue
    config_fields=[
        # Authentication parameters
        ConfigField(
            name="account_id",
            label="Account ID",
            type="text",
            required=False,
            placeholder="your_account_id",
            description="Zoom account ID from Server-to-Server OAuth app",
        ),
        ConfigField(
            name="client_id",
            label="Client ID",
            type="text",
            required=False,
            placeholder="your_client_id",
            description="Client ID from Server-to-Server OAuth app",
        ),
        ConfigField(
            name="client_secret",
            label="Client Secret",
            type="password",
            required=False,
            placeholder="your_client_secret",
            description="Client secret from Server-to-Server OAuth app",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
            description="Per-request HTTP timeout in seconds",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/social/zoom",
    function_names=(
        "delete_meeting",
        "get_meeting",
        "get_meeting_recordings",
        "get_upcoming_meetings",
        "instructions",
        "list_meetings",
        "schedule_meeting",
    ),
)
def zoom_tools() -> type[ZoomTools]:
    """Return Zoom tools for video conferencing and meeting management."""
    from agno.tools.zoom import ZoomTools

    class MindRoomZoomTools(ZoomTools):
        """Zoom toolkit that never hands the account's access token to the model."""

        def __init__(
            self,
            account_id: str | None = None,
            client_id: str | None = None,
            client_secret: str | None = None,
            timeout: int = 30,
            **kwargs: object,
        ) -> None:
            # AGNO_COMPAT: ZoomTools offers its OAuth access token to the model as a tool.
            # Reason: get_access_token returns the Server-to-Server bearer token for the whole Zoom account,
            # which works outside MindRoom's approvals; the meeting tools mint it themselves.
            # Upstream issue: Tracking gap; no matching issue has been verified.
            # Upstream PR: No matching fix identified.
            # Remove when: Agno stops registering get_access_token as a toolkit function.
            # Coverage: tests/test_tool_config_sync.py::test_zoom_never_offers_its_access_token_to_the_model.
            super().__init__(
                account_id=account_id,
                client_id=client_id,
                client_secret=client_secret,
                timeout=timeout,
                exclude_tools=["get_access_token"],
                **kwargs,
            )

    return MindRoomZoomTools
