"""Root configuration model and helpers."""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, cast

import yaml
from pydantic import BaseModel, Field, PrivateAttr, ValidationInfo, model_validator

from mindroom.config.agent import AgentConfig, CultureConfig, TeamConfig  # noqa: TC001
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.matrix import MatrixRoomAccessConfig, MatrixSpaceConfig, MindRoomUserConfig
from mindroom.config.memory import MemoryBackend, MemoryConfig
from mindroom.config.models import DefaultsConfig, ModelConfig, RouterConfig
from mindroom.config.voice import VoiceConfig
from mindroom.constants import (
    ROUTER_AGENT_NAME,
    RuntimePaths,
    resolve_runtime_paths,
    runtime_matrix_homeserver,
    safe_replace,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import (
    agent_username_localpart,
    managed_room_alias_localpart,
    managed_space_alias_localpart,
)
from mindroom.tool_system.worker_routing import WorkerScope  # noqa: TC001

if TYPE_CHECKING:
    from mindroom.matrix.identity import MatrixID

_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
_OPENCLAW_COMPAT_PRESET_TOOLS: tuple[str, ...] = (
    "shell",
    "coding",
    "duckduckgo",
    "website",
    "browser",
    "scheduler",
    "subagents",
    "matrix_message",
)
logger = get_logger(__name__)

_OPTIONAL_DICT_SECTION_NAMES = (
    "teams",
    "cultures",
    "room_models",
    "knowledge_bases",
    "matrix_room_access",
    "matrix_space",
)


def _resolve_agent_thread_mode(
    agent_config: AgentConfig,
    room_id: str | None,
    runtime_paths: RuntimePaths,
) -> Literal["thread", "room"]:
    """Resolve one agent's effective thread mode for an optional room context.

    Resolution order is:
    1. Explicit room ID key match in ``room_thread_modes``.
    2. Reverse alias lookup from room ID to managed room key.
    3. Any ``room_thread_modes`` key that resolves to the active room ID.
    4. Fallback to ``thread_mode``.
    """
    default_mode = agent_config.thread_mode
    if room_id is None or not agent_config.room_thread_modes:
        return default_mode

    overrides = agent_config.room_thread_modes

    # Fast path: direct room-id key.
    direct_mode = overrides.get(room_id)
    if direct_mode is not None:
        return direct_mode

    # Keep this import local to avoid config<->matrix import cycles during module initialization.
    from mindroom.matrix.rooms import get_room_alias_from_id, resolve_room_aliases  # noqa: PLC0415

    room_alias = get_room_alias_from_id(room_id, runtime_paths)
    if room_alias:
        alias_mode = overrides.get(room_alias)
        if alias_mode is not None:
            return alias_mode

    for override_key, resolved_room_id in zip(
        overrides,
        resolve_room_aliases(list(overrides), runtime_paths),
        strict=False,
    ):
        if resolved_room_id == room_id:
            return overrides[override_key]

    return default_mode


def _normalize_optional_config_sections(data: dict[str, object]) -> None:
    """Replace explicit YAML nulls with the model's expected empty containers."""
    for name in _OPTIONAL_DICT_SECTION_NAMES:
        if data.get(name) is None:
            data[name] = {}
    if data.get("plugins") is None:
        data["plugins"] = []


def _normalized_config_data(data: object) -> object:
    """Return config input with legacy optional sections normalized."""
    if not isinstance(data, dict):
        return data

    normalized_data = cast("dict[str, object]", data.copy())
    _normalize_optional_config_sections(normalized_data)
    return normalized_data


def _router_agents_for_room(
    agents: dict[str, AgentConfig],
    teams: dict[str, TeamConfig],
    room_id: str | None,
    runtime_paths: RuntimePaths,
) -> set[str]:
    """Return agents relevant for router mode resolution in one room context.

    Includes:
    - Agents directly configured for the room.
    - Agents brought into the room via ``teams.<name>.rooms`` mappings.

    Falls back to all agents when no room-specific subset is found.
    """
    if room_id is None:
        return set(agents)

    # Keep this import local to avoid config<->matrix import cycles during module initialization.
    from mindroom.matrix.rooms import resolve_room_aliases  # noqa: PLC0415

    router_agents: set[str] = set()
    for agent_name, agent_cfg in agents.items():
        if room_id in set(resolve_room_aliases(agent_cfg.rooms, runtime_paths)):
            router_agents.add(agent_name)
    for team_cfg in teams.values():
        if room_id not in set(resolve_room_aliases(team_cfg.rooms, runtime_paths)):
            continue
        router_agents.update(agent_name for agent_name in team_cfg.agents if agent_name in agents)
    return router_agents or set(agents)


class Config(BaseModel):
    """Complete configuration from YAML."""

    PRIVATE_KNOWLEDGE_BASE_ID_PREFIX: ClassVar[str] = "__agent_private__:"
    _runtime_paths: RuntimePaths | None = PrivateAttr(default=None)
    TOOL_PRESETS: ClassVar[dict[str, tuple[str, ...]]] = {
        "openclaw_compat": _OPENCLAW_COMPAT_PRESET_TOOLS,
    }
    IMPLIED_TOOLS: ClassVar[dict[str, tuple[str, ...]]] = {
        "matrix_message": ("attachments",),
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
    mindroom_user: MindRoomUserConfig | None = Field(
        default=None,
        description="Configuration for the internal MindRoom user account (omit for hosted/public profiles)",
    )
    matrix_room_access: MatrixRoomAccessConfig = Field(
        default_factory=MatrixRoomAccessConfig,
        description="Managed Matrix room access/discoverability behavior",
    )
    matrix_space: MatrixSpaceConfig = Field(
        default_factory=MatrixSpaceConfig,
        description="Optional root Matrix Space for grouping managed rooms",
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
        invalid_agents = [name for name in self.agents if not _AGENT_NAME_PATTERN.fullmatch(name)]
        invalid_teams = [name for name in self.teams if not _AGENT_NAME_PATTERN.fullmatch(name)]
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
    def validate_reserved_knowledge_base_ids(self) -> Config:
        """Reject top-level knowledge base IDs that collide with synthetic private IDs."""
        reserved_ids = sorted(
            base_id for base_id in self.knowledge_bases if base_id.startswith(self.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX)
        )
        if reserved_ids:
            formatted = ", ".join(reserved_ids)
            msg = (
                "knowledge_bases keys must not use the reserved private prefix "
                f"'{self.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX}'; invalid keys: {formatted}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_private_knowledge(self) -> Config:
        """Ensure enabled private knowledge declares an explicit path."""
        invalid_private_knowledge = [
            agent_name
            for agent_name, agent_config in self.agents.items()
            if (
                agent_config.private is not None
                and agent_config.private.knowledge is not None
                and agent_config.private.knowledge.enabled
                and agent_config.private.knowledge.path is None
            )
        ]
        if invalid_private_knowledge:
            formatted = ", ".join(sorted(invalid_private_knowledge))
            msg = f"agents.<name>.private.knowledge.path is required when private.knowledge is enabled; invalid agents: {formatted}"
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
    def validate_internal_user_username_not_reserved(self, info: ValidationInfo) -> Config:
        """Ensure the internal user localpart does not collide with bot accounts."""
        if self.mindroom_user is None:
            return self
        runtime_paths = info.context.get("runtime_paths") if isinstance(info.context, dict) else None
        if runtime_paths is None:
            return self
        reserved_localparts = {
            agent_username_localpart(ROUTER_AGENT_NAME, runtime_paths=runtime_paths): f"router '{ROUTER_AGENT_NAME}'",
            **{
                agent_username_localpart(agent_name, runtime_paths=runtime_paths): f"agent '{agent_name}'"
                for agent_name in self.agents
            },
            **{
                agent_username_localpart(team_name, runtime_paths=runtime_paths): f"team '{team_name}'"
                for team_name in self.teams
            },
        }
        conflict = reserved_localparts.get(self.mindroom_user.username)
        if conflict:
            msg = f"mindroom_user.username '{self.mindroom_user.username}' conflicts with {conflict} Matrix localpart"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_root_space_alias_does_not_collide_with_managed_rooms(self, info: ValidationInfo) -> Config:
        """Ensure no managed room key maps to the reserved root Space alias."""
        if not self.matrix_space.enabled:
            return self
        runtime_paths = info.context.get("runtime_paths") if isinstance(info.context, dict) else None
        if runtime_paths is None:
            return self
        reserved_alias_localpart = managed_space_alias_localpart(runtime_paths=runtime_paths)
        colliding_rooms = sorted(
            room_key
            for room_key in self.get_all_configured_rooms()
            if not room_key.startswith(("!", "#"))
            and managed_room_alias_localpart(room_key, runtime_paths=runtime_paths) == reserved_alias_localpart
        )
        if colliding_rooms:
            formatted = ", ".join(colliding_rooms)
            msg = (
                "Managed room keys conflict with the reserved root Space alias "
                f"'{reserved_alias_localpart}': {formatted}"
            )
            raise ValueError(msg)
        return self

    def get_domain(self, runtime_paths: RuntimePaths) -> str:
        """Extract the Matrix domain for one explicit runtime context."""
        from mindroom.matrix.identity import extract_server_name_from_homeserver  # noqa: PLC0415

        homeserver = runtime_matrix_homeserver(runtime_paths)
        return extract_server_name_from_homeserver(homeserver, runtime_paths)

    def get_ids(self, runtime_paths: RuntimePaths) -> dict[str, MatrixID]:
        """Get MatrixID objects for all agents and teams.

        Returns:
            Dictionary mapping agent/team names to their MatrixID objects.

        """
        from mindroom.matrix.identity import MatrixID  # noqa: PLC0415

        mapping: dict[str, MatrixID] = {}
        domain = self.get_domain(runtime_paths)

        # Add all agents
        for agent_name in self.agents:
            mapping[agent_name] = MatrixID.from_agent(agent_name, domain, runtime_paths)

        # Add router agent separately (it's not in config.agents)
        mapping[ROUTER_AGENT_NAME] = MatrixID.from_agent(ROUTER_AGENT_NAME, domain, runtime_paths)

        # Add all teams
        for team_name in self.teams:
            mapping[team_name] = MatrixID.from_agent(team_name, domain, runtime_paths)
        return mapping

    def get_mindroom_user_id(self, runtime_paths: RuntimePaths) -> str | None:
        """Get the full Matrix user ID for the configured internal user."""
        if self.mindroom_user is None:
            return None
        from mindroom.matrix.identity import MatrixID  # noqa: PLC0415

        return MatrixID.from_username(self.mindroom_user.username, self.get_domain(runtime_paths)).full_id

    @classmethod
    def validate_with_runtime(
        cls,
        data: object,
        runtime_paths: RuntimePaths,
    ) -> Config:
        """Validate config data against one explicit runtime context."""
        config = cls.model_validate(_normalized_config_data(data), context={"runtime_paths": runtime_paths})
        config._runtime_paths = runtime_paths
        return config

    @classmethod
    def from_yaml(
        cls,
        config_path: Path,
    ) -> Config:
        """Create a pure Config instance from one explicit YAML file path."""
        path = Path(config_path).expanduser().resolve()
        if not path.exists():
            msg = f"Agent configuration file not found: {path}"
            raise FileNotFoundError(msg)

        with path.open() as f:
            data = yaml.safe_load(f) or {}

        runtime_paths = resolve_runtime_paths(config_path=path)
        config = cls.validate_with_runtime(data, runtime_paths)
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

    def get_agent_worker_tools(
        self,
        agent_name: str,
        runtime_paths: RuntimePaths,
    ) -> list[str]:
        """Get effective worker-routed tools for an agent, including default policy resolution."""
        agent_config = self.get_agent(agent_name)
        configured = agent_config.worker_tools
        if configured is None:
            configured = self.defaults.worker_tools
        if configured is None:
            # Imported lazily to avoid a circular import: tool metadata also imports Config.
            from mindroom.tool_system.metadata import (  # noqa: PLC0415
                default_worker_routed_tools,
                ensure_tool_registry_loaded,
            )

            ensure_tool_registry_loaded(runtime_paths, self)
            return default_worker_routed_tools(self.get_agent_tools(agent_name))
        return self.expand_tool_names(list(configured))

    def get_agent_worker_scope(self, agent_name: str) -> WorkerScope | None:
        """Get the effective worker scope for an agent."""
        agent_config = self.get_agent(agent_name)
        if agent_config.private is not None:
            return agent_config.private.per
        if agent_config.worker_scope is not None:
            return agent_config.worker_scope
        return self.defaults.worker_scope

    def get_agent_private_knowledge_base_id(self, agent_name: str) -> str | None:
        """Return the synthetic knowledge base ID for one agent's private knowledge."""
        agent_config = self.get_agent(agent_name)
        if agent_config.private is None:
            return None
        private_knowledge = agent_config.private.knowledge
        if private_knowledge is None or not private_knowledge.enabled or private_knowledge.path is None:
            return None
        return f"{self.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX}{agent_name}"

    def get_private_knowledge_base_agent(self, base_id: str) -> str | None:
        """Return the owning agent for a synthetic private knowledge base ID."""
        if not base_id.startswith(self.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX):
            return None
        agent_name = base_id.removeprefix(self.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX)
        if agent_name not in self.agents:
            return None
        if self.get_agent_private_knowledge_base_id(agent_name) != base_id:
            return None
        return agent_name

    def get_agent_knowledge_base_ids(self, agent_name: str) -> list[str]:
        """Return shared and private knowledge base IDs assigned to one agent."""
        agent_config = self.get_agent(agent_name)
        base_ids = list(agent_config.knowledge_bases)
        private_base_id = self.get_agent_private_knowledge_base_id(agent_name)
        if private_base_id is not None:
            base_ids.append(private_base_id)
        return base_ids

    def get_knowledge_base_config(self, base_id: str) -> KnowledgeBaseConfig:
        """Return one effective knowledge base config, including synthetic private bases."""
        configured = self.knowledge_bases.get(base_id)
        if configured is not None:
            return configured

        agent_name = self.get_private_knowledge_base_agent(base_id)
        if agent_name is None:
            msg = f"Knowledge base '{base_id}' is not configured"
            raise ValueError(msg)

        agent_config = self.get_agent(agent_name)
        private_config = agent_config.private
        if private_config is None:
            msg = f"Knowledge base '{base_id}' is not configured"
            raise ValueError(msg)

        private_knowledge = private_config.knowledge
        if private_knowledge is None or not private_knowledge.enabled:
            msg = f"Knowledge base '{base_id}' is not configured"
            raise ValueError(msg)

        knowledge_path = private_knowledge.path
        if knowledge_path is None:
            msg = f"Knowledge base '{base_id}' is not configured"
            raise ValueError(msg)

        return KnowledgeBaseConfig(
            path=knowledge_path,
            watch=private_knowledge.watch,
            chunk_size=private_knowledge.chunk_size,
            chunk_overlap=private_knowledge.chunk_overlap,
            git=private_knowledge.git,
        )

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
        """Expand tool presets and implied tools, deduping while preserving order."""
        expanded: list[str] = []
        seen: set[str] = set()
        queue = deque(tool_names)
        while queue:
            tool_name = queue.popleft()
            if tool_name in seen:
                continue
            seen.add(tool_name)
            expanded.append(tool_name)
            next_tools = list(cls.get_tool_preset(tool_name) or ())
            next_tools.extend(cls.IMPLIED_TOOLS.get(tool_name, ()))
            queue.extend(implied_tool for implied_tool in next_tools if implied_tool not in seen)
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

    def get_entity_thread_mode(
        self,
        entity_name: str,
        runtime_paths: RuntimePaths,
        room_id: str | None = None,
    ) -> Literal["thread", "room"]:
        """Get effective thread mode for an agent, team, or router.

        Agents use their explicit per-agent setting.
        Teams inherit a mode only when all member agents share it.
        Router inherits a mode only when all relevant configured agents share it.
        In ambiguous cases, default to "thread".
        """
        if entity_name in self.agents:
            return _resolve_agent_thread_mode(
                self.agents[entity_name],
                room_id,
                runtime_paths,
            )

        if entity_name in self.teams:
            team_modes: set[Literal["thread", "room"]] = {
                _resolve_agent_thread_mode(
                    self.agents[name],
                    room_id,
                    runtime_paths,
                )
                for name in self.teams[entity_name].agents
                if name in self.agents
            }
            if len(team_modes) == 1:
                return next(iter(team_modes))

        if entity_name == ROUTER_AGENT_NAME:
            router_agents = _router_agents_for_room(
                self.agents,
                self.teams,
                room_id,
                runtime_paths,
            )
            configured_modes: set[Literal["thread", "room"]] = {
                _resolve_agent_thread_mode(
                    self.agents[agent_name],
                    room_id,
                    runtime_paths,
                )
                for agent_name in router_agents
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

    def get_configured_bots_for_room(
        self,
        room_id: str,
        runtime_paths: RuntimePaths,
    ) -> set[str]:
        """Get the set of bot usernames that should be in a specific room.

        Args:
            room_id: The Matrix room ID
            runtime_paths: Explicit runtime context for room resolution.

        Returns:
            Set of bot usernames (without domain) that should be in this room

        """
        from mindroom.matrix.identity import agent_username_localpart  # noqa: PLC0415
        from mindroom.matrix.rooms import resolve_room_aliases  # noqa: PLC0415

        configured_bots = set()

        # Check which agents should be in this room
        for agent_name, agent_config in self.agents.items():
            resolved_rooms = set(resolve_room_aliases(agent_config.rooms, runtime_paths))
            if room_id in resolved_rooms:
                configured_bots.add(agent_username_localpart(agent_name, runtime_paths))

        # Check which teams should be in this room
        for team_name, team_config in self.teams.items():
            resolved_rooms = set(resolve_room_aliases(team_config.rooms, runtime_paths))
            if room_id in resolved_rooms:
                configured_bots.add(agent_username_localpart(team_name, runtime_paths))

        # Router should be in any room that has any configured agents/teams
        if configured_bots:  # If any bots are configured for this room
            configured_bots.add(agent_username_localpart(ROUTER_AGENT_NAME, runtime_paths))

        return configured_bots

    def save_to_yaml(
        self,
        config_path: Path,
    ) -> None:
        """Save the config to a YAML file, excluding None values.

        Args:
            config_path: Path to save the config to.

        """
        config_dict = self.model_dump(exclude_none=True)
        path_obj = Path(config_path)
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
        logger.info(f"Saved configuration to {config_path}")


def load_config(runtime_paths: RuntimePaths) -> Config:
    """Load and validate one config against an explicit runtime context."""
    path = runtime_paths.config_path
    if not path.exists():
        msg = f"Agent configuration file not found: {path}"
        raise FileNotFoundError(msg)

    with path.open() as f:
        data = yaml.safe_load(f) or {}

    config = Config.validate_with_runtime(data, runtime_paths)
    logger.info(f"Loaded agent configuration from {path}")
    logger.info(f"Found {len(config.agents)} agent configurations")
    return config
