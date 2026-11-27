"""MindRoom Chat UI action tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.chat_ui import ChatUITools


@register_tool_with_metadata(
    name="chat_ui",
    file_access=ToolFileAccess.AGENT,
    display_name="Chat UI",
    description=(
        "Show MindRoom Chat UI: Computer/Members panels, Settings, or interactive HTML/chart/diagram Canvases "
        "with live state (when enabled)"
    ),
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    config_fields=[
        bool_config_field(
            "enable_show_canvas",
            "Enable Show Canvas",
            default=False,
            description="Let the agent show interactive web pages in the Canvas panel. "
            "MindRoom Chat shows them unless its deployment turns canvases off (mindroom.canvas.enabled).",
        ),
        bool_config_field(
            "enable_canvas_libraries",
            "Enable Canvas Libraries",
            default=False,
            description="Tell the agent its canvas pages may load libraries from https://cdn.jsdelivr.net/npm/. "
            "Needs enable_show_canvas, and MindRoom Chat must also allow them (mindroom.canvas.libraries).",
        ),
        bool_config_field(
            "enable_canvas_remote_assets",
            "Enable Canvas Remote Assets",
            default=False,
            description="Tell the agent its canvas pages may load images, styles, fonts, media and scripts from any https: address. "
            "Needs enable_show_canvas, and MindRoom Chat must also allow them (mindroom.canvas.remoteAssets).",
        ),
    ],
    requires_primary_runtime=True,
    requires_room_context=True,
    icon="PanelsTopLeft",
    icon_color="text-violet-500",
    docs_url="https://docs.mindroom.chat/tools/chat-ui/",
    function_names=("open_settings", "open_panel", "show_canvas", "read_canvas_state"),
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT, ToolManagedInitArg.FILE_ACCESS),
)
def chat_ui_tools() -> type[ChatUITools]:
    """Return bounded MindRoom Chat UI action tools."""
    from mindroom.custom_tools.chat_ui import ChatUITools

    return ChatUITools
