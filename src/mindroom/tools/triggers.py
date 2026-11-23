"""Unified triggers tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.triggers import TriggersTools


@register_tool_with_metadata(
    name="triggers",
    file_access=ToolFileAccess.NONE,
    display_name="Triggers",
    description="Watch rooms, thread tags, shared event sources, and signed webhooks to wake agents",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_primary_runtime=True,
    requires_room_context=True,
    icon="Webhook",
    icon_color="text-emerald-500",
    docs_url="https://docs.mindroom.chat/external-triggers/",
    function_names=(
        "watch_room",
        "watch_thread",
        "subscribe_event",
        "create_trigger",
        "list_triggers",
        "disable_trigger",
        "delete_trigger",
        "rotate_trigger_key",
    ),
)
def triggers_tools() -> type[TriggersTools]:
    """Return unified trigger management tools."""
    from mindroom.custom_tools.triggers import TriggersTools

    return TriggersTools
