"""Pydantic models for configuration."""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, Field

from .constants import DEFAULT_AGENTS_CONFIG, MATRIX_HOMESERVER, ROUTER_AGENT_NAME
from .logging_config import get_logger

if TYPE_CHECKING:
    from .matrix.identity import MatrixID

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
    show_stop_button: bool = Field(default=False, description="Whether to automatically show stop button on messages")


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
    extra_kwargs: dict[str, Any] | None = Field(
        default=None,
        description="Additional provider-specific parameters passed directly to the model",
    )


class RouterConfig(BaseModel):
    """Configuration for the router system."""

    model: str = Field(default="default", description="Model to use for routing decisions")


class TeamConfig(BaseModel):
    """Configuration for a team of agents."""

    display_name: str = Field(description="Human-readable name for the team")
    role: str = Field(description="Description of the team's purpose")
    agents: list[str] = Field(description="List of agent names that compose this team")
    rooms: list[str] = Field(default_factory=list, description="List of room IDs or names to auto-join")
    model: str | None = Field(default="default", description="Default model for this team (optional)")
    mode: str = Field(default="coordinate", description="Team collaboration mode: coordinate or collaborate")


class VoiceSTTConfig(BaseModel):
    """Configuration for voice speech-to-text."""

    provider: str = Field(default="openai", description="STT provider (openai or compatible)")
    model: str = Field(default="whisper-1", description="STT model name")
    api_key: str | None = Field(default=None, description="API key for STT service")
    host: str | None = Field(default=None, description="Host URL for self-hosted STT")


class VoiceLLMConfig(BaseModel):
    """Configuration for voice command intelligence."""

    model: str = Field(default="default", description="Model for command recognition")
    confidence_threshold: float = Field(default=0.7, description="Confidence threshold for commands")


class VoiceConfig(BaseModel):
    """Configuration for voice message handling."""

    enabled: bool = Field(default=False, description="Enable voice message processing")
    stt: VoiceSTTConfig = Field(default_factory=VoiceSTTConfig, description="STT configuration")
    intelligence: VoiceLLMConfig = Field(
        default_factory=VoiceLLMConfig,
        description="Command intelligence configuration",
    )


class AuthorizationConfig(BaseModel):
    """Authorization configuration with fine-grained permissions."""

    global_users: list[str] = Field(
        default_factory=list,
        description="Users with access to all rooms (e.g., '@user:example.com')",
    )
    room_permissions: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Room-specific user permissions. Keys are room IDs, values are lists of authorized user IDs",
    )
    default_room_access: bool = Field(
        default=False,
        description="Default permission for rooms not explicitly configured",
    )


class Config(BaseModel):
    """Complete configuration from YAML."""

    agents: dict[str, AgentConfig] = Field(default_factory=dict, description="Agent configurations")
    teams: dict[str, TeamConfig] = Field(default_factory=dict, description="Team configurations")
    room_models: dict[str, str] = Field(default_factory=dict, description="Room-specific model overrides")
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig, description="Default values")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory configuration")
    models: dict[str, ModelConfig] = Field(default_factory=dict, description="Model configurations")
    router: RouterConfig = Field(default_factory=RouterConfig, description="Router configuration")
    voice: VoiceConfig = Field(default_factory=VoiceConfig, description="Voice configuration")
    timezone: str = Field(
        default="UTC",
        description="Timezone for displaying scheduled tasks (e.g., 'America/New_York')",
    )
    authorization: AuthorizationConfig = Field(
        default_factory=AuthorizationConfig,
        description="Authorization configuration with fine-grained permissions",
    )

    @cached_property
    def domain(self) -> str:
        """Extract the domain from the MATRIX_HOMESERVER."""
        from .matrix.identity import extract_server_name_from_homeserver  # noqa: PLC0415

        return extract_server_name_from_homeserver(MATRIX_HOMESERVER)

    @cached_property
    def ids(self) -> dict[str, MatrixID]:
        """Get MatrixID objects for all agents and teams.

        Returns:
            Dictionary mapping agent/team names to their MatrixID objects.

        """
        from .matrix.identity import MatrixID  # noqa: PLC0415

        mapping: dict[str, MatrixID] = {}

        # Add all agents
        for agent_name in self.agents:
            mapping[agent_name] = MatrixID.from_agent(agent_name, self.domain)

        # Add router agent separately (it's not in config.agents)
        mapping[ROUTER_AGENT_NAME] = MatrixID.from_agent(ROUTER_AGENT_NAME, self.domain)

        # Add all teams
        for team_name in self.teams:
            mapping[team_name] = MatrixID.from_agent(team_name, self.domain)
        return mapping

    @classmethod
    def from_yaml(cls, config_path: Path | None = None) -> Config:
        """Create a Config instance from YAML data."""
        path = config_path or DEFAULT_AGENTS_CONFIG

        if not path.exists():
            msg = f"Agent configuration file not found: {path}"
            raise FileNotFoundError(msg)

        with path.open() as f:
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
            msg = f"Unknown agent: {agent_name}. Available agents: {available}"
            raise ValueError(msg)
        return self.agents[agent_name]

    def get_all_configured_rooms(self) -> set[str]:
        """Extract all room aliases configured for agents and teams.

        Returns:
            Set of all unique room aliases from agent and team configurations

        """
        all_room_aliases = set()
        for agent_config in self.agents.values():
            all_room_aliases.update(agent_config.rooms)
        for team_config in self.teams.values():
            all_room_aliases.update(team_config.rooms)
        return all_room_aliases

    def get_entity_model_name(self, entity_name: str) -> str:
        """Get the model name for an agent, team, or router.

        Args:
            entity_name: Name of the entity (agent, team, or router)

        Returns:
            Model name (e.g., "default", "gpt-4", etc.)

        Raises:
            ValueError: If entity_name is not found in configuration

        """
        # Router uses router model
        if entity_name == ROUTER_AGENT_NAME:
            return self.router.model
        # Teams use their configured model (required to have one)
        if entity_name in self.teams:
            model = self.teams[entity_name].model
            if model is None:
                msg = f"Team {entity_name} has no model configured"
                raise ValueError(msg)
            return model
        # Regular agents use their configured model
        if entity_name in self.agents:
            return self.agents[entity_name].model

        # Entity not found in any category
        available = sorted(set(self.agents.keys()) | set(self.teams.keys()) | {ROUTER_AGENT_NAME})
        msg = f"Unknown entity: {entity_name}. Available entities: {', '.join(available)}"
        raise ValueError(msg)

    def get_configured_bots_for_room(self, room_id: str) -> set[str]:
        """Get the set of bot usernames that should be in a specific room.

        Args:
            room_id: The Matrix room ID

        Returns:
            Set of bot usernames (without domain) that should be in this room

        """
        from .matrix.rooms import resolve_room_aliases  # noqa: PLC0415

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
        """Save the config to a YAML file, excluding None values.

        Args:
            config_path: Path to save the config to. If None, uses DEFAULT_AGENTS_CONFIG.

        """
        path = config_path or DEFAULT_AGENTS_CONFIG
        config_dict = self.model_dump(exclude_none=True)
        path_obj = Path(path)
        path_obj.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path_obj.with_suffix(path_obj.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            yaml.dump(
                config_dict,
                f,
                default_flow_style=False,
                sort_keys=True,
                allow_unicode=True,  # Preserve Unicode characters like ë
                width=120,  # Wider lines to reduce wrapping
            )
        tmp_path.replace(path_obj)
        logger.info(f"Saved configuration to {path}")
