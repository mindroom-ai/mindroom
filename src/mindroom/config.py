"""Pydantic models for configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field

from .constants import DEFAULT_AGENTS_CONFIG, ROUTER_AGENT_NAME
from .logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


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
    model: str = Field(default="default", description="Model name")


class DefaultsConfig(BaseModel):
    """Default configuration values for agents."""

    num_history_runs: int = Field(default=5, description="Default number of history runs")
    markdown: bool = Field(default=True, description="Default markdown setting")
    add_history_to_messages: bool = Field(default=True, description="Default history setting")


class EmbedderConfig(BaseModel):
    """Configuration for memory embedder."""

    model: str = Field(default="text-embedding-3-small", description="Model name for embeddings")
    api_key: str | None = Field(default=None, description="API key (usually from environment variable)")
    host: str | None = Field(default=None, description="Host URL for self-hosted models like Ollama")


class MemoryEmbedderConfig(BaseModel):
    """Memory embedder configuration."""

    provider: str = Field(default="openai", description="Embedder provider (openai, huggingface, etc)")
    config: EmbedderConfig = Field(default_factory=EmbedderConfig, description="Provider-specific config")


class MemoryLLMConfig(BaseModel):
    """Memory LLM configuration."""

    provider: str = Field(default="ollama", description="LLM provider (ollama, openai, anthropic)")
    config: dict[str, Any] = Field(default_factory=dict, description="Provider-specific LLM config")


class MemoryConfig(BaseModel):
    """Memory system configuration."""

    embedder: MemoryEmbedderConfig = Field(
        default_factory=MemoryEmbedderConfig,
        description="Embedder configuration for memory",
    )
    llm: MemoryLLMConfig | None = Field(default=None, description="LLM configuration for memory")


class ModelConfig(BaseModel):
    """Configuration for an AI model."""

    provider: str = Field(description="Model provider (openai, anthropic, ollama, etc)")
    id: str = Field(description="Model ID specific to the provider")
    host: str | None = Field(default=None, description="Optional host URL (e.g., for Ollama)")
    api_key: str | None = Field(default=None, description="Optional API key (usually from env vars)")
    # Add other provider-specific fields as needed


class RouterConfig(BaseModel):
    """Configuration for the router system."""

    model: str = Field(default="default", description="Model to use for routing decisions")


class TeamConfig(BaseModel):
    """Configuration for a team of agents."""

    display_name: str = Field(description="Human-readable name for the team")
    role: str = Field(description="Description of the team's purpose")
    agents: list[str] = Field(description="List of agent names that compose this team")
    rooms: list[str] = Field(default_factory=list, description="List of room IDs or names to auto-join")
    model: str | None = Field(default=None, description="Default model for this team (optional)")
    mode: str = Field(default="coordinate", description="Team collaboration mode: coordinate or collaborate")


class Config(BaseModel):
    """Complete configuration from YAML."""

    agents: dict[str, AgentConfig] = Field(default_factory=dict, description="Agent configurations")
    teams: dict[str, TeamConfig] = Field(default_factory=dict, description="Team configurations")
    room_models: dict[str, str] = Field(default_factory=dict, description="Room-specific model overrides")
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig, description="Default values")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory configuration")
    models: dict[str, ModelConfig] = Field(default_factory=dict, description="Model configurations")
    router: RouterConfig = Field(default_factory=RouterConfig, description="Router configuration")

    @classmethod
    def from_yaml(cls, config_path: Path | None = None) -> Config:
        """Create a Config instance from YAML data."""
        path = config_path or DEFAULT_AGENTS_CONFIG

        if not path.exists():
            msg = f"Agent configuration file not found: {path}"
            raise FileNotFoundError(msg)

        with open(path) as f:
            data = yaml.safe_load(f)

        # Handle None values for optional dictionaries
        if data.get("teams") is None:
            data["teams"] = {}
        if data.get("room_models") is None:
            data["room_models"] = {}

        config = cls(**data)
        logger.info(f"Loaded agent configuration from {path}")
        logger.info(f"Found {len(config.agents)} agent configurations")
        return config

    def get_agent(self, agent_name: str) -> AgentConfig:
        """
        Get an agent configuration by name.

        Args:
            agent_name: Name of the agent

        Returns:
            Agent configuration

        Raises:
            ValueError: If agent not found

        """
        if agent_name not in self.agents:
            available = ", ".join(sorted(self.agents.keys()))
            msg = f"Unknown agent: {agent_name}. Available agents: {available}"
            raise ValueError(msg)
        return self.agents[agent_name]

    def get_all_configured_rooms(self) -> set[str]:
        """
        Extract all room aliases configured for agents and teams.

        Returns:
            Set of all unique room aliases from agent and team configurations

        """
        all_room_aliases = set()
        for agent_config in self.agents.values():
            all_room_aliases.update(agent_config.rooms)
        for team_config in self.teams.values():
            all_room_aliases.update(team_config.rooms)
        return all_room_aliases

    def get_configured_bots_for_room(self, room_id: str) -> set[str]:
        """
        Get the set of bot usernames that should be in a specific room.

        Args:
            room_id: The Matrix room ID

        Returns:
            Set of bot usernames (without domain) that should be in this room

        """
        from .matrix.rooms import resolve_room_aliases

        configured_bots = set()

        # Check which agents should be in this room
        for agent_name, agent_config in self.agents.items():
            resolved_rooms = set(resolve_room_aliases(agent_config.rooms))
            if room_id in resolved_rooms:
                configured_bots.add(f"mindroom_{agent_name}")

        # Check which teams should be in this room
        for team_name, team_config in self.teams.items():
            resolved_rooms = set(resolve_room_aliases(team_config.rooms))
            if room_id in resolved_rooms:
                configured_bots.add(f"mindroom_{team_name}")

        # Router should be in any room that has any configured agents/teams
        if configured_bots:  # If any bots are configured for this room
            configured_bots.add(f"mindroom_{ROUTER_AGENT_NAME}")

        return configured_bots

    def save_to_yaml(self, config_path: Path | None = None) -> None:
        """
        Save the config to a YAML file, excluding None values.

        Args:
            config_path: Path to save the config to. If None, uses DEFAULT_AGENTS_CONFIG.

        """
        path = config_path or DEFAULT_AGENTS_CONFIG
        config_dict = self.model_dump(exclude_none=True)
        with open(path, "w") as f:
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=True)
        logger.info(f"Saved configuration to {path}")
