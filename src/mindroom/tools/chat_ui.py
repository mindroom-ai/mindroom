"""MindRoom Chat UI action tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.chat_ui import ChatUITools


@register_tool_with_metadata(
    name="chat_ui",
    file_access=ToolFileAccess.AGENT,
    display_name="Chat UI",
    description=(
        "Show parts of MindRoom Chat to the user: the Computer panel (a live view of the agent's own "
        "worker browser), the Canvas panel (an interactive web page the agent writes, such as a dashboard, "
        "slides, or a form, which the user can answer; opt in with enable_show_canvas), the Members panel, "
        "or a Settings section. "
        "Sends a UI request; does not navigate or control the user's own browser."
    ),
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    config_fields=[
        ConfigField(
            name="enable_show_canvas",
            label="Enable Show Canvas",
            type="boolean",
            required=False,
            default=False,
            description=(
                "Let the agent show interactive web pages in the Canvas panel. "
                "MindRoom Chat must also turn canvases on (mindroom.canvas.enabled)."
            ),
        ),
        ConfigField(
            name="enable_canvas_libraries",
            label="Enable Canvas Libraries",
            type="boolean",
            required=False,
            default=False,
            description=(
                "Tell the agent its canvas pages may load libraries from https://cdn.jsdelivr.net/npm/. "
                "Needs enable_show_canvas, and MindRoom Chat must also allow them (mindroom.canvas.libraries)."
            ),
        ),
    ],
    requires_primary_runtime=True,
    requires_room_context=True,
    icon="PanelsTopLeft",
    icon_color="text-violet-500",
    dependencies=["agno"],
    docs_url="https://docs.mindroom.chat/tools/chat-ui/",
    function_names=("show_computer", "open_settings", "open_panel", "show_canvas", "read_canvas_state"),
    managed_init_args=(ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT, ToolManagedInitArg.FILE_ACCESS),
)
def chat_ui_tools() -> type[ChatUITools]:
    """Return bounded MindRoom Chat UI action tools."""
    from mindroom.custom_tools.chat_ui import ChatUITools

    return ChatUITools
