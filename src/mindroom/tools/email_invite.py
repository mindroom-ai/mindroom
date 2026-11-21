"""Email invite tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.email_invite import EmailInviteTools


@register_tool_with_metadata(
    name="email_invite",
    file_access=ToolFileAccess.NONE,
    display_name="Email Invite",
    description="Invite people to the current room by email, after someone who may invite approves",
    docs_url="https://docs.mindroom.chat/tools/matrix-and-attachments/#email_invite",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_primary_runtime=True,
    requires_room_context=True,
    function_names=("invite_by_email", "list_email_invites", "revoke_email_invite"),
)
def email_invite_tools() -> type[EmailInviteTools]:
    """Return the email invite tools."""
    from mindroom.custom_tools.email_invite import EmailInviteTools

    return EmailInviteTools
