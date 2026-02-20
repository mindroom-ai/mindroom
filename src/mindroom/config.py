"""Pydantic models for configuration."""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .constants import CONFIG_PATH, MATRIX_HOMESERVER, ROUTER_AGENT_NAME, safe_replace
from .logging_config import get_logger

if TYPE_CHECKING:
    from .matrix.identity import MatrixID

logger = get_logger(__name__)

AgentLearningMode = Literal["always", "agentic"]
CultureMode = Literal["automatic", "agentic", "manual"]
MATRIX_LOCALPART_PATTERN = re.compile(r"^[a-z0-9._=/-]+$")
AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
DEFAULT_DEFAULT_TOOLS = ("scheduler",)


class AgentConfig(BaseModel):
    """Configuration for a single agent."""

    display_name: str = Field(description="Human-readable name for the agent")
    role: str = Field(default="", description="Description of the agent's purpose")
    tools: list[str] = Field(default_factory=list, description="List of tool names")
    include_default_tools: bool = Field(
        default=True,
        description="Whether to merge defaults.tools into this agent's tools",
    )
    skills: list[str] = Field(default_factory=list, description="List of skill names")
    instructions: list[str] = Field(default_factory=list, description="Agent instructions")
    rooms: list[str] = Field(default_factory=list, description="List of room IDs or names to auto-join")
    markdown: bool | None = Field(default=None, description="Whether to use markdown formatting")
    learning: bool | None = Field(default=None, description="Enable Agno Learning (defaults to true when omitted)")
    learning_mode: AgentLearningMode | None = Field(
        default=None,
        description="Learning mode for Agno Learning: always (automatic) or agentic (tool-driven)",
    )
    model: str = Field(default="default", description="Model name")
    knowledge_bases: list[str] = Field(
        default_factory=list,
        description="Knowledge base IDs assigned to this agent",
    )
    context_files: list[str] = Field(
        default_factory=list,
        description="File paths read at agent init and prepended to role context",
    )
    memory_dir: str | None = Field(
        default=None,
        description="Directory containing MEMORY.md and dated memory files to auto-load into role context",
    )
    thread_mode: Literal["thread", "room"] = Field(
        default="thread",
        description="Conversation threading mode: 'thread' creates Matrix threads per conversation, 'room' uses a single continuous conversation per room (ideal for bridges/mobile)",
    )
    num_history_runs: int | None = Field(
        default=None,
        description="Number of prior Agno runs to include as history context (per-agent override)",
    )
    num_history_messages: int | None = Field(
        default=None,
        description="Max messages from history (mutually exclusive with num_history_runs)",
    )
    compress_tool_results: bool | None = Field(
        default=None,
        description="Compress tool results in history to save context (per-agent override)",
    )
    enable_session_summaries: bool | None = Field(
        default=None,
        description="Enable Agno session summaries for conversation compaction (per-agent override)",
    )
    max_tool_calls_from_history: int | None = Field(
        default=None,
        ge=0,
        description="Max tool call messages replayed from history (per-agent override)",
    )
    show_tool_calls: bool | None = Field(
        default=None,
        description="Whether to show tool call details inline in responses (per-agent override)",
    )
    sandbox_tools: list[str] | None = Field(
        default=None,
        description="Tool names to execute through sandbox proxy (overrides defaults; None = inherit)",
    )
    delegate_to: list[str] = Field(
        default_factory=list,
        description="List of agent names this agent can delegate tasks to via tool calls",
    )

    @model_validator(mode="after")
    def _check_history_config(self) -> Self:
        if self.num_history_runs is not None and self.num_history_messages is not None:
            msg = "num_history_runs and num_history_messages are mutually exclusive"
            raise ValueError(msg)
        return self

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_knowledge_base_field(cls, data: object) -> object:
        """Reject legacy single knowledge_base field to prevent silent misconfiguration."""
        if isinstance(data, dict) and "knowledge_base" in data:
            msg = "Agent field 'knowledge_base' was removed. Use 'knowledge_bases' (list) instead."
            raise ValueError(msg)
        return data

    @field_validator("knowledge_bases")
    @classmethod
    def validate_unique_knowledge_bases(cls, knowledge_bases: list[str]) -> list[str]:
        """Ensure each knowledge base assignment appears at most once per agent."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for base_id in knowledge_bases:
            if base_id in seen and base_id not in duplicates:
                duplicates.append(base_id)
            seen.add(base_id)

        if duplicates:
            msg = f"Duplicate knowledge bases are not allowed: {', '.join(duplicates)}"
            raise ValueError(msg)
        return knowledge_bases


class DefaultsConfig(BaseModel):
    """Default configuration values for agents."""

    tools: list[str] = Field(
        default_factory=lambda: list(DEFAULT_DEFAULT_TOOLS),
        description="Tool names automatically added to every agent",
    )
    markdown: bool = Field(default=True, description="Default markdown setting")
    enable_streaming: bool = Field(
        default=True,
        description="Enable streaming responses via progressive message edits",
    )
    show_stop_button: bool = Field(default=False, description="Whether to automatically show stop button on messages")
    learning: bool = Field(default=True, description="Default Agno Learning setting")
    learning_mode: AgentLearningMode = Field(default="always", description="Default Agno Learning mode")
    num_history_runs: int | None = Field(
        default=None,
        description="Default number of prior Agno runs to include as history context (None = all)",
    )
    num_history_messages: int | None = Field(
        default=None,
        description="Default max messages from history (mutually exclusive with num_history_runs)",
    )
    compress_tool_results: bool = Field(
        default=True,
        description="Compress tool results in history to save context",
    )
    enable_session_summaries: bool = Field(
        default=False,
        description="Enable Agno session summaries for conversation compaction",
    )
    max_tool_calls_from_history: int | None = Field(
        default=None,
        ge=0,
        description="Max tool call messages replayed from history (None = no limit)",
    )
    show_tool_calls: bool = Field(
        default=True,
        description="Whether to show tool call details inline in responses",
    )
    sandbox_tools: list[str] | None = Field(
        default=None,
        description="Tool names to sandbox by default for all agents (None = use env var config)",
    )
    max_preload_chars: int = Field(
        default=50000,
        ge=1,
        description="Hard cap for extra role preload context loaded from context_files and memory_dir",
    )

    @model_validator(mode="after")
    def _check_history_config(self) -> Self:
        if self.num_history_runs is not None and self.num_history_messages is not None:
            msg = "num_history_runs and num_history_messages are mutually exclusive"
            raise ValueError(msg)
        return self

    @field_validator("tools")
    @classmethod
    def validate_unique_tools(cls, tools: list[str]) -> list[str]:
        """Ensure each default tool appears at most once."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for tool_name in tools:
            if tool_name in seen and tool_name not in duplicates:
                duplicates.append(tool_name)
            seen.add(tool_name)

        if duplicates:
            msg = f"Duplicate default tools are not allowed: {', '.join(duplicates)}"
            raise ValueError(msg)
        return tools


