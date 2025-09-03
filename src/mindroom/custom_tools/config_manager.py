"""Consolidated ConfigManager tool for building and managing MindRoom agents."""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from agno.tools import Toolkit

from mindroom.commands import get_command_help
from mindroom.config import AgentConfig, Config, TeamConfig
from mindroom.constants import DEFAULT_AGENTS_CONFIG
from mindroom.logging_config import get_logger
from mindroom.tools_metadata import TOOL_METADATA, TOOL_REGISTRY, ToolCategory, ToolStatus

logger = get_logger(__name__)


class InfoType(str, Enum):
    """Types of information that can be retrieved."""

    MINDROOM_DOCS = "mindroom_docs"
    CONFIG_SCHEMA = "config_schema"
    AVAILABLE_MODELS = "available_models"
    AGENTS = "agents"
    TEAMS = "teams"
    AVAILABLE_TOOLS = "available_tools"
    TOOL_DETAILS = "tool_details"
    AGENT_CONFIG = "agent_config"
    AGENT_TEMPLATE = "agent_template"


class ConfigManagerTools(Toolkit):
    """Consolidated tools for managing MindRoom agent configurations.

    This toolkit provides comprehensive agent building capabilities with a minimal
    number of tools to reduce cognitive load on AI models.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        """Initialize the ConfigManager toolkit.

        Args:
            config_path: Optional path to configuration file

        """
        self.config_path = config_path or DEFAULT_AGENTS_CONFIG
        self._mindroom_docs: str | None = None
        self._help_text: str | None = None

        # Register only the consolidated tools
        super().__init__(
            name="config_manager",
            tools=[
                self.get_info,
                self.manage_agent,
                self.manage_team,
            ],
        )

    def get_info(  # noqa: C901, PLR0911, PLR0912
        self,
        info_type: str,
        name: str | None = None,
    ) -> str:
        """Get various types of information about MindRoom, agents, tools, and configuration.

        Args:
            info_type: Type of information to retrieve. Options:
                - "mindroom_docs": MindRoom documentation and help
                - "config_schema": Configuration schema for agents and teams
                - "available_models": List of configured AI models
                - "agents": List all configured agents
                - "teams": List all configured teams
                - "available_tools": List all available tools by category
                - "tool_details": Get details about a specific tool (requires name)
                - "agent_config": Get configuration for a specific agent (requires name)
                - "agent_template": Generate template for agent type (requires name as type)
            name: Optional name/identifier for specific queries (tool name, agent name, or template type)

        Returns:
            Requested information as formatted string

        """
        try:
            if info_type == InfoType.MINDROOM_DOCS:
                return self._get_mindroom_info()
            if info_type == InfoType.CONFIG_SCHEMA:
                return self._get_config_schema()
            if info_type == InfoType.AVAILABLE_MODELS:
                return self._get_available_models()
            if info_type == InfoType.AGENTS:
                return self._list_agents()
            if info_type == InfoType.TEAMS:
                return self._list_teams()
            if info_type == InfoType.AVAILABLE_TOOLS:
                return self._list_available_tools()
            if info_type == InfoType.TOOL_DETAILS:
                if not name:
                    return "Error: tool_details requires 'name' parameter with the tool name"
                return self._get_tool_details(name)
            if info_type == InfoType.AGENT_CONFIG:
                if not name:
                    return "Error: agent_config requires 'name' parameter with the agent name"
                return self._get_agent_config(name)
            if info_type == InfoType.AGENT_TEMPLATE:
                if not name:
                    return "Error: agent_template requires 'name' parameter with the template type (researcher, developer, social, communicator, analyst, productivity)"
                return self._generate_agent_template(name)
            return f"Error: Unknown info_type '{info_type}'. Valid options: {', '.join([t.value for t in InfoType])}"
        except Exception as e:
            logger.exception(f"Failed to get info for type {info_type}")
            return f"Error getting {info_type}: {e}"

    def manage_agent(
        self,
        operation: Literal["create", "update", "validate"],
        agent_name: str,
        display_name: str | None = None,
        role: str | None = None,
        tools: list[str] | None = None,
        instructions: list[str] | None = None,
        model: str | None = None,
        rooms: list[str] | None = None,
        num_history_runs: int | None = None,
        markdown: bool | None = None,
        add_history_to_messages: bool | None = None,
    ) -> str:
        """Manage agent configurations - create, update, or validate agents.

        Args:
            operation: Operation to perform - "create", "update", or "validate"
            agent_name: Internal name for the agent (alphanumeric, lowercase)
            display_name: Human-readable display name (required for create)
            role: Description of the agent's purpose (required for create)
            tools: List of tool names to enable for the agent
            instructions: List of instructions for the agent
            model: Model to use (default: "default")
            rooms: List of room IDs or names to auto-join
            num_history_runs: Number of history runs to include
            markdown: Whether to use markdown formatting
            add_history_to_messages: Whether to add history to messages

        Returns:
            Success message or error details

        """
        if operation == "create":
            if not display_name:
                return "Error: display_name is required for create operation"
            if role is None:
                role = ""
            return self._create_agent_config(
                agent_name=agent_name,
                display_name=display_name,
                role=role,
                tools=tools or [],
                instructions=instructions or [],
                model=model or "default",
                rooms=rooms or [],
                num_history_runs=num_history_runs,
                markdown=markdown,
                add_history_to_messages=add_history_to_messages,
            )
        if operation == "update":
            return self._update_agent_config(
                agent_name=agent_name,
                display_name=display_name,
                role=role,
                tools=tools,
                instructions=instructions,
                model=model,
                rooms=rooms,
                num_history_runs=num_history_runs,
                markdown=markdown,
                add_history_to_messages=add_history_to_messages,
            )
        if operation == "validate":
            return self._validate_agent_config(agent_name)
        return f"Error: Unknown operation '{operation}'. Valid options: create, update, validate"

    def manage_team(
        self,
        team_name: str,
        display_name: str,
        role: str,
        agents: list[str],
        mode: str = "coordinate",
    ) -> str:
        """Create or manage team configurations.

        Args:
            team_name: Internal name for the team
            display_name: Human-readable display name
            role: Description of the team's purpose
            agents: List of agent names that compose this team
            mode: Team mode - "coordinate" or "collaborate"

        Returns:
            Success message or error details

        """
        return self._create_team_config(team_name, display_name, role, agents, mode)

    # ===== Internal helper methods (not exposed as tools) =====

    def _load_mindroom_docs(self) -> str:
        """Load MindRoom documentation once and cache it."""
        if self._mindroom_docs is None:
            readme_path = Path(__file__).parent.parent.parent.parent / "README.md"
            try:
                with readme_path.open() as f:
                    self._mindroom_docs = f.read()
            except Exception as e:
                logger.warning(f"Could not load README.md: {e}")
                self._mindroom_docs = "README.md not available"
        return self._mindroom_docs

    def _load_help_text(self) -> str:
        """Load help text once and cache it."""
        if self._help_text is None:
            self._help_text = get_command_help()
        return self._help_text

    def _get_available_models(self) -> str:
        """Get the list of configured models from the current configuration."""
        try:
            config = Config.from_yaml(self.config_path)

            output = ["# Available Models\n"]

            if not config.models:
                return "No models configured in the system."

            output.append("These models are currently configured and can be used:\n")

            for model_name, model_config in config.models.items():
                provider = model_config.provider
                model_id = model_config.id

                output.append(f"## `{model_name}`")
                output.append(f"- **Provider**: {provider}")
                output.append(f"- **Model ID**: {model_id}")

                if model_config.host:
                    output.append(f"- **Host**: {model_config.host}")

                if model_name == "default":
                    output.append("- **Note**: This is typically the system default model")

                output.append("")

            if config.router and config.router.model:
                output.append("## Router Configuration")
                output.append(f"The router uses model: `{config.router.model}`")
                output.append("")

            return "\n".join(output)
        except Exception as e:
            return f"Error loading model configuration: {e}"

    def _format_schema_field(self, field: str, info: dict, required_fields: list) -> list[str]:
        """Format a single schema field for display."""
        lines = []
        required = field in required_fields
        field_type = info.get("type", "unknown")
        description = info.get("description", "")
        default = info.get("default")

        if "enum" in info:
            field_type = f"enum: {info['enum']}"

        lines.append(f"{field}:  # {field_type}")
        if description:
            lines.append(f"  # {description}")
        if required:
            lines.append("  # REQUIRED")
        elif default is not None:
            lines.append(f"  # Default: {default}")
        lines.append("")
        return lines

    def _get_config_schema(self) -> str:
        """Get the JSON schema for MindRoom configuration."""
        output = ["# MindRoom Configuration Schema\n"]

        agent_schema = AgentConfig.model_json_schema()
        team_schema = TeamConfig.model_json_schema()

        output.append("## Agent Configuration Fields\n")
        output.append("```yaml")
        output.append("# Required fields:")
        for field, info in agent_schema.get("properties", {}).items():
            output.extend(self._format_schema_field(field, info, agent_schema.get("required", [])))
        output.append("```\n")

        output.append("## Team Configuration Fields\n")
        output.append("```yaml")
        for field, info in team_schema.get("properties", {}).items():
            output.extend(self._format_schema_field(field, info, team_schema.get("required", [])))
        output.append("```\n")

        output.append("## Team Modes")
        if "properties" in team_schema and "mode" in team_schema["properties"]:
            mode_info = team_schema["properties"]["mode"]
            if "enum" in mode_info:
                output.extend(f"- `{mode}`: {mode.title()} mode" for mode in mode_info["enum"])

        return "\n".join(output)

    def _get_mindroom_info(self) -> str:
        """Get comprehensive information about MindRoom."""
        docs = self._load_mindroom_docs()
        help_text = self._load_help_text()

        return f"""# MindRoom Documentation

