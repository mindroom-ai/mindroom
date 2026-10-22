"""Outlook mail tool registration."""

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
    from mindroom.custom_tools.outlook_mail import OutlookMailTools


@register_tool_with_metadata(
    name="outlook_mail",
    display_name="Outlook Mail",
    description="Search, read, draft, send, and organize Outlook mail as the connected user",
    category=ToolCategory.EMAIL,
    file_access=ToolFileAccess.AGENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    consumes_workspace_paths=True,
    auth_provider="outlook_mail",
    icon="Mail",
    icon_color="text-sky-600",
    config_fields=[
        ConfigField(
            name="write",
            label="Allow Write Operations",
            type="boolean",
            required=False,
            default=True,
            description=(
                "Allow creating drafts, sending and replying, marking read, moving messages, "
                "and changing automatic replies."
            ),
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
        ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,
        ToolManagedInitArg.FILE_ACCESS,
    ),
    docs_url="https://docs.mindroom.chat/tools/microsoft-365/",
    helper_text=(
        "Each user connects their own Microsoft account. "
        "Store the Microsoft Entra app's client ID and secret in the microsoft_oauth_client credential service."
    ),
    function_names=(
        "outlook_mail_search",
        "outlook_mail_get_message",
        "outlook_mail_download_attachment",
        "outlook_mail_get_automatic_replies",
        "outlook_mail_create_draft",
        "outlook_mail_send",
        "outlook_mail_reply",
        "outlook_mail_mark_read",
        "outlook_mail_move",
        "outlook_mail_set_automatic_replies",
    ),
)
def outlook_mail_tools() -> type[OutlookMailTools]:
    """Return Outlook mail tools that act as the requester."""
    from mindroom.custom_tools.outlook_mail import OutlookMailTools

    return OutlookMailTools
