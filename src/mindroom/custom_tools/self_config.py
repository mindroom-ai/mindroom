"""Self-config tool: lets an agent read and modify its own configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import yaml
from agno.tools import Toolkit
from pydantic import ValidationError

from mindroom.config import AgentConfig, AgentLearningMode, Config
from mindroom.constants import CONFIG_PATH
from mindroom.custom_tools.config_manager import validate_knowledge_bases
from mindroom.logging_config import get_logger
from mindroom.tools_metadata import TOOL_METADATA

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

_SELF_CONFIG_BLOCKED_TOOLS = {"config_manager"}


class SelfConfigTools(Toolkit):
    """Tools that let an agent read and modify its own configuration only."""

    def __init__(self, agent_name: str, config_path: Path | None = None) -> None:
        self.agent_name = agent_name
        self.config_path = config_path or CONFIG_PATH
        super().__init__(
            name="self_config",
            tools=[self.get_own_config, self.update_own_config],
        )

    def get_own_config(self) -> str:
        """Get this agent's current configuration as YAML.

        Returns:
            The agent's configuration formatted as YAML, or an error message.

        """
        try:
            config = Config.from_yaml(self.config_path)
        except Exception as e:
            return f"Error loading configuration: {e}"

        if self.agent_name not in config.agents:
            return f"Error: Agent '{self.agent_name}' not found in configuration."

        agent_dict = config.agents[self.agent_name].model_dump(exclude_none=True)
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
        enable_session_summaries: bool | None = None,
        max_tool_calls_from_history: int | None = None,
        context_files: list[str] | None = None,
        memory_dir: str | None = None,
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
            compress_tool_results: Compress tool results in history
            enable_session_summaries: Enable session summaries
            max_tool_calls_from_history: Max tool call messages replayed from history
            context_files: File paths read at agent init
            memory_dir: Directory containing memory files

        Returns:
            Success message with changes or an error message.

        """
        try:
            config = Config.from_yaml(self.config_path)
        except Exception as e:
            return f"Error loading configuration: {e}"

        if self.agent_name not in config.agents:
            return f"Error: Agent '{self.agent_name}' not found in configuration."

        # Validate tools against known tool metadata
        if tools is not None:
            invalid_tools = [t for t in tools if t not in TOOL_METADATA]
            if invalid_tools:
                return f"Error: Unknown tools: {', '.join(invalid_tools)}"
            blocked_tools = sorted({t for t in tools if t in _SELF_CONFIG_BLOCKED_TOOLS})
            if blocked_tools:
                return f"Error: Self-config cannot assign privileged tools: {', '.join(blocked_tools)}"

        # Block include_default_tools if defaults.tools contains privileged tools
        if include_default_tools is True:
            inherited_blocked = sorted({t for t in config.defaults.tools if t in _SELF_CONFIG_BLOCKED_TOOLS})
            if inherited_blocked:
                return (
                    f"Error: Cannot enable include_default_tools because defaults.tools "
                    f"contains privileged tools: {', '.join(inherited_blocked)}"
                )

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
            ("tools", tools),
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
            ("enable_session_summaries", enable_session_summaries),
            ("max_tool_calls_from_history", max_tool_calls_from_history),
            ("context_files", context_files),
            ("memory_dir", memory_dir),
        ]
        non_null_updates = {field_name: value for field_name, value in requested_updates if value is not None}

        candidate_agent_data = agent.model_dump()
        candidate_agent_data.update(non_null_updates)

        try:
            validated_agent = AgentConfig.model_validate(candidate_agent_data)
        except ValidationError as e:
            return f"Error validating configuration: {e}"

        updates: dict[str, str] = {}
        for field_name, new_value in requested_updates:
            if new_value is None:
                continue
            current_value = getattr(agent, field_name)
            validated_value = getattr(validated_agent, field_name)
            if validated_value == current_value:
                continue
            display = field_name.replace("_", " ").title()
            if isinstance(validated_value, list):
                formatted = ", ".join(str(v) for v in validated_value) if validated_value else "(empty)"
            else:
                formatted = str(validated_value)
            updates[display] = formatted

        if not updates:
            return "No changes made. All provided values match the current configuration."

        config.agents[self.agent_name] = validated_agent
        try:
            config.save_to_yaml(self.config_path)
        except Exception as e:
            return f"Error saving configuration: {e}"

        changes = "\n".join(f"- {name} -> {new}" for name, new in updates.items())
        return f"Successfully updated own configuration:\n\n{changes}"