## README Content:
{docs}

## Available Commands:
{help_text}

## Key Concepts:
- **Agents**: AI assistants with specific roles and tools
- **Teams**: Groups of agents that collaborate
- **Tools**: Integrations that give agents capabilities (80+ available)
- **Memory**: Persistent conversation memory across sessions
- **Threading**: Agents respond in threads for organized conversations
- **Routing**: Smart agent selection based on message content
- **Commands**: Special !commands for configuration and control
"""

    def _list_agents(self) -> str:
        """List all configured agents and their details."""
        try:
            config = Config.from_yaml(self.config_path)
            agents_info = []

            for name, agent in config.agents.items():
                tools_str = ", ".join(agent.tools) if agent.tools else "No tools"
                role_line = f"  - Role: {agent.role[:100]}..." if len(agent.role) > 100 else f"  - Role: {agent.role}"
                agents_info.append(
                    f"**{name}** ({agent.display_name})\n"
                    f"{role_line}\n"
                    f"  - Tools: {tools_str}\n"
                    f"  - Model: {agent.model}\n",
                )

            if not agents_info:
                return "No agents configured yet."

            return "## Configured Agents:\n\n" + "\n".join(agents_info)
        except Exception as e:
            return f"Error loading agents: {e}"

    def _list_teams(self) -> str:
        """List all configured teams and their composition."""
        try:
            config = Config.from_yaml(self.config_path)
            teams_info = []

            for name, team in config.teams.items():
                agents_str = ", ".join(team.agents)
                teams_info.append(
                    f"**{name}** ({team.display_name})\n"
                    f"  - Role: {team.role}\n"
                    f"  - Agents: {agents_str}\n"
                    f"  - Mode: {team.mode}\n",
                )

            if not teams_info:
                return "No teams configured yet."

            return "## Configured Teams:\n\n" + "\n".join(teams_info)
        except Exception as e:
            return f"Error loading teams: {e}"

    def _list_available_tools(self) -> str:
        """List all available tools that can be used by agents."""
        tools_by_category: dict[str, list[tuple[str, str]]] = {}

        for tool_name in sorted(TOOL_REGISTRY.keys()):
            if tool_name in TOOL_METADATA:
                metadata = TOOL_METADATA[tool_name]
                category = metadata.category.value
                description = metadata.description

                if category not in tools_by_category:
                    tools_by_category[category] = []
                tools_by_category[category].append((tool_name, description))
            else:
                if "uncategorized" not in tools_by_category:
                    tools_by_category["uncategorized"] = []
                tools_by_category["uncategorized"].append((tool_name, "No description available"))

        output = ["## Available Tools by Category:\n"]
        for category in sorted(tools_by_category.keys()):
            output.append(f"\n### {category.title()}:")
            for tool_name, description in tools_by_category[category]:
                output.append(f"- **{tool_name}**: {description}")

        return "\n".join(output)

    def _get_tool_details(self, tool_name: str) -> str:
        """Get detailed information about a specific tool."""
        if tool_name not in TOOL_REGISTRY:
            available = ", ".join(sorted(TOOL_REGISTRY.keys()))
            return f"Unknown tool: {tool_name}\n\nAvailable tools: {available}"

        output = [f"## Tool: {tool_name}\n"]

        if tool_name in TOOL_METADATA:
            metadata = TOOL_METADATA[tool_name]
            output.append(f"**Display Name**: {metadata.display_name}")
            output.append(f"**Description**: {metadata.description}")
            output.append(f"**Category**: {metadata.category.value}")
            output.append(f"**Status**: {metadata.status.value}")
            output.append(f"**Setup Type**: {metadata.setup_type.value}")

            if metadata.config_fields:
                output.append("\n**Configuration Fields**:")
                for field in metadata.config_fields:
                    required = "Required" if field.required else "Optional"
                    output.append(f"- **{field.name}** ({field.type}, {required}): {field.description}")
                    if field.default is not None:
                        output.append(f"  Default: {field.default}")

            if metadata.dependencies:
                output.append(f"\n**Dependencies**: {', '.join(metadata.dependencies)}")

            if metadata.docs_url:
                output.append(f"\n**Documentation**: {metadata.docs_url}")
        else:
            output.append("No metadata available for this tool.")

        return "\n".join(output)

    def _create_agent_config(
        self,
        agent_name: str,
        display_name: str,
        role: str,
        tools: list[str],
        instructions: list[str],
        model: str,
        rooms: list[str],
        num_history_runs: int | None,
        markdown: bool | None,
        add_history_to_messages: bool | None,
    ) -> str:
        """Create a new agent configuration."""
        # Validate agent name
        if not re.match(r"^[a-z0-9_]+$", agent_name):
            return "Error: Agent name must be lowercase alphanumeric with underscores only"

        # Validate tools
        invalid_tools = [t for t in tools if t not in TOOL_REGISTRY]
        if invalid_tools:
            return f"Error: Unknown tools: {', '.join(invalid_tools)}\n\nUse get_info with info_type='available_tools' to see valid tools."

        try:
            config = Config.from_yaml(self.config_path)

            if agent_name in config.agents:
                return f"Error: Agent '{agent_name}' already exists. Use manage_agent with operation='update' to modify it."

            # Create new agent config
            new_agent = AgentConfig(
                display_name=display_name,
                role=role,
                tools=tools,
                instructions=instructions,
                model=model,
                rooms=rooms,
                num_history_runs=num_history_runs,
                markdown=markdown,
                add_history_to_messages=add_history_to_messages,
            )

            # Add to config
            config.agents[agent_name] = new_agent

            # Save config
            config.save_to_yaml(self.config_path)

            # Build success message
            tools_str = ", ".join(tools) if tools else "None"
            rooms_str = ", ".join(rooms) if rooms else "None"
            return (  # noqa: TRY300
                f"✅ Successfully created agent '{agent_name}'!\n\n"
                f"**Configuration:**\n"
                f"- Display Name: {display_name}\n"
                f"- Role: {role}\n"
                f"- Tools: {tools_str}\n"
                f"- Model: {model}\n"
                f"- Rooms: {rooms_str}\n\n"
                f"The agent is now available and can be mentioned with @{agent_name}"
            )
        except Exception as e:
            logger.exception("Failed to create agent")
            return f"Error creating agent: {e}"

    def _update_agent_config(
        self,
        agent_name: str,
        display_name: str | None,
        role: str | None,
        tools: list[str] | None,
        instructions: list[str] | None,
        model: str | None,
        rooms: list[str] | None,
        num_history_runs: int | None,
        markdown: bool | None,
        add_history_to_messages: bool | None,
    ) -> str:
        """Update an existing agent configuration."""
        try:
            config = Config.from_yaml(self.config_path)

            if agent_name not in config.agents:
                return f"Error: Agent '{agent_name}' not found. Use manage_agent with operation='create' to create it."

            agent = config.agents[agent_name]

            # Validate tools if provided
            if tools is not None:
                invalid_tools = [t for t in tools if t not in TOOL_REGISTRY]
                if invalid_tools:
                    return f"Error: Unknown tools: {', '.join(invalid_tools)}"

            # Map of field names to (new_value, display_formatter)
            updates = {
                "display_name": (display_name, lambda v: v),
                "role": (role, lambda v: v),
                "tools": (tools, lambda v: ", ".join(v) if v else "(empty)"),
                "instructions": (instructions, lambda v: f"{len(v)} instructions" if v else "(empty)"),
                "model": (model, lambda v: v),
                "rooms": (rooms, lambda v: ", ".join(v) if v else "(empty)"),
                "num_history_runs": (num_history_runs, lambda v: str(v)),
                "markdown": (markdown, lambda v: str(v)),
                "add_history_to_messages": (add_history_to_messages, lambda v: str(v)),
            }

            # Apply updates and track changes
            changes = []
            for field_name, (new_value, formatter) in updates.items():
                if new_value is not None:
                    current_value = getattr(agent, field_name)
                    if new_value != current_value:
                        setattr(agent, field_name, new_value)
                        display_name = field_name.replace("_", " ").title()
                        changes.append(f"{display_name} -> {formatter(new_value)}")

            if not changes:
                return "No changes made. All provided values are the same as current configuration."

            # Save config
            config.save_to_yaml(self.config_path)

            return f"✅ Successfully updated agent '{agent_name}'!\n\n**Changes:**\n" + "\n".join(
                f"- {c}" for c in changes
            )
        except Exception as e:
            logger.exception("Failed to update agent")
            return f"Error updating agent: {e}"

    def _create_team_config(
        self,
        team_name: str,
        display_name: str,
        role: str,
        agents: list[str],
        mode: str = "coordinate",
    ) -> str:
        """Create a new team configuration."""
        if mode not in ["coordinate", "collaborate"]:
            return "Error: Team mode must be 'coordinate' or 'collaborate'"

        try:
            config = Config.from_yaml(self.config_path)

            if team_name in config.teams:
                return f"Error: Team '{team_name}' already exists."

            # Validate agents exist
            invalid_agents = [a for a in agents if a not in config.agents]
            if invalid_agents:
                return f"Error: Unknown agents: {', '.join(invalid_agents)}"

            # Create new team config
            new_team = TeamConfig(
                display_name=display_name,
                role=role,
                agents=agents,
                mode=mode,
            )

            # Add to config
            config.teams[team_name] = new_team

            # Save config
            config.save_to_yaml(self.config_path)

            return (
                f"✅ Successfully created team '{team_name}'!\n\n"
                f"**Configuration:**\n"
                f"- Display Name: {display_name}\n"
                f"- Role: {role}\n"
                f"- Agents: {', '.join(agents)}\n"
                f"- Mode: {mode}\n\n"
                f"The team can now be mentioned with @{team_name}"
            )
        except Exception as e:
            logger.exception("Failed to create team")
            return f"Error creating team: {e}"

    def _validate_agent_config(self, agent_name: str) -> str:  # noqa: C901, PLR0912
        """Validate an agent's configuration."""
        try:
            config = Config.from_yaml(self.config_path)

            if agent_name not in config.agents:
                return f"Error: Agent '{agent_name}' not found."

            agent = config.agents[agent_name]
            issues = []
            warnings = []

            # Check display name
            if not agent.display_name:
                issues.append("Missing display name")

            # Check role
            if not agent.role:
                warnings.append("No role description provided")
            elif len(agent.role) < 20:
                warnings.append("Role description is very short")

            # Check tools
            if not agent.tools:
                warnings.append("No tools configured")
            else:
                invalid_tools = [t for t in agent.tools if t not in TOOL_REGISTRY]
                if invalid_tools:
                    issues.append(f"Invalid tools: {', '.join(invalid_tools)}")

            # Check model
            available_models = list(config.models.keys()) if config.models else []
            if available_models and agent.model not in available_models:
                warnings.append(f"Model '{agent.model}' not in configured models: {', '.join(available_models)}")

            # Format results
            output = [f"## Validation Results for '{agent_name}':\n"]

            if not issues and not warnings:
                output.append("✅ Configuration is valid!")
            else:
                if issues:
                    output.append("### ❌ Issues (must fix):")
                    output.extend(f"- {issue}" for issue in issues)

                if warnings:
                    output.append("\n### ⚠️ Warnings (consider fixing):")
                    output.extend(f"- {warning}" for warning in warnings)

            # Add summary
            output.append("\n### Configuration Summary:")
            output.append(f"- Display Name: {agent.display_name}")
            output.append(f"- Role: {agent.role[:100]}..." if len(agent.role) > 100 else f"- Role: {agent.role}")
            output.append(f"- Tools: {', '.join(agent.tools) if agent.tools else 'None'}")
            output.append(f"- Model: {agent.model}")

            return "\n".join(output)
        except Exception as e:
            return f"Error validating agent: {e}"

    def _get_agent_config(self, agent_name: str) -> str:
        """Get the full configuration for a specific agent."""
        try:
            config = Config.from_yaml(self.config_path)

            if agent_name not in config.agents:
                return f"Error: Agent '{agent_name}' not found."

            agent = config.agents[agent_name]
            agent_dict = agent.model_dump(exclude_none=True)

            yaml_str = yaml.dump(agent_dict, default_flow_style=False, sort_keys=False)
        except Exception as e:
            return f"Error loading agent config: {e}"
        else:
            return f"## Configuration for '{agent_name}':\n\n```yaml\n{yaml_str}```"

    def _generate_agent_template(self, agent_type: str) -> str:
        """Generate a template configuration for common agent types."""
        # Map agent types to tool categories
        type_to_category = {
            "researcher": ToolCategory.RESEARCH,
            "developer": ToolCategory.DEVELOPMENT,
            "social": ToolCategory.SOCIAL,
            "communicator": ToolCategory.COMMUNICATION,
            "analyst": ToolCategory.INFORMATION,
            "productivity": ToolCategory.PRODUCTIVITY,
        }

        if agent_type not in type_to_category:
            available = ", ".join(type_to_category.keys())
            return f"Unknown template type: {agent_type}\n\nAvailable templates: {available}"

        category = type_to_category[agent_type]

        # Get tools from this category that are available
        tools = [
            name
            for name, metadata in TOOL_METADATA.items()
            if metadata.category == category and metadata.status == ToolStatus.AVAILABLE
        ][:5]  # Limit to 5 tools

        # Generate role based on category
        role_descriptions = {
            ToolCategory.RESEARCH: "Research specialist focused on finding and analyzing information",
            ToolCategory.DEVELOPMENT: "Software development expert for coding and technical tasks",
            ToolCategory.SOCIAL: "Social interaction specialist for community engagement",
            ToolCategory.COMMUNICATION: "Communication expert for messaging and collaboration",
            ToolCategory.INFORMATION: "Information analyst for data processing and insights",
            ToolCategory.PRODUCTIVITY: "Productivity specialist for task and workflow management",
        }

        role = role_descriptions.get(category, f"Specialist in {category.value} tasks")

        # Generic instructions
        instructions = [
            f"Focus on {category.value} tasks",
            "Provide clear and actionable responses",
            "Use available tools effectively",
        ]

        return f"""## Template for '{agent_type}' agent:

```yaml
display_name: "{agent_type.title()} Agent"
role: "{role}"
tools: {yaml.dump(tools, default_flow_style=True).strip() if tools else "[]"}
instructions: {yaml.dump(instructions, default_flow_style=False).strip()}
model: "default"
```

**Available tools in {category.value} category:**
{chr(10).join(f"- {name}: {metadata.description}" for name, metadata in TOOL_METADATA.items() if metadata.category == category)}

**To create this agent, use:**
```
manage_agent(
    operation="create",
    agent_name="{agent_type}_agent",
    display_name="{agent_type.title()} Agent",
    role="{role}",
    tools={tools},
    instructions={instructions},
)
```"""
