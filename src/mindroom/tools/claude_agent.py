"""Claude Agent SDK tool configuration."""

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
    from mindroom.custom_tools.claude_agent import ClaudeAgentTools


@register_tool_with_metadata(
    name="claude_agent",
    display_name="Claude Agent SDK",
    description="Run persistent Claude coding sessions with tool-use and subagents",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Bot",
    icon_color="text-orange-500",
    config_fields=[
        ConfigField(name="api_key", label="Anthropic API Key", type="password", required=False, default=None),
        ConfigField(
            name="anthropic_base_url",
            label="Anthropic Base URL",
            type="url",
            required=False,
            default=None,
            description="Optional Anthropic-compatible gateway URL. Use host root (for example http://litellm.local), not a /v1 suffix.",
        ),
        ConfigField(
            name="anthropic_auth_token",
            label="Anthropic Auth Token",
            type="password",
            required=False,
            default=None,
            description="Optional bearer token for Anthropic-compatible gateways.",
        ),
        ConfigField(
            name="disable_experimental_betas",
            label="Disable Experimental Betas",
            type="boolean",
            required=False,
            default=False,
            description="Set CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 for Anthropic-compatible gateway compatibility.",
        ),
        ConfigField(name="cwd", label="Working Directory", type="text", required=False, default=None),
        ConfigField(name="model", label="Claude Model", type="text", required=False, default=None),
        ConfigField(
            name="permission_mode",
            label="Permission Mode",
            type="text",
            required=False,
            default="default",
            description="default, acceptEdits, plan, or bypassPermissions",
        ),
        ConfigField(
            name="continue_conversation",
            label="Continue Conversation",
            type="boolean",
            required=False,
            default=False,
            description="Continue the same Claude conversation context across queries in a session.",
        ),
        ConfigField(
            name="allowed_tools",
            label="Allowed Tools",
            type="text",
            required=False,
            default=None,
            description="Comma-separated Claude Code tool names",
        ),
        ConfigField(
            name="disallowed_tools",
            label="Disallowed Tools",
            type="text",
            required=False,
            default=None,
            description="Comma-separated Claude Code tool names",
        ),
        ConfigField(name="max_turns", label="Max Turns", type="number", required=False, default=None),
        ConfigField(name="system_prompt", label="System Prompt", type="text", required=False, default=None),
        ConfigField(name="cli_path", label="CLI Path", type="text", required=False, default=None),
        ConfigField(
            name="session_ttl_minutes",
            label="Session TTL Minutes",
            type="number",
            required=False,
            default=60,
        ),
        ConfigField(
            name="max_sessions",
            label="Max Sessions",
            type="number",
            required=False,
            default=200,
        ),
    ],
    dependencies=["claude-agent-sdk"],
    docs_url="https://platform.claude.com/docs/en/agent-sdk/python",
)
def claude_agent_tools() -> type[ClaudeAgentTools]:
    """Return tools for managing persistent Claude Agent SDK sessions."""
    from mindroom.custom_tools.claude_agent import ClaudeAgentTools

    return ClaudeAgentTools
