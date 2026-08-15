"""Daytona tool configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.daytona import DaytonaTools


def _parse_string_mapping(value: dict[str, str] | str | None, *, field_name: str) -> dict[str, str] | None:
    """Parse one JSON-authored mapping while preserving native mappings."""
    if value is None or isinstance(value, dict):
        return value
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()
    ):
        msg = f"{field_name} must be a JSON object with string keys and values"
        raise ValueError(msg)
    return parsed


@register_tool_with_metadata(
    name="daytona",
    display_name="Daytona",
    description="Execute code in secure, remote sandbox environments",
    category=ToolCategory.DEVELOPMENT,  # others/ maps to DEVELOPMENT
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key
    setup_type=SetupType.API_KEY,  # Uses API key authentication
    icon="FaTerminal",  # Terminal icon for code execution
    icon_color="text-blue-600",  # Blue color for development tools
    config_fields=[
        # Authentication parameters
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="dt_...",
            description="Daytona API key",
        ),
        ConfigField(
            name="api_url",
            label="API URL",
            type="url",
            required=False,
            placeholder="https://api.daytona.io",
            description="Daytona API URL",
        ),
        # Sandbox configuration
        ConfigField(
            name="sandbox_id",
            label="Sandbox ID",
            type="text",
            required=False,
            placeholder="sandbox-123",
            description="Specific sandbox ID to use. If None, creates or uses persistent sandbox",
        ),
        ConfigField(
            name="sandbox_language",
            label="Sandbox Language",
            type="text",
            required=False,
            default="python",
            placeholder="python",
            description="Primary language for the sandbox (python, javascript, typescript)",
        ),
        ConfigField(
            name="sandbox_target",
            label="Sandbox Target",
            type="text",
            required=False,
            placeholder="target-config",
            description="Target configuration for the sandbox",
        ),
        ConfigField(
            name="sandbox_os",
            label="Sandbox OS",
            type="text",
            required=False,
            placeholder="ubuntu-20.04",
            description="Operating system for the sandbox",
        ),
        ConfigField(
            name="auto_stop_interval",
            label="Auto Stop Interval",
            type="number",
            required=False,
            default=60,
            description="Auto-stop interval in minutes (0 to disable)",
        ),
        ConfigField(
            name="sandbox_os_user",
            label="Sandbox OS User",
            type="text",
            required=False,
            placeholder="daytona",
            description="OS user for the sandbox",
        ),
        ConfigField(
            name="sandbox_env_vars",
            label="Sandbox Environment Variables",
            type="text",
            required=False,
            placeholder='{"ENV_VAR": "value"}',
            description="Environment variables for the sandbox (JSON format)",
        ),
        ConfigField(
            name="sandbox_labels",
            label="Sandbox Labels",
            type="text",
            required=False,
            default="{}",
            placeholder='{"label": "value"}',
            description="Labels for the sandbox (JSON format)",
        ),
        ConfigField(
            name="organization_id",
            label="Organization ID",
            type="text",
            required=False,
            placeholder="org-123",
            description="Organization ID for the sandbox",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=300,
            description="Timeout for sandbox operations in seconds",
        ),
        # Feature flags
        ConfigField(
            name="auto_create_sandbox",
            label="Auto Create Sandbox",
            type="boolean",
            required=False,
            default=True,
            description="Automatically create sandbox if none exists",
        ),
        ConfigField(
            name="verify_ssl",
            label="Verify SSL",
            type="boolean",
            required=False,
            default=False,
            description="Whether to verify SSL certificates",
        ),
        ConfigField(
            name="persistent",
            label="Persistent",
            type="boolean",
            required=False,
            default=True,
            description="Whether to reuse the same sandbox across agent sessions",
        ),
        ConfigField(
            name="sandbox_public",
            label="Sandbox Public",
            type="boolean",
            required=False,
            description="Whether the sandbox should be public",
        ),
        # Custom instructions
        ConfigField(
            name="instructions",
            label="Instructions",
            type="text",
            required=False,
            placeholder="Custom guidelines for the toolkit",
            description="Custom instructions for the toolkit",
        ),
        ConfigField(
            name="add_instructions",
            label="Add Instructions",
            type="boolean",
            required=False,
            default=False,
            description="Whether to add instructions to the agent",
        ),
    ],
    dependencies=["daytona"],
    docs_url="https://docs.agno.com/tools/toolkits/others/daytona",
    function_names=(
        "change_directory",
        "create_file",
        "delete_file",
        "list_files",
        "read_file",
        "run_code",
        "run_shell_command",
    ),
)
def daytona_tools() -> type[DaytonaTools]:
    """Return Daytona tools for secure code execution in remote sandbox environments."""
    from agno.tools.daytona import DaytonaTools
    from daytona import CodeLanguage

    class MindRoomDaytonaTools(DaytonaTools):
        """Daytona toolkit that normalizes dashboard-authored values."""

        def __init__(
            self,
            api_key: str | None = None,
            api_url: str | None = None,
            sandbox_id: str | None = None,
            sandbox_language: CodeLanguage | str | None = None,
            sandbox_target: str | None = None,
            sandbox_os: str | None = None,
            auto_stop_interval: int | None = 60,
            sandbox_os_user: str | None = None,
            sandbox_env_vars: dict[str, str] | str | None = None,
            sandbox_labels: dict[str, str] | str | None = None,
            sandbox_public: bool | None = None,
            organization_id: str | None = None,
            timeout: int = 300,
            auto_create_sandbox: bool = True,
            verify_ssl: bool | None = False,
            persistent: bool = True,
            instructions: str | None = None,
            add_instructions: bool = False,
            **kwargs: object,
        ) -> None:
            resolved_language = (
                CodeLanguage(sandbox_language.lower()) if isinstance(sandbox_language, str) else sandbox_language
            )
            super().__init__(
                api_key=api_key,
                api_url=api_url,
                sandbox_id=sandbox_id,
                sandbox_language=resolved_language,
                sandbox_target=sandbox_target,
                sandbox_os=sandbox_os,
                auto_stop_interval=auto_stop_interval,
                sandbox_os_user=sandbox_os_user,
                sandbox_env_vars=_parse_string_mapping(sandbox_env_vars, field_name="sandbox_env_vars"),
                sandbox_labels=_parse_string_mapping(sandbox_labels, field_name="sandbox_labels"),
                sandbox_public=sandbox_public,
                organization_id=organization_id,
                timeout=timeout,
                auto_create_sandbox=auto_create_sandbox,
                verify_ssl=verify_ssl,
                persistent=persistent,
                instructions=instructions,
                add_instructions=add_instructions,
                **kwargs,
            )

    MindRoomDaytonaTools.__init__.__annotations__["sandbox_language"] = CodeLanguage | str | None
    return MindRoomDaytonaTools
