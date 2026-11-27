"""Google Docs tool configuration."""

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
    from mindroom.custom_tools.google_docs import GoogleDocsTools


@register_tool_with_metadata(
    name="google_docs",
    file_access=ToolFileAccess.NONE,
    display_name="Google Docs",
    description="Create, read, and edit documents through the connected user's Google Docs",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="google_docs",
    icon="SiGoogledocs",
    icon_color="text-blue-600",
    config_fields=[
        bool_config_field(
            "create_document",
            "Create Documents",
            description="Allow creating Google Docs and adding initial text.",
        ),
        bool_config_field(
            "read_document",
            "Read Documents",
            description="Allow reading complete tab-aware document structure and content.",
        ),
        bool_config_field(
            "edit_document",
            "Edit Documents",
            description="Allow editing Google Docs: text insertion, text replacement, and batch updates.",
        ),
    ],
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    dependencies=[
        "google-api-python-client",
        "google-auth",
        "google-auth-httplib2",
    ],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#google_docs",
    function_names=(
        "google_docs_get_document",
        "google_docs_write",
    ),
    action_scoped_grants=("google_docs_write",),
)
def google_docs_tools() -> type[GoogleDocsTools]:
    """Return Google Docs tools for document creation, reading, and editing."""
    from mindroom.custom_tools.google_docs import GoogleDocsTools

    return GoogleDocsTools
