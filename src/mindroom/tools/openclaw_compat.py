"""OpenClaw compatibility preset tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.tool_system.authoring import Toolkit


@register_tool_with_metadata(
    name="openclaw_compat",
    file_access=ToolFileAccess.NONE,
    display_name="OpenClaw Compat",
    description="Convenience bundle that implies shell, coding, browser, and other common tools",
    docs_url="https://docs.mindroom.chat/openclaw/#openclaw_compat",
    category=ToolCategory.DEVELOPMENT,
    icon="Workflow",
    icon_color="text-orange-500",
    helper_text="Implies: shell, coding, duckduckgo, website, browser, scheduler, matrix_message, attachments, matrix_room.",
)
def openclaw_compat_tools() -> type[Toolkit]:
    """Return an empty toolkit — the real tools are loaded via tool preset expansion."""
    from mindroom.tool_system.authoring import Toolkit

    return Toolkit
