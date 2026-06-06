"""Self-config tool: lets an agent read and modify its own configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import yaml
from agno.tools import Toolkit
from pydantic import ValidationError

from mindroom.api.config_lifecycle import validate_and_persist_config_payload
from mindroom.config.agent import AgentConfig
from mindroom.config.main import ConfigRuntimeValidationError, format_invalid_config_message, load_config_or_user_error
from mindroom.config.models import AgentLearningMode  # noqa: TC001
from mindroom.custom_tools.config_manager import preserve_tool_overrides, validate_knowledge_bases
from mindroom.logging_config import get_logger
from mindroom.tool_system.catalog import resolved_tool_metadata_for_runtime
from mindroom.tool_system.metadata import ToolExecutionTarget, ToolMetadata

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_CONFIG_CHANGE_REJECTED_MESSAGE = "Changes were NOT applied."


def _tool_is_privileged(metadata: ToolMetadata) -> bool:
    """Return whether self-config must not grant this tool."""
    return metadata.privileged or metadata.default_execution_target == ToolExecutionTarget.WORKER


def _privileged_tools(
    tool_names: set[str],
    tool_metadata: dict[str, ToolMetadata],
) -> list[str]:
    """Return requested tool names that would grant privileged capability."""
    return sorted(tool_name for tool_name in tool_names if _tool_is_privileged(tool_metadata[tool_name]))


def _requested_privileged_fields(
    *,
    context_files: list[str] | None,
    knowledge_bases: list[str] | None,
    model: str | None,
) -> list[str]:
    """Return self-config fields that grant data or endpoint access."""
    requested_values = {
        "context_files": context_files,
        "knowledge_bases": knowledge_bases,
        "model": model,
    }
    return sorted(field_name for field_name, value in requested_values.items() if value is not None)


def _tool_update_error(
    tools: list[str] | None,
    *,
    current_tool_names: list[str],
    tool_metadata: dict[str, ToolMetadata],
) -> str | None:
    """Return why one requested self-config tool update is unsafe or invalid."""
    if tools is None:
        return None
    invalid_tools = [t for t in tools if t not in tool_metadata]
    if invalid_tools:
        return f"Error: Unknown tools: {', '.join(invalid_tools)}"
    newly_requested_tools = set(tools) - set(current_tool_names)
    blocked_tools = _privileged_tools(newly_requested_tools, tool_metadata)
    if blocked_tools:
        return f"Error: Self-config cannot assign privileged tools: {', '.join(blocked_tools)}"
    return None


def _default_tools_update_error(
    include_default_tools: bool | None,
    *,
    default_tool_names: list[str],
    tool_metadata: dict[str, ToolMetadata],
) -> str | None:
    """Return why include_default_tools would inherit privileged tools."""
    if include_default_tools is not True:
        return None
    inherited_blocked = _privileged_tools(set(default_tool_names), tool_metadata)
    if not inherited_blocked:
        return None
    return (
        f"Error: Cannot enable include_default_tools because defaults.tools "
        f"contains privileged tools: {', '.join(inherited_blocked)}"
    )


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

        if self.agent_name not in config.agents:
            return f"Error: Agent '{self.agent_name}' not found in configuration."

        agent = config.agents[self.agent_name]
        requested_blocked_fields = _requested_privileged_fields(
            context_files=context_files,
            knowledge_bases=knowledge_bases,
            model=model,
        )
        if requested_blocked_fields:
            return f"Error: Self-config cannot update privileged fields: {', '.join(requested_blocked_fields)}"

        # Validate tools against known tool metadata
        tool_metadata = resolved_tool_metadata_for_runtime(
            self.runtime_paths,
            config,
            tolerate_plugin_load_errors=True,
        )
        tool_update_error = _tool_update_error(
            tools,
            current_tool_names=agent.tool_names,
            tool_metadata=tool_metadata,
        )
        if tool_update_error:
            return tool_update_error

        # Block include_default_tools if defaults.tools contains privileged tools
        default_tools_update_error = _default_tools_update_error(
            include_default_tools,
            default_tool_names=config.defaults.tool_names,
            tool_metadata=tool_metadata,
        )
        if default_tools_update_error:
            return default_tools_update_error

        # Validate knowledge bases
        if knowledge_bases is not None:
            kb_error = validate_knowledge_bases(knowledge_bases, set(config.knowledge_bases))
            if kb_error:
                return kb_error

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
