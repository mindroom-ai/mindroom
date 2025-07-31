"""Pydantic models for agent configuration."""

from pydantic import BaseModel, Field


class AgentConfig(BaseModel):
    """Configuration for a single agent."""

    display_name: str = Field(description="Human-readable name for the agent")
    role: str = Field(default="", description="Description of the agent's purpose")
    tools: list[str] = Field(default_factory=list, description="List of tool names")
    instructions: list[str] = Field(default_factory=list, description="Agent instructions")
    rooms: list[str] = Field(default_factory=list, description="List of room IDs or names to auto-join")
    num_history_runs: int | None = Field(default=None, description="Number of history runs to include")
    markdown: bool | None = Field(default=None, description="Whether to use markdown formatting")
    add_history_to_messages: bool | None = Field(default=None, description="Whether to add history to messages")


class DefaultsConfig(BaseModel):
    """Default configuration values for agents."""

    num_history_runs: int = Field(default=5, description="Default number of history runs")
    markdown: bool = Field(default=True, description="Default markdown setting")
    add_history_to_messages: bool = Field(default=True, description="Default history setting")


class AgentsConfig(BaseModel):
    """Complete agent configuration from YAML."""

    agents: dict[str, AgentConfig] = Field(default_factory=dict, description="Agent configurations")
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig, description="Default values")

    def get_agent(self, agent_name: str) -> AgentConfig:
        """Get an agent configuration by name.

        Args:
            agent_name: Name of the agent

        Returns:
            Agent configuration

        Raises:
            ValueError: If agent not found
        """
        if agent_name not in self.agents:
            available = ", ".join(sorted(self.agents.keys()))
            raise ValueError(f"Unknown agent: {agent_name}. Available agents: {available}")
        return self.agents[agent_name]

    def list_agents(self) -> list[str]:
        """Get sorted list of agent names."""
        return sorted(self.agents.keys())