class EmbedderConfig(BaseModel):
    """Configuration for memory embedder."""

    model: str = Field(default="text-embedding-3-small", description="Model name for embeddings")
    api_key: str | None = Field(default=None, description="API key (usually from environment variable)")
    host: str | None = Field(default=None, description="Host URL for self-hosted models (Ollama, llama.cpp, etc.)")


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


class KnowledgeGitConfig(BaseModel):
    """Git repository synchronization settings for a knowledge base."""

    repo_url: str = Field(description="Git repository URL used as the knowledge source")
    branch: str = Field(default="main", description="Git branch to track")
    poll_interval_seconds: int = Field(
        default=300,
        ge=5,
        description="How often to poll the remote repository for updates",
    )
    credentials_service: str | None = Field(
        default=None,
        description="Optional CredentialsManager service name used for private HTTPS repos",
    )
    skip_hidden: bool = Field(
        default=True,
        description="Skip hidden files/folders (paths with components starting with '.') during indexing",
    )
    include_patterns: list[str] = Field(
        default_factory=list,
        description="Optional root-anchored glob patterns to include (e.g. 'content/post/*/index.md')",
    )
    exclude_patterns: list[str] = Field(
        default_factory=list,
        description="Optional root-anchored glob patterns to exclude after include filtering",
    )


