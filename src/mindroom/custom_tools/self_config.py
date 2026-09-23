"""Self-config tool: lets an agent read and modify its own configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import yaml
from agno.tools import Toolkit
from pydantic import ValidationError

from mindroom.api.config_lifecycle import validate_and_persist_config_payload
from mindroom.authorization import is_platform_administrator
from mindroom.config.agent import AgentConfig
from mindroom.config.main import ConfigRuntimeValidationError, format_invalid_config_message, load_config_or_user_error
from mindroom.config.models import AgentLearningMode  # noqa: TC001
from mindroom.custom_tools.config_manager import preserve_tool_overrides, validate_knowledge_bases
from mindroom.logging_config import get_logger
from mindroom.mcp.registry import mcp_tool_name
from mindroom.tool_system.catalog import resolved_tool_metadata_for_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# Tools an agent may never grant itself, even for an administrator requester. The model
# that issues the call also reads untrusted room messages, documents, and web pages, so
# a self-grant of code execution, host control, database execution, arbitrary outbound
# requests, or platform administration is treated as escalation regardless of requester.
# Tools the agent already holds stay assignable so that self-tuning can keep them.
_SELF_CONFIG_BLOCKED_TOOLS = frozenset(
    {
        # Platform, scheduling, and credential control.
        "agent_vault_access",
        "approved_egress",
        "callback_manager",
        "config_manager",
        "dynamic_workflow",
        "external_trigger_manager",
        "invite_router",
        "oauth_connections",
        "report_publishing",
        "scheduler",
        # Code execution and host control.
        "airflow",
        "apify",
        "aws_lambda",
        "browser",
        "browser_mcp",
        "browserbase",
        "claude_agent",
        "coding",
        "daytona",
        "desktop",
        "docker",
        "e2b",
        "file",
        "pandas",
        "python",
        "script",
        "shell",
        "web_browser_tools",
        # Repository write access, which reaches execution through the operator's CI.
        "bitbucket",
        "github",
        # Low-level Matrix control and arbitrary outbound requests.
        "composio",
        "custom_api",
        "matrix_api",
        # Database and query execution.
        "csv",
        "duckdb",
        "google_bigquery",
        "neo4j",
        "postgres",
        "redshift",
        "sql",
    },
)
_CONFIG_CHANGE_REJECTED_MESSAGE = "Changes were NOT applied."
_PLATFORM_ADMIN_REQUIRED_MESSAGE = (
    "Error: Self-configuration changes require an active platform administrator requester."
)


def _self_config_mutation_authorization_error(config: Config, agent_name: str) -> str | None:
    """Deny self-config writes without a current platform administrator requester."""
    runtime_context = get_tool_runtime_context()
    if runtime_context is not None and is_platform_administrator(
        runtime_context.requester_id,
        config,
        runtime_context.runtime_paths,
    ):
        return None
    logger.warning(
        "self_config_update_denied",
        agent=agent_name,
        requester_id=runtime_context.requester_id if runtime_context is not None else None,
        reason="requester_is_not_a_platform_administrator",
    )
    return f"{_PLATFORM_ADMIN_REQUIRED_MESSAGE}\n\n{_CONFIG_CHANGE_REJECTED_MESSAGE}"


def _blocked_tools_for_config(config: Config) -> frozenset[str]:
    """Return the blocked names for one config, including its configured MCP servers.

    An MCP server exposes whatever remote surface its operator pointed it at, so no
    static list can classify it; every configured server is blocked by construction.
    """
    return _SELF_CONFIG_BLOCKED_TOOLS | {mcp_tool_name(server_id) for server_id in config.mcp_servers}


def _newly_granted_blocked_tools(config: Config, agent_name: str, tool_names: Sequence[str]) -> list[str]:
    """Return blocked tools these names would add to an agent that lacks them."""
    blocked = _blocked_tools_for_config(config)
    already_available = set(config.resolve_entity(agent_name).available_tools)
    requested = config.expand_tool_names(list(tool_names))
    return sorted({name for name in requested if name in blocked and name not in already_available})


def _tool_grant_error(
    config: Config,
    agent_name: str,
    runtime_paths: RuntimePaths,
    tools: list[str] | None,
    include_default_tools: bool | None,
) -> str | None:
    """Reject unknown tools and privileged tools this agent does not already hold."""
    if tools is not None:
        tool_metadata = resolved_tool_metadata_for_runtime(
            runtime_paths,
            config,
            tolerate_plugin_load_errors=True,
        )
        invalid_tools = [name for name in tools if name not in tool_metadata]
        if invalid_tools:
            return f"Error: Unknown tools: {', '.join(invalid_tools)}"
        blocked_tools = _newly_granted_blocked_tools(config, agent_name, tools)
        if blocked_tools:
            logger.warning("self_config_privileged_tool_grant_denied", agent=agent_name, tools=blocked_tools)
            return f"Error: Self-config cannot assign privileged tools: {', '.join(blocked_tools)}"

    if include_default_tools is True:
        inherited_blocked = _newly_granted_blocked_tools(config, agent_name, config.defaults.tool_names)
        if inherited_blocked:
            logger.warning("self_config_privileged_tool_grant_denied", agent=agent_name, tools=inherited_blocked)
            return (
                f"Error: Cannot enable include_default_tools because defaults.tools "
                f"contains privileged tools: {', '.join(inherited_blocked)}"
            )
    return None


class SelfConfigTools(Toolkit):
    """Tools that let an agent read and modify its own configuration only."""

    def __init__(self, agent_name: str, runtime_paths: RuntimePaths) -> None:
        self.agent_name = agent_name
        self.runtime_paths = runtime_paths
        self.config_path = runtime_paths.config_path
        super().__init__(
            name="self_config",
            tools=[self.get_own_config, self.update_own_config],
        )
        # Self-config writes persist for every later requester of this agent, so a human
        # confirms each one even when tool_approval.default is auto_approve.
        self.functions["update_own_config"].requires_confirmation = True

    def get_own_config(self) -> str:
        """Get this agent's current configuration as YAML.

        Returns:
            The agent's configuration formatted as YAML, or an error message.

        """
        config, load_error = load_config_or_user_error(
            self.runtime_paths,
            tolerate_plugin_load_errors=True,
        )
        if load_error:
            return load_error
        assert config is not None

        if self.agent_name not in config.agents:
            return f"Error: Agent '{self.agent_name}' not found in configuration."

        agent_dict = config.agents[self.agent_name].authored_model_dump()
        yaml_str = yaml.dump(agent_dict, default_flow_style=False, sort_keys=False)
        return f"## Configuration for '{self.agent_name}':\n\n```yaml\n{yaml_str}```"

    def update_own_config(  # noqa: C901, PLR0912, PLR0911
        self,
        display_name: str | None = None,
        role: str | None = None,
        instructions: list[str] | None = None,
        tools: list[str] | None = None,
        model: str | None = None,
        rooms: list[str] | None = None,
        markdown: bool | None = None,
        learning: bool | None = None,
        learning_mode: AgentLearningMode | None = None,
        knowledge_bases: list[str] | None = None,
        skills: list[str] | None = None,
        include_default_tools: bool | None = None,
        show_tool_calls: bool | None = None,
        thread_mode: Literal["thread", "room"] | None = None,
        num_history_runs: int | None = None,
        num_history_messages: int | None = None,
        compress_tool_results: bool | None = None,
        max_tool_calls_from_history: int | None = None,
        context_files: list[str] | None = None,
    ) -> str:
        """Update this agent's own configuration. Only provided fields are changed.

        Args:
            display_name: Human-readable display name
            role: Description of the agent's purpose
            instructions: List of instructions for the agent
            tools: List of tool names to enable
            model: Model name to use
            rooms: List of room names to auto-join
            markdown: Whether to use markdown formatting
            learning: Whether to enable Agno Learning
            learning_mode: Learning mode ("always" or "agentic")
            knowledge_bases: List of knowledge base IDs
            skills: List of skill names
            include_default_tools: Whether to merge defaults.tools
            show_tool_calls: Show tool call details inline in responses
            thread_mode: Conversation threading mode ("thread" or "room")
            num_history_runs: Number of prior runs to include as history
            num_history_messages: Max messages from history
            compress_tool_results: Compress tool results in history (disabled by default because it can invalidate Anthropic/Vertex Claude prompt caches)
            max_tool_calls_from_history: Max tool call messages replayed from history
            context_files: Workspace-relative file paths loaded into each freshly built agent instance

        Returns:
            Success message with changes or an error message.

        """
        config, load_error = load_config_or_user_error(
            self.runtime_paths,
            footer=_CONFIG_CHANGE_REJECTED_MESSAGE,
            tolerate_plugin_load_errors=True,
        )
        if load_error:
            return load_error
        assert config is not None

        authorization_error = _self_config_mutation_authorization_error(config, self.agent_name)
        if authorization_error:
            return authorization_error

        if self.agent_name not in config.agents:
            return f"Error: Agent '{self.agent_name}' not found in configuration."

        tool_grant_error = _tool_grant_error(
            config,
            self.agent_name,
            self.runtime_paths,
            tools,
            include_default_tools,
        )
        if tool_grant_error:
            return tool_grant_error

        # Validate knowledge bases
        if knowledge_bases is not None:
            kb_error = validate_knowledge_bases(knowledge_bases, set(config.knowledge_bases))
            if kb_error:
                return kb_error

        agent = config.agents[self.agent_name]
        requested_updates: list[tuple[str, object]] = [
            ("display_name", display_name),
            ("role", role),
            ("instructions", instructions),
            ("tools", preserve_tool_overrides(agent.tools, tools) if tools is not None else None),
            ("model", model),
            ("rooms", rooms),
            ("markdown", markdown),
            ("learning", learning),
            ("learning_mode", learning_mode),
            ("knowledge_bases", knowledge_bases),
            ("skills", skills),
            ("include_default_tools", include_default_tools),
            ("show_tool_calls", show_tool_calls),
            ("thread_mode", thread_mode),
            ("num_history_runs", num_history_runs),
            ("num_history_messages", num_history_messages),
            ("compress_tool_results", compress_tool_results),
            ("max_tool_calls_from_history", max_tool_calls_from_history),
            ("context_files", context_files),
        ]
        non_null_updates = {field_name: value for field_name, value in requested_updates if value is not None}

        candidate_agent_data = agent.model_dump()
        candidate_agent_data.update(non_null_updates)

        try:
            validated_agent = AgentConfig.model_validate(candidate_agent_data)
        except ValidationError as e:
            return f"Error validating configuration: {e}"

        current_values = agent.model_dump()
        validated_values = validated_agent.model_dump()
        updates: dict[str, str] = {}
        for field_name, new_value in requested_updates:
            if new_value is None:
                continue
            current_value = current_values[field_name]
            validated_value = validated_values[field_name]
            if validated_value == current_value:
                continue
            display = field_name.replace("_", " ").title()
            if field_name == "tools":
                formatted = ", ".join(validated_agent.tool_names) if validated_agent.tool_names else "(empty)"
            elif isinstance(validated_value, list):
                formatted = ", ".join(str(v) for v in validated_value) if validated_value else "(empty)"
            else:
                formatted = str(validated_value)
            updates[display] = formatted

        if not updates:
            return "No changes made. All provided values match the current configuration."

        config.agents[self.agent_name] = validated_agent
        try:
            validate_and_persist_config_payload(config.authored_model_dump(), self.runtime_paths)
        except (ValidationError, ConfigRuntimeValidationError) as exc:
            return format_invalid_config_message(exc, footer=_CONFIG_CHANGE_REJECTED_MESSAGE)
        except Exception as e:
            return f"Error saving configuration: {e}"

        changes = "\n".join(f"- {name} -> {new}" for name, new in updates.items())
        return f"Successfully updated own configuration:\n\n{changes}"
