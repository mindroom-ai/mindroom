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
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_SELF_CONFIG_MUTABLE_FIELDS = frozenset(
    {
        "compress_tool_results",
        "display_name",
        "instructions",
        "learning",
        "learning_mode",
        "markdown",
        "max_tool_calls_from_history",
        "num_history_messages",
        "num_history_runs",
        "role",
        "rooms",
        "show_tool_calls",
        "thread_mode",
    },
)
_CONFIG_CHANGE_REJECTED_MESSAGE = "Changes were NOT applied."


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

    def update_own_config(  # noqa: C901, PLR0911
        self,
        display_name: str | None = None,
        role: str | None = None,
        instructions: list[str] | None = None,
        rooms: list[str] | None = None,
        markdown: bool | None = None,
        learning: bool | None = None,
        learning_mode: AgentLearningMode | None = None,
        show_tool_calls: bool | None = None,
        thread_mode: Literal["thread", "room"] | None = None,
        num_history_runs: int | None = None,
        num_history_messages: int | None = None,
        compress_tool_results: bool | None = None,
        max_tool_calls_from_history: int | None = None,
    ) -> str:
        """Update this agent's own configuration. Only provided fields are changed.

        Args:
            display_name: Human-readable display name
            role: Description of the agent's purpose
            instructions: List of instructions for the agent
            rooms: List of room names to auto-join
            markdown: Whether to use markdown formatting
            learning: Whether to enable Agno Learning
            learning_mode: Learning mode ("always" or "agentic")
            show_tool_calls: Show tool call details inline in responses
            thread_mode: Conversation threading mode ("thread" or "room")
            num_history_runs: Number of prior runs to include as history
            num_history_messages: Max messages from history
            compress_tool_results: Compress tool results in history (disabled by default because it can invalidate Anthropic/Vertex Claude prompt caches)
            max_tool_calls_from_history: Max tool call messages replayed from history

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

        requested_updates: list[tuple[str, object]] = [
            ("display_name", display_name),
            ("role", role),
            ("instructions", instructions),
            ("rooms", rooms),
            ("markdown", markdown),
            ("learning", learning),
            ("learning_mode", learning_mode),
            ("show_tool_calls", show_tool_calls),
            ("thread_mode", thread_mode),
            ("num_history_runs", num_history_runs),
            ("num_history_messages", num_history_messages),
            ("compress_tool_results", compress_tool_results),
            ("max_tool_calls_from_history", max_tool_calls_from_history),
        ]
        privileged_updates = sorted(
            field_name
            for field_name, value in requested_updates
            if value is not None and field_name not in _SELF_CONFIG_MUTABLE_FIELDS
        )
        if privileged_updates:
            formatted_fields = ", ".join(field_name.replace("_", " ") for field_name in privileged_updates)
            return f"Error: Self-config cannot change privileged fields: {formatted_fields}"

        agent = config.agents[self.agent_name]
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
            if isinstance(validated_value, list):
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