class KnowledgeBaseConfig(BaseModel):
    """Knowledge base configuration."""

    path: str = Field(default="./knowledge_docs", description="Path to knowledge documents folder")
    watch: bool = Field(default=True, description="Watch folder for changes")
    git: KnowledgeGitConfig | None = Field(
        default=None,
        description="Optional Git sync configuration for this knowledge base",
    )


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
    context_window: int | None = Field(
        default=None,
        ge=1,
        description="Context window size in tokens; when set, history is dynamically reduced toward an 80% target of this limit",
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


class CultureConfig(BaseModel):
    """Configuration for a shared culture."""

    description: str = Field(default="", description="Description of shared principles and practices")
    agents: list[str] = Field(default_factory=list, description="List of agent names assigned to this culture")
    mode: CultureMode = Field(
        default="automatic",
        description="Culture update mode: automatic, agentic, or manual",
    )

    @field_validator("agents")
    @classmethod
    def validate_unique_agents(cls, agents: list[str]) -> list[str]:
        """Ensure each agent is assigned at most once per culture."""
        seen: set[str] = set()
        duplicates: list[str] = []
        for agent_name in agents:
            if agent_name in seen and agent_name not in duplicates:
                duplicates.append(agent_name)
            seen.add(agent_name)

        if duplicates:
            msg = f"Duplicate agents are not allowed in a culture: {', '.join(duplicates)}"
            raise ValueError(msg)
        return agents


class VoiceSTTConfig(BaseModel):
    """Configuration for voice speech-to-text."""

    provider: str = Field(default="openai", description="STT provider (openai or compatible)")
    model: str = Field(default="whisper-1", description="STT model name")
    api_key: str | None = Field(default=None, description="API key for STT service")
    host: str | None = Field(default=None, description="Host URL for self-hosted STT")


class VoiceLLMConfig(BaseModel):
    """Configuration for voice command intelligence."""

    model: str = Field(default="default", description="Model for command recognition")


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
    aliases: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Map canonical Matrix user IDs to bridge aliases. "
            "A message from any alias is treated as if sent by the canonical user. "
            "E.g., {'@alice:example.com': ['@telegram_123:example.com']}"
        ),
    )

    @field_validator("aliases")
    @classmethod
    def validate_unique_aliases(cls, aliases: dict[str, list[str]]) -> dict[str, list[str]]:
        """Ensure each alias is assigned to at most one canonical user."""
        seen_aliases: set[str] = set()
        duplicates: list[str] = []
        for alias_list in aliases.values():
            for alias in alias_list:
                if alias in seen_aliases and alias not in duplicates:
                    duplicates.append(alias)
                seen_aliases.add(alias)

        if duplicates:
            msg = f"Duplicate bridge aliases are not allowed: {', '.join(duplicates)}"
            raise ValueError(msg)
        return aliases

    def resolve_alias(self, sender_id: str) -> str:
        """Return the canonical user ID for a bridge alias, or the sender_id itself."""
        for canonical, alias_list in self.aliases.items():
            if sender_id in alias_list:
                return canonical
        return sender_id


class MindRoomUserConfig(BaseModel):
    """Configuration for the internal MindRoom user account."""

    username: str = Field(
        default="mindroom_user",
        description="Matrix username localpart for the internal user account (without @ or domain); set before first startup",
    )
    display_name: str = Field(
        default="MindRoomUser",
        description="Display name for the internal user account",
    )

    @field_validator("username")
    @classmethod
    def validate_username(cls, username: str) -> str:
        """Validate and normalize Matrix localpart for the internal user."""
        normalized = username.strip().removeprefix("@")

        if not normalized:
            msg = "mindroom_user.username cannot be empty"
            raise ValueError(msg)

        if "@" in normalized:
            msg = "mindroom_user.username must contain at most one leading @"
            raise ValueError(msg)

        if ":" in normalized:
            msg = "mindroom_user.username must be a Matrix localpart (without domain)"
            raise ValueError(msg)

        if not MATRIX_LOCALPART_PATTERN.fullmatch(normalized):
            msg = (
                "mindroom_user.username contains invalid characters; "
                "allowed: lowercase letters, digits, '.', '_', '=', '-', '/'"
            )
            raise ValueError(msg)

        return normalized


