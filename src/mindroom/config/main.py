"""Root configuration model and helpers."""

from __future__ import annotations

import re
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from mindroom.constants import CONFIG_PATH, MATRIX_HOMESERVER, ROUTER_AGENT_NAME, safe_replace
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import agent_username_localpart

from .agent import AgentConfig, CultureConfig, TeamConfig  # noqa: TC001
from .auth import AuthorizationConfig
from .knowledge import KnowledgeBaseConfig  # noqa: TC001
from .matrix import MatrixRoomAccessConfig, MindRoomUserConfig
from .memory import MemoryBackend, MemoryConfig
from .models import DefaultsConfig, ModelConfig, RouterConfig
from .voice import VoiceConfig

if TYPE_CHECKING:
    from mindroom.matrix.identity import MatrixID

AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
OPENCLAW_COMPAT_PRESET_TOOLS: tuple[str, ...] = (
    "shell",
    "coding",
    "duckduckgo",
    "website",
    "browser",
    "scheduler",
)
logger = get_logger(__name__)


class Config(BaseModel):
    """Complete configuration from YAML."""

    TOOL_PRESETS: ClassVar[dict[str, tuple[str, ...]]] = {
        "openclaw_compat": OPENCLAW_COMPAT_PRESET_TOOLS,
    }

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
    matrix_room_access: MatrixRoomAccessConfig = Field(
        default_factory=MatrixRoomAccessConfig,
        description="Managed Matrix room access/discoverability behavior",
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
        overlapping_names = sorted(set(self.agents) & set(self.teams))
        if overlapping_names:
            msg = f"Agent and team names must be distinct, overlapping keys: {', '.join(overlapping_names)}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_agent_reply_permissions(self) -> Config:
        """Ensure per-agent reply permissions reference known entities."""
        known_entities = set(self.agents) | set(self.teams) | {ROUTER_AGENT_NAME}
        known_entities.add("*")
        unknown_entities = sorted(set(self.authorization.agent_reply_permissions) - known_entities)
        if unknown_entities:
            msg = f"authorization.agent_reply_permissions contains unknown entities: {', '.join(unknown_entities)}"
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
    def validate_memory_file_path_overrides(self) -> Config:
        """Ensure memory_file_path is only configured for effective file-backed agents."""
        invalid_overrides = [
            agent_name
            for agent_name, agent_config in self.agents.items()
            if agent_config.memory_file_path is not None and self.get_agent_memory_backend(agent_name) != "file"
        ]
        if invalid_overrides:
            formatted = ", ".join(sorted(invalid_overrides))
            msg = f"agents.<name>.memory_file_path requires effective file memory backend; invalid agents: {formatted}"
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
            agent_username_localpart(ROUTER_AGENT_NAME): f"router '{ROUTER_AGENT_NAME}'",
            **{agent_username_localpart(agent_name): f"agent '{agent_name}'" for agent_name in self.agents},
            **{agent_username_localpart(team_name): f"team '{team_name}'" for team_name in self.teams},
        }
        conflict = reserved_localparts.get(self.mindroom_user.username)
        if conflict:
            msg = f"mindroom_user.username '{self.mindroom_user.username}' conflicts with {conflict} Matrix localpart"
            raise ValueError(msg)
        return self

    @cached_property
    def domain(self) -> str:
        """Extract the domain from the MATRIX_HOMESERVER."""
        from mindroom.matrix.identity import extract_server_name_from_homeserver  # noqa: PLC0415

        return extract_server_name_from_homeserver(MATRIX_HOMESERVER)

    @cached_property
    def ids(self) -> dict[str, MatrixID]:
        """Get MatrixID objects for all agents and teams.

        Returns:
            Dictionary mapping agent/team names to their MatrixID objects.

        """
        from mindroom.matrix.identity import MatrixID  # noqa: PLC0415

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
        from mindroom.matrix.identity import MatrixID  # noqa: PLC0415

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
        if data.get("matrix_room_access") is None:
            data["matrix_room_access"] = {}

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
            tool_names.extend(self.defaults.tools)
        return self.expand_tool_names(tool_names)

    @classmethod
    def get_tool_preset(cls, tool_name: str) -> tuple[str, ...] | None:
        """Return the tool expansion for a preset name."""
        return cls.TOOL_PRESETS.get(tool_name)

    @classmethod
    def is_tool_preset(cls, tool_name: str) -> bool:
        """Return whether a tool name is a known config preset."""
        return tool_name in cls.TOOL_PRESETS

    @classmethod
    def expand_tool_names(cls, tool_names: list[str]) -> list[str]:
        """Expand configured tool presets and dedupe while preserving order."""
        expanded: list[str] = []
        seen: set[str] = set()
        for tool_name in tool_names:
            entries = cls.get_tool_preset(tool_name) or (tool_name,)
            for entry in entries:
                if entry in seen:
                    continue
                seen.add(entry)
                expanded.append(entry)
        return expanded

    def get_agent_memory_backend(self, agent_name: str) -> MemoryBackend:
        """Get effective memory backend for one agent."""
        agent_config = self.agents.get(agent_name)
        if agent_config is None:
            return self.memory.backend
        if agent_config.memory_backend is not None:
            return agent_config.memory_backend
        return self.memory.backend

    def uses_file_memory(self) -> bool:
        """Return whether any configured agent uses file-backed memory."""
        if not self.agents:
            return self.memory.backend == "file"
        return any(self.get_agent_memory_backend(agent_name) == "file" for agent_name in self.agents)

    def uses_mem0_memory(self) -> bool:
        """Return whether any configured agent uses Mem0-backed memory."""
        if not self.agents:
            return self.memory.backend == "mem0"
        return any(self.get_agent_memory_backend(agent_name) == "mem0" for agent_name in self.agents)

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

    def get_entity_thread_mode(self, entity_name: str) -> Literal["thread", "room"]:
        """Get effective thread mode for an agent, team, or router.

        Agents use their explicit per-agent setting.
        Teams inherit a mode only when all member agents share it.
        Router inherits a mode only when all configured agents share it.
        In ambiguous cases, default to "thread".
        """
        if entity_name in self.agents:
            return self.agents[entity_name].thread_mode

        if entity_name in self.teams:
            team_modes: set[Literal["thread", "room"]] = {
                self.agents[name].thread_mode for name in self.teams[entity_name].agents if name in self.agents
            }
            if len(team_modes) == 1:
                return next(iter(team_modes))

        if entity_name == ROUTER_AGENT_NAME:
            configured_modes: set[Literal["thread", "room"]] = {
                agent_cfg.thread_mode for agent_cfg in self.agents.values()
            }
            if len(configured_modes) == 1:
                return next(iter(configured_modes))

        return "thread"

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
        from mindroom.matrix.identity import agent_username_localpart  # noqa: PLC0415
        from mindroom.matrix.rooms import resolve_room_aliases  # noqa: PLC0415

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
