"""Gmail tool configuration."""

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
    from mindroom.custom_tools.gmail import GmailTools


@register_tool_with_metadata(
    name="gmail",
    display_name="Gmail",
    description="Search, read, send, and organize email in Gmail",
    category=ToolCategory.EMAIL,
    file_access=ToolFileAccess.AGENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    consumes_workspace_paths=True,
    auth_provider="google_gmail",
    icon="SiGmail",
    icon_color="text-red-500",
    config_fields=[
        bool_config_field("get_latest_emails", "Get Latest Emails", description="Allow retrieving the latest emails"),
        bool_config_field(
            "get_emails_from_user",
            "Get Emails From User",
            description="Allow retrieving emails from specific users",
        ),
        bool_config_field("get_unread_emails", "Get Unread Emails", description="Allow retrieving unread emails"),
        bool_config_field("get_starred_emails", "Get Starred Emails", description="Allow retrieving starred emails"),
        bool_config_field("search_emails", "Search Emails", description="Allow searching through emails"),
        bool_config_field("create_draft_email", "Create Draft Emails", description="Allow creating draft emails"),
        bool_config_field("send_email", "Send Emails", description="Allow sending emails"),
        bool_config_field("send_email_reply", "Send Email Replies", description="Allow sending replies to emails"),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
        ToolManagedInitArg.TOOL_OUTPUT_WORKSPACE_ROOT,
        ToolManagedInitArg.FILE_ACCESS,
    ),
    dependencies=["google-api-python-client", "google-auth", "google-auth-oauthlib", "google-auth-httplib2"],
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#gmail",
    function_names=(
        "gmail_read",
        "gmail_draft",
        "gmail_send",
        "gmail_send_to_self",
        "gmail_manage",
    ),
    action_scoped_grants=(
        "gmail_read",
        "gmail_draft",
        "gmail_send",
        "gmail_manage",
    ),
)
def gmail_tools() -> type[GmailTools]:
    """Return Gmail tools for email management."""
    from mindroom.custom_tools.gmail import GmailTools

    return GmailTools
