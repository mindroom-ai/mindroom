"""Attachments toolkit registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.attachments import AttachmentTools


@register_tool_with_metadata(
    name="attachments",
    display_name="Attachments",
    description="List, register, and send context-scoped file attachments",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Paperclip",
    icon_color="text-teal-500",
    config_fields=[],
    dependencies=[],
)
def attachments_tools() -> type[AttachmentTools]:
    """Return attachments tools."""
    from mindroom.custom_tools.attachments import AttachmentTools

    return AttachmentTools
