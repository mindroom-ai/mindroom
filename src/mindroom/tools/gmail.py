"""Gmail tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from mindroom.custom_tools.gmail import GmailTools


@register_tool_with_metadata(
    name="gmail",
    display_name="Gmail",
    description="Read, search, and manage Gmail emails",
    category=ToolCategory.EMAIL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.SPECIAL,
    auth_provider="google",  # Authentication provided by Google Services integration
    icon="FaGoogle",
    icon_color="text-red-500",
    config_fields=[
        ConfigField(
            name="get_latest_emails",
            label="Get Latest Emails",
            type="boolean",
            required=False,
            default=True,
            description="Allow retrieving the latest emails",
        ),
        ConfigField(
            name="get_emails_from_user",
            label="Get Emails From User",
            type="boolean",
            required=False,
            default=True,
            description="Allow retrieving emails from specific users",
        ),
        ConfigField(
            name="get_unread_emails",
            label="Get Unread Emails",
            type="boolean",
            required=False,
            default=True,
            description="Allow retrieving unread emails",
        ),
        ConfigField(
            name="get_starred_emails",
            label="Get Starred Emails",
            type="boolean",
            required=False,
            default=True,
            description="Allow retrieving starred emails",
        ),
        ConfigField(
            name="search_emails",
            label="Search Emails",
            type="boolean",
            required=False,
            default=True,
            description="Allow searching through emails",
        ),
        ConfigField(
            name="create_draft_email",
            label="Create Draft Emails",
            type="boolean",
            required=False,
            default=True,
            description="Allow creating draft emails",
        ),
        ConfigField(
            name="send_email",
            label="Send Emails",
            type="boolean",
            required=False,
            default=True,
            description="Allow sending emails",
        ),
        ConfigField(
            name="send_email_reply",
            label="Send Email Replies",
            type="boolean",
            required=False,
            default=True,
            description="Allow sending replies to emails",
        ),
    ],
    dependencies=["google-api-python-client", "google-auth", "google-auth-oauthlib", "google-auth-httplib2"],
    docs_url="https://docs.agno.com/tools/toolkits/social/gmail",
)
def gmail_tools() -> type[GmailTools]:
    """Return Gmail tools for email management."""
    from mindroom.custom_tools.gmail import GmailTools

    return GmailTools
