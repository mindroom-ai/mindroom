"""GitHub tool configuration."""

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
    from mindroom.custom_tools.github import GithubTools


@register_tool_with_metadata(
    name="github",
    file_access=ToolFileAccess.NONE,
    display_name="GitHub",
    description="Browse code and manage GitHub repositories, issues, and pull requests",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    requires_primary_runtime=True,
    auth_provider="github",
    icon="SiGithub",
    icon_color="text-gray-800",  # GitHub black
    config_fields=[
        ConfigField(
            name="access_token",
            env=("GITHUB_ACCESS_TOKEN",),
            label="Access Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default=None,
        ),
    ],
    dependencies=["PyGithub"],
    oauth_fallback_fields=("access_token",),
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CREDENTIALS_MANAGER,
        ToolManagedInitArg.WORKER_TARGET,
        ToolManagedInitArg.RUNTIME_CONFIG,
    ),
    docs_url="https://docs.mindroom.chat/tools/project-management/#github",
    function_names=(
        "github_read_repo",
        "github_read_issues_prs",
        "github_write_issues",
        "github_write_pr",
        "github_write_repo",
    ),
    action_scoped_grants=(
        "github_read_repo",
        "github_read_issues_prs",
        "github_write_issues",
        "github_write_pr",
        "github_write_repo",
    ),
)
def github_tools() -> type[GithubTools]:
    """Return GitHub tools for repository management."""
    from mindroom.custom_tools.github import GithubTools

    return GithubTools
