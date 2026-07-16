"""Callback manager tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.callback_manager import CallbackManagerTools


@register_tool_with_metadata(
    name="callback_manager",
    display_name="Callback Manager",
    description="Mint one-shot completion callbacks that wake this agent when a sub-agent finishes",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Webhook",
    icon_color="text-amber-500",
    dependencies=["agno"],
    docs_url="https://docs.mindroom.chat/agent-callbacks/",
    function_names=(
        "mint_callback",
        "list_callbacks",
        "revoke_callback",
    ),
)
def callback_manager_tools() -> type[CallbackManagerTools]:
    """Return callback manager tools."""
    from mindroom.custom_tools.callback_manager import CallbackManagerTools

    return CallbackManagerTools