class Config(BaseModel):
    """Complete configuration from YAML."""

    agents: dict[str, AgentConfig] = Field(default_factory=dict, description="Agent configurations")
    teams: dict[str, TeamConfig] = Field(default_factory=dict, description="Team configurations")
    cultures: dict[str, CultureConfig] = Field(default_factory=dict, description="Culture configurations")
    room_models: dict[str, str] = Field(default_factory=dict, description="Room-specific model overrides")
    plugins: list[str] = Field(default_factory=list, description="Plugin paths")
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig, description="Default values")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory configuration")
    knowledge_bases: dict[str, KnowledgeBaseConfig] = Field(
        default_factory=dict,
        description="Knowledge base configurations keyed by base ID",
    )
    models: dict[str, ModelConfig] = Field(default_factory=dict, description="Model configurations")
    router: RouterConfig = Field(default_factory=RouterConfig, description="Router configuration")
    voice: VoiceConfig = Field(default_factory=VoiceConfig, description="Voice configuration")
    timezone: str = Field(
        default="UTC",
        description="Timezone for displaying scheduled tasks (e.g., 'America/New_York')",
    )
    mindroom_user: MindRoomUserConfig = Field(
        default_factory=MindRoomUserConfig,
        description="Configuration for the internal MindRoom user account",
    )
    authorization: AuthorizationConfig = Field(
        default_factory=AuthorizationConfig,
        description="Authorization configuration with fine-grained permissions",
    )
    bot_accounts: list[str] = Field(
        default_factory=list,
        description="Matrix user IDs of non-MindRoom bots (e.g., bridge bots) that should be treated like agents for response logic — their messages won't trigger the multi-human-thread mention requirement",
    )

    @model_validator(mode="after")
    def validate_entity_names(self) -> Config:
        """Ensure agent and team names contain only alphanumeric characters and underscores."""
        invalid_agents = [name for name in self.agents if not AGENT_NAME_PATTERN.fullmatch(name)]
        invalid_teams = [name for name in self.teams if not AGENT_NAME_PATTERN.fullmatch(name)]
        invalid = sorted(invalid_agents + invalid_teams)
        if invalid:
            msg = f"Agent/team names must be alphanumeric/underscore only, got: {', '.join(invalid)}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_delegate_to(self) -> Config:
        """Ensure delegate_to targets exist and agents don't delegate to themselves."""
        for agent_name, agent_config in self.agents.items():
            for target in agent_config.delegate_to:
                if target == agent_name:
                    msg = f"Agent '{agent_name}' cannot delegate to itself"
                    raise ValueError(msg)
                if target not in self.agents:
                    msg = f"Agent '{agent_name}' delegates to unknown agent '{target}'"
                    raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_knowledge_base_assignments(self) -> Config:
        """Ensure agents only reference configured knowledge base IDs."""
        invalid_assignments = [
            (agent_name, base_id)
            for agent_name, agent_config in self.agents.items()
            for base_id in agent_config.knowledge_bases
            if base_id not in self.knowledge_bases
        ]
        if invalid_assignments:
            formatted = ", ".join(
                f"{agent_name} -> {base_id}"
                for agent_name, base_id in sorted(invalid_assignments, key=lambda item: (item[0], item[1]))
            )
            msg = f"Agents reference unknown knowledge bases: {formatted}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_culture_assignments(self) -> Config:
        """Ensure culture assignments reference known agents and remain one-to-one."""
        unknown_assignments = [
            (culture_name, agent_name)
            for culture_name, culture_config in self.cultures.items()
            for agent_name in culture_config.agents
            if agent_name not in self.agents
        ]
        if unknown_assignments:
            formatted = ", ".join(
                f"{culture_name} -> {agent_name}"
                for culture_name, agent_name in sorted(unknown_assignments, key=lambda item: (item[0], item[1]))
            )
            msg = f"Cultures reference unknown agents: {formatted}"
            raise ValueError(msg)

        agent_to_culture: dict[str, str] = {}
        duplicate_assignments: list[tuple[str, str, str]] = []
        for culture_name, culture_config in self.cultures.items():
            for agent_name in culture_config.agents:
                existing_culture = agent_to_culture.get(agent_name)
                if existing_culture is not None and existing_culture != culture_name:
                    duplicate_assignments.append((agent_name, existing_culture, culture_name))
                    continue
                agent_to_culture[agent_name] = culture_name

        if duplicate_assignments:
            formatted = ", ".join(
                f"{agent_name} -> {culture_a}, {culture_b}"
                for agent_name, culture_a, culture_b in sorted(
                    duplicate_assignments,
                    key=lambda item: (item[0], item[1], item[2]),
                )
            )
            msg = f"Agents cannot belong to multiple cultures: {formatted}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_internal_user_username_not_reserved(self) -> Config:
        """Ensure the internal user localpart does not collide with bot accounts."""
        reserved_localparts = {
            f"mindroom_{ROUTER_AGENT_NAME}": f"router '{ROUTER_AGENT_NAME}'",
            **{f"mindroom_{agent_name}": f"agent '{agent_name}'" for agent_name in self.agents},
            **{f"mindroom_{team_name}": f"team '{team_name}'" for team_name in self.teams},
        }
        conflict = reserved_localparts.get(self.mindroom_user.username)
        if conflict:
            msg = f"mindroom_user.username '{self.mindroom_user.username}' conflicts with {conflict} Matrix localpart"
            raise ValueError(msg)
        return self

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

    def get_mindroom_user_id(self) -> str:
        """Get the full Matrix user ID for the configured internal user."""
        from .matrix.identity import MatrixID  # noqa: PLC0415

        return MatrixID.from_username(self.mindroom_user.username, self.domain).full_id

    @classmethod
    def from_yaml(cls, config_path: Path | None = None) -> Config:
        """Create a Config instance from YAML data."""
        path = config_path or CONFIG_PATH

        if not path.exists():
            msg = f"Agent configuration file not found: {path}"
            raise FileNotFoundError(msg)

        with path.open() as f:
            data = yaml.safe_load(f) or {}

        # Handle None values for optional dictionaries
        if data.get("teams") is None:
            data["teams"] = {}
        if data.get("cultures") is None:
            data["cultures"] = {}
        if data.get("room_models") is None:
            data["room_models"] = {}
        if data.get("plugins") is None:
            data["plugins"] = []
        if data.get("knowledge_bases") is None:
            data["knowledge_bases"] = {}

        config = cls(**data)
        logger.info(f"Loaded agent configuration from {path}")
        logger.info(f"Found {len(config.agents)} agent configurations")
        return config

    def get_agent_culture(self, agent_name: str) -> tuple[str, CultureConfig] | None:
        """Get the configured culture assignment for an agent, if any."""
        for culture_name, culture_config in self.cultures.items():
            if agent_name in culture_config.agents:
                return culture_name, culture_config
        return None

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

    def get_agent_sandbox_tools(self, agent_name: str) -> list[str] | None:
        """Get the sandbox tool list for an agent, falling back to defaults.

        Returns:
            List of tool names to sandbox, or None to defer to env var globals.

        """
        agent_config = self.get_agent(agent_name)
        if agent_config.sandbox_tools is not None:
            return agent_config.sandbox_tools
        return self.defaults.sandbox_tools

    def get_agent_tools(self, agent_name: str) -> list[str]:
        """Get effective tools for an agent.

        Args:
            agent_name: Name of the agent.

        Returns:
            Ordered tool names with duplicates removed.

        Raises:
            ValueError: If agent not found.

        """
        agent_config = self.get_agent(agent_name)
        tool_names = list(agent_config.tools)
        if agent_config.include_default_tools:
            for default_tool_name in self.defaults.tools:
                if default_tool_name not in tool_names:
                    tool_names.append(default_tool_name)
        return tool_names

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
        from .matrix.identity import agent_username_localpart  # noqa: PLC0415
        from .matrix.rooms import resolve_room_aliases  # noqa: PLC0415

        configured_bots = set()

        # Check which agents should be in this room
        for agent_name, agent_config in self.agents.items():
            resolved_rooms = set(resolve_room_aliases(agent_config.rooms))
            if room_id in resolved_rooms:
                configured_bots.add(agent_username_localpart(agent_name))

        # Check which teams should be in this room
        for team_name, team_config in self.teams.items():
            resolved_rooms = set(resolve_room_aliases(team_config.rooms))
            if room_id in resolved_rooms:
                configured_bots.add(agent_username_localpart(team_name))

        # Router should be in any room that has any configured agents/teams
        if configured_bots:  # If any bots are configured for this room
            configured_bots.add(agent_username_localpart(ROUTER_AGENT_NAME))

        return configured_bots

    def save_to_yaml(self, config_path: Path | None = None) -> None:
        """Save the config to a YAML file, excluding None values.

        Args:
            config_path: Path to save the config to. If None, uses CONFIG_PATH.

        """
        path = config_path or CONFIG_PATH
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
        safe_replace(tmp_path, path_obj)
        logger.info(f"Saved configuration to {path}")
