"""Google Docs tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.google_docs import GoogleDocsTools


@register_tool_with_metadata(
    name="google_docs",
    display_name="Google Docs",
    description="Create, read, and edit documents through the connected user's Google Docs",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    auth_provider="google_docs",
    icon="SiGoogledocs",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="create_document",
            label="Create Documents",
            type="boolean",
            required=False,
            default=True,
            description="Allow creating Google Docs and adding initial text.",
        ),
        ConfigField(
            name="read_document",
            label="Read Documents",
            type="boolean",
            required=False,
            default=True,
            description="Allow reading complete tab-aware document structure and content.",
        ),
        ConfigField(
            name="edit_document",
            label="Edit Documents",
            type="boolean",
            required=False,
            default=True,
            description="Allow inserting and replacing text in Google Docs.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
    ),
    dependencies=[
        "google-api-python-client",
        "google-auth",
        "google-auth-httplib2",
        "google-auth-oauthlib",
    ],
    docs_url="https://developers.google.com/workspace/docs/api/reference/rest",
    function_names=(
        "google_docs_create_document",
        "google_docs_get_document",
        "google_docs_insert_text",
        "google_docs_replace_text",
    ),
)
def google_docs_tools() -> type[GoogleDocsTools]:
    """Return Google Docs tools for document creation, reading, and editing."""
    from mindroom.custom_tools.google_docs import GoogleDocsTools

    return GoogleDocsTools
