"""Root configuration model and helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from mindroom.agent_policy import (
    build_agent_policy_seeds,
    get_unsupported_team_agents,
    unsupported_team_agent_message,
)
from mindroom.config.agent import AgentConfig, CultureConfig, RoomConfig, TeamConfig  # noqa: TC001
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.errors import ConfigRuntimeValidationError
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig
from mindroom.config.knowledge import KnowledgeBaseConfig  # noqa: TC001
from mindroom.config.matrix import (
    CacheConfig,
    MatrixRoomAccessConfig,
    MatrixSpaceConfig,
    MindRoomUserConfig,
)
from mindroom.config.memory import MemoryConfig
from mindroom.config.models import DebugConfig, DefaultsConfig, ModelConfig, RouterConfig, ToolConfigEntry
from mindroom.config.plugin import PluginEntryConfig  # noqa: TC001
from mindroom.config.tool_entries import raw_tool_entry_name_and_lazy_flag_fields, raw_tools_entries
from mindroom.config.validation import relative_paths_overlap
from mindroom.config.voice import VoiceConfig
from mindroom.config.yaml_includes import ConfigIncludeError, attach_partial_source_files, load_yaml_config_source
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.logging_config import get_logger
from mindroom.mcp.config import MCPServerConfig, normalize_mcp_server_id
from mindroom.prompt_templates import render_prompt_template, validate_prompt_template_fields
from mindroom.prompts import PROMPT_DEFAULT_NAMES, PROMPT_DEFAULTS

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.entity_view import ResolvedEntityView, ResolvedRuntimeModel

_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]+$")
_RESERVED_ENTITY_NAMES = frozenset({ROUTER_AGENT_NAME, "user"})
_DEFER_PROHIBITED_CONTROL_TOOLS = frozenset({"delegate", "dynamic_tools", "external_trigger_manager", "self_config"})
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

_RuntimeToolOverrides = tuple[tuple[str, tuple[tuple[str, object], ...]], ...]


_OPTIONAL_DICT_SECTION_NAMES = (
    "teams",
    "cultures",
    "rooms",
    "room_models",
    "room_thread_summary_models",
    "knowledge_bases",
    "mcp_servers",
    "prompts",
    "matrix_room_access",
    "matrix_space",
)
_OPTIONAL_MODEL_SECTION_NAMES = ("debug", "external_trigger_policy", "tool_approval")


CONFIG_LOAD_USER_ERROR_TYPES = (
    ValidationError,
    ConfigRuntimeValidationError,
    yaml.YAMLError,
    OSError,
    UnicodeError,
)


def iter_config_validation_messages(
    exc: ValidationError | ConfigRuntimeValidationError | yaml.YAMLError | OSError | UnicodeError,
) -> list[tuple[str, str]]:
    """Return user-facing validation messages from one config validation exception."""
    if isinstance(exc, ValidationError):
        return [(" → ".join(str(x) for x in error["loc"]), error["msg"]) for error in exc.errors(include_context=False)]
    if isinstance(exc, ConfigRuntimeValidationError):
        return [("config", str(exc))]
    if isinstance(exc, ConfigIncludeError):
        return [("config", str(exc))]
    if isinstance(exc, yaml.YAMLError):
        return [("config", f"Could not parse configuration YAML: {exc}")]
    if isinstance(exc, UnicodeError):
        return [("config", f"Could not read configuration text: {exc}")]
    return [("config", f"Could not load configuration: {exc}")]


def format_invalid_config_message(
    exc: ValidationError | ConfigRuntimeValidationError | yaml.YAMLError | OSError | UnicodeError,
    *,
    footer: str | None = None,
) -> str:
    """Return one shared invalid-configuration message for user-facing surfaces."""
    errors = [f"• {location}: {message}" for location, message in iter_config_validation_messages(exc)]
    response = f"❌ Invalid configuration:\n{'\n'.join(errors)}"
    if footer:
        response = f"{response}\n\n{footer}"
    return response


@dataclass(frozen=True)
class _AuthoredOptionalModel:
    """Static authored semantics for an optional model override field."""

    kind: Literal["unset", "clear", "value"]
    value: str | None = None


@dataclass(frozen=True)
class _StaticCompactionConfigSemantics:
    """Static compaction semantics for one config scope."""

    scope_label: str
    authored_model: _AuthoredOptionalModel


def _normalize_optional_config_sections(data: dict[str, object]) -> None:
    """Replace explicit YAML nulls with the model's expected empty containers."""
    for name in _OPTIONAL_DICT_SECTION_NAMES:
        if data.get(name) is None:
            data[name] = {}
    for name in _OPTIONAL_MODEL_SECTION_NAMES:
        if data.get(name) is None:
            data[name] = {}
    if data.get("plugins") is None:
        data["plugins"] = []


def normalized_config_data(data: object) -> object:
    """Return config input with legacy optional sections normalized."""
    if not isinstance(data, dict):
        return data

    normalized_data = cast("dict[str, object]", data.copy())
    _normalize_optional_config_sections(normalized_data)
    return normalized_data


def _authored_optional_model(model_name: str | None, *, field_is_set: bool) -> _AuthoredOptionalModel:
    """Return the authored tri-state semantics for one optional model field."""
    if not field_is_set:
        return _AuthoredOptionalModel(kind="unset")
    if model_name is None:
        return _AuthoredOptionalModel(kind="clear")
    return _AuthoredOptionalModel(kind="value", value=model_name)


def _strip_empty_root_sections(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop normalized empty root sections from authored config serialization."""
    authored_payload = dict(payload)
    for name in _OPTIONAL_DICT_SECTION_NAMES:
        if authored_payload.get(name) == {}:
            authored_payload.pop(name, None)
    for name in _OPTIONAL_MODEL_SECTION_NAMES:
        if authored_payload.get(name) == {}:
            authored_payload.pop(name, None)
    if authored_payload.get("plugins") == []:
        authored_payload.pop("plugins", None)
    return authored_payload


def _tool_entry_has_lazy_flag_field(entry: ToolConfigEntry) -> bool:
    """Return whether one normalized tool entry authored a lazy-loading field."""
    return bool(entry.model_fields_set & {"defer", "initial"})


def _assert_team_agents_supported(
    config: Config,
    agent_names: list[str],
    *,
    team_name: str | None = None,
    allow_direct_private_agents: bool = False,
) -> None:
    """Reject unknown or statically unsupported team members."""
    prefix = f"Team '{team_name}'" if team_name is not None else "Team request"
    unsupported_agents = get_unsupported_team_agents(
        agent_names,
        build_agent_policy_seeds(
            config.agents,
            default_worker_scope=config.defaults.worker_scope,
        ),
        closures={},
        allow_direct_private_agents=allow_direct_private_agents,
    )
    if not unsupported_agents:
        return
    first_unsupported_agent, private_targets = next(iter(unsupported_agents.items()))
    raise ValueError(
        unsupported_team_agent_message(
            first_unsupported_agent,
            prefix=prefix,
            private_targets=private_targets,
        ),
    )


class Config(BaseModel):
    """Authored configuration parsed from YAML without runtime registries."""

    model_config = ConfigDict(extra="forbid")

    PRIVATE_KNOWLEDGE_BASE_ID_PREFIX: ClassVar[str] = "__agent_private__:"
    TOOL_PRESETS: ClassVar[dict[str, tuple[str, ...]]] = {
        "openclaw_compat": _OPENCLAW_COMPAT_PRESET_TOOLS,
    }
    IMPLIED_TOOLS: ClassVar[dict[str, tuple[str, ...]]] = {
        "matrix_message": ("attachments", "matrix_room"),
    }

    agents: dict[str, AgentConfig] = Field(default_factory=dict, description="Agent configurations")
    teams: dict[str, TeamConfig] = Field(default_factory=dict, description="Team configurations")
    cultures: dict[str, CultureConfig] = Field(default_factory=dict, description="Culture configurations")
    rooms: dict[str, RoomConfig] = Field(default_factory=dict, description="Managed Matrix room metadata")
    room_models: dict[str, str] = Field(default_factory=dict, description="Room-specific model overrides")
    room_thread_summary_models: dict[str, str] = Field(
        default_factory=dict,
        description="Room-specific model overrides for automatic thread summaries",
    )
    plugins: list[PluginEntryConfig] = Field(default_factory=list, description="Plugin entries")
    debug: DebugConfig = Field(default_factory=DebugConfig, description="Debug and diagnostic settings")
    prompts: dict[str, str] = Field(
        default_factory=dict,
        description="Built-in prompt overrides keyed by the uppercase global name from mindroom.prompts",
    )
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig, description="Default values")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory configuration")
    knowledge_bases: dict[str, KnowledgeBaseConfig] = Field(
        default_factory=dict,
        description="Knowledge base configurations keyed by base ID",
    )
    mcp_servers: dict[str, MCPServerConfig] = Field(
        default_factory=dict,
        description="MCP server configurations keyed by server id",
    )
    external_trigger_policy: ExternalTriggerPolicyConfig = Field(
        default_factory=ExternalTriggerPolicyConfig,
        description="Global policy for tool-managed signed external triggers",
    )
    models: dict[str, ModelConfig] = Field(default_factory=dict, description="Model configurations")
    tool_approval: ToolApprovalConfig = Field(
        default_factory=ToolApprovalConfig,
        description="Tool-approval rules for agent-initiated tool calls",
    )
    router: RouterConfig = Field(default_factory=RouterConfig, description="Router configuration")
    voice: VoiceConfig = Field(default_factory=VoiceConfig, description="Voice configuration")
    cache: CacheConfig = Field(default_factory=CacheConfig, description="Persistent Matrix event cache")
    timezone: str = Field(
        default="UTC",
        description="Timezone for interpreting scheduling requests and displaying scheduled tasks (e.g., 'America/New_York')",
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

    @classmethod
    def _lazy_flag_prohibited_message(cls, *, tool_name: str, config_path: str) -> str | None:
        if tool_name in cls.TOOL_PRESETS:
            return (
                f"{config_path}: '{tool_name}' is a preset and cannot be deferred; "
                "defer/initial are only valid on individual tools."
            )
        if tool_name in _DEFER_PROHIBITED_CONTROL_TOOLS:
            return (
                f"{config_path}: '{tool_name}' is a control-plane tool and cannot be deferred; "
                "defer/initial are only valid on runtime tools."
            )
        return None

    @classmethod
    def _validate_raw_tool_lazy_flag_boundary(cls, entry: object, *, config_path: str) -> None:
        name, defer, initial = raw_tool_entry_name_and_lazy_flag_fields(entry)
        if name is None or not (defer or initial):
            return
        if msg := cls._lazy_flag_prohibited_message(tool_name=name, config_path=config_path):
            raise ValueError(msg)

    @model_validator(mode="before")
    @classmethod
    def validate_raw_root_config(cls, data: object) -> object:
        """Normalize optional root sections and reject preset lazy flags before nested validation."""
        normalized = normalized_config_data(data)
        if not isinstance(normalized, dict):
            return normalized

        raw_data = cast("dict[object, object]", normalized)
        for entry in raw_tools_entries(raw_data, "defaults"):
            cls._validate_raw_tool_lazy_flag_boundary(entry, config_path="defaults.tools")

        raw_agents = raw_data.get("agents")
        if not isinstance(raw_agents, dict):
            return normalized

        for agent_name, raw_agent in raw_agents.items():
            if not isinstance(agent_name, str) or not isinstance(raw_agent, dict):
                continue
            agent_data = cast("dict[object, object]", raw_agent)
            tools = agent_data.get("tools")
            if not isinstance(tools, list):
                continue
            for entry in tools:
                cls._validate_raw_tool_lazy_flag_boundary(entry, config_path=f"agents.{agent_name}.tools")

        return normalized

    @model_validator(mode="after")
    def validate_tool_presets_do_not_use_lazy_flags(self) -> Config:
        """Reject lazy-loading control fields on presets after tool-entry coercion."""
        for entry in self.defaults.tools:
            if _tool_entry_has_lazy_flag_field(entry) and (
                msg := self._lazy_flag_prohibited_message(tool_name=entry.name, config_path="defaults.tools")
            ):
                raise ValueError(msg)

        for agent_name, agent_config in self.agents.items():
            for entry in agent_config.tools:
                if _tool_entry_has_lazy_flag_field(entry) and (
                    msg := self._lazy_flag_prohibited_message(
                        tool_name=entry.name,
                        config_path=f"agents.{agent_name}.tools",
                    )
                ):
                    raise ValueError(msg)

        return self

    @field_validator("plugins", mode="before")
    @classmethod
    def normalize_plugins(cls, value: object) -> object:
        """Normalize legacy string plugin entries into structured config objects."""
        if value is None:
            return []
        if not isinstance(value, list):
            return value

        normalized_plugins: list[object] = []
        for plugin_entry in value:
            if isinstance(plugin_entry, str):
                normalized_plugins.append({"path": plugin_entry})
                continue
            normalized_plugins.append(plugin_entry)
        return normalized_plugins

    @field_validator("prompts")
    @classmethod
    def validate_prompt_overrides(cls, value: dict[str, str]) -> dict[str, str]:
        """Ensure prompt overrides map to known built-in string prompt globals."""
        unknown_names = sorted(set(value) - PROMPT_DEFAULT_NAMES)
        if unknown_names:
            allowed = ", ".join(sorted(PROMPT_DEFAULT_NAMES))
            unknown = ", ".join(unknown_names)
            msg = f"Unknown prompt override(s): {unknown}. Allowed prompt names: {allowed}"
            raise ValueError(msg)
        for prompt_name, prompt_text in value.items():
            validate_prompt_template_fields(prompt_name, prompt_text)
        return value

    def get_prompt(self, name: str) -> str:
        """Return one configured prompt override or the built-in default."""
        if name in self.prompts:
            return self.prompts[name]
        return PROMPT_DEFAULTS[name]

    def render_prompt(self, name: str, **kwargs: object) -> str:
        """Render one configured prompt with MindRoom's small bare-field template syntax."""
        return render_prompt_template(self.get_prompt(name), **kwargs)

    @model_validator(mode="after")
    def validate_entity_names(self) -> Config:
        """Ensure agent and team names contain only alphanumeric characters and underscores."""
        invalid_agents = [name for name in self.agents if not _AGENT_NAME_PATTERN.fullmatch(name)]
        invalid_teams = [name for name in self.teams if not _AGENT_NAME_PATTERN.fullmatch(name)]
        invalid_mcp_servers = [name for name in self.mcp_servers if not _AGENT_NAME_PATTERN.fullmatch(name)]
        invalid = sorted(invalid_agents + invalid_teams + invalid_mcp_servers)
        if invalid:
            msg = f"Agent, team, and MCP server names must be alphanumeric/underscore only, got: {', '.join(invalid)}"
            raise ValueError(msg)
        overlapping_names = sorted(set(self.agents) & set(self.teams))
        if overlapping_names:
            msg = f"Agent and team names must be distinct, overlapping keys: {', '.join(overlapping_names)}"
            raise ValueError(msg)
        reserved_entity_names = sorted((set(self.agents) | set(self.teams)) & _RESERVED_ENTITY_NAMES)
        if reserved_entity_names:
            msg = (
                f"Agent and team names must not use reserved internal entity names: {', '.join(reserved_entity_names)}"
            )
            raise ValueError(msg)
        for server_id in self.mcp_servers:
            normalize_mcp_server_id(server_id)
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
    def validate_team_agents(self) -> Config:
        """Ensure team members exist and do not use private requester-local state."""
        for team_name, team_config in self.teams.items():
            _assert_team_agents_supported(self, team_config.agents, team_name=team_name)
        return self

    def _invalid_compaction_model_references(self) -> list[str]:
        """Return any compaction.model references that point at unknown models."""
        invalid_references: list[str] = []
        for semantics in self._static_compaction_semantics():
            if semantics.authored_model.kind != "value":
                continue
            assert semantics.authored_model.value is not None
            if semantics.authored_model.value not in self.models:
                invalid_references.append(
                    f"{semantics.scope_label}.compaction.model -> {semantics.authored_model.value}",
                )

        return invalid_references

    def _compaction_models_missing_context_window(self) -> list[str]:
        """Return explicit compaction.model references whose target model lacks context_window."""
        invalid_references: list[str] = []
        for semantics in self._static_compaction_semantics():
            if semantics.authored_model.kind != "value":
                continue
            assert semantics.authored_model.value is not None
            if self.models[semantics.authored_model.value].context_window is None:
                invalid_references.append(
                    f"{semantics.scope_label}.compaction.model -> {semantics.authored_model.value}",
                )

        return invalid_references

    def _static_compaction_semantics(self) -> list[_StaticCompactionConfigSemantics]:
        """Return static compaction semantics for defaults, agents, and teams."""
        semantics: list[_StaticCompactionConfigSemantics] = []
        defaults_compaction = self.defaults.compaction

        if defaults_compaction is not None:
            authored_model = _authored_optional_model(
                defaults_compaction.model,
                field_is_set="model" in defaults_compaction.model_fields_set,
            )
            semantics.append(
                _StaticCompactionConfigSemantics(
                    scope_label="defaults",
                    authored_model=authored_model,
                ),
            )

        for agent_name, agent_config in self.agents.items():
            override = agent_config.compaction
            if override is None:
                continue
            authored_model = _authored_optional_model(
                override.model,
                field_is_set="model" in override.model_fields_set,
            )
            semantics.append(
                _StaticCompactionConfigSemantics(
                    scope_label=f"agents.{agent_name}",
                    authored_model=authored_model,
                ),
            )

        for team_name, team_config in self.teams.items():
            override = team_config.compaction
            if override is None:
                continue
            authored_model = _authored_optional_model(
                override.model,
                field_is_set="model" in override.model_fields_set,
            )
            semantics.append(
                _StaticCompactionConfigSemantics(
                    scope_label=f"teams.{team_name}",
                    authored_model=authored_model,
                ),
            )

        return semantics

    @model_validator(mode="after")
    def validate_compaction_model_references(self) -> Config:
        """Ensure explicit compaction.model references are statically valid."""
        invalid_references = self._invalid_compaction_model_references()
        if invalid_references:
            msg = "Compaction model references unknown models: " + ", ".join(sorted(invalid_references))
            raise ValueError(msg)

        missing_context_windows = self._compaction_models_missing_context_window()
        if missing_context_windows:
            msg = "Explicit compaction.model requires a model with context_window: " + ", ".join(
                sorted(missing_context_windows),
            )
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
    def validate_knowledge_base_ids_do_not_use_line_breaks(self) -> Config:
        """Reject knowledge base IDs that would create multi-line source-list labels."""
        invalid_ids = sorted(base_id for base_id in self.knowledge_bases if "\n" in base_id or "\r" in base_id)
        if invalid_ids:
            formatted = ", ".join(invalid_ids)
            msg = f"knowledge_bases keys must not contain line breaks; invalid keys: {formatted}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_knowledge_base_ids_are_path_safe(self) -> Config:
        """Reject knowledge base IDs that would create nested or overlapping alias paths."""
        invalid_ids = sorted(
            base_id
            for base_id in self.knowledge_bases
            if not base_id or base_id in {".", ".."} or "/" in base_id or "\\" in base_id
        )
        if invalid_ids:
            formatted = ", ".join(invalid_ids)
            msg = (
                "knowledge_bases keys must be non-empty single path components without path separators "
                f"or dot segments; invalid keys: {formatted}"
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
    def validate_private_git_knowledge_paths(self) -> Config:
        """Ensure git-backed private knowledge uses a dedicated subtree."""
        memory_notes_dir = Path("memory")
        memory_notes_entrypoint = Path("MEMORY.md")
        for agent_name, agent_config in self.agents.items():
            private_config = agent_config.private
            if private_config is None or private_config.knowledge is None:
                continue
            private_knowledge = private_config.knowledge
            if private_knowledge.git is None or private_knowledge.path is None:
                continue
            knowledge_path = Path(private_knowledge.path)
            if knowledge_path == Path():
                msg = (
                    f"Agent '{agent_name}' uses git-backed private knowledge at '{private_knowledge.path}', "
                    "but git-backed private knowledge must use a dedicated subtree outside the private root "
                    "and outside scaffolded private workspace content"
                )
                raise ValueError(msg)
            memory_backend = agent_config.memory_backend or self.memory.backend
            uses_file_memory_backend = memory_backend == "file"
            overlaps_private_file_memory = uses_file_memory_backend and relative_paths_overlap(
                knowledge_path,
                memory_notes_dir,
            )
            if uses_file_memory_backend and relative_paths_overlap(
                knowledge_path,
                memory_notes_entrypoint,
            ):
                overlaps_private_file_memory = True
            overlaps_template_scaffold = False
            if private_config.template_dir is not None and relative_paths_overlap(
                knowledge_path,
                memory_notes_dir,
            ):
                overlaps_template_scaffold = True
            if overlaps_private_file_memory or overlaps_template_scaffold:
                msg = (
                    f"Agent '{agent_name}' uses git-backed private knowledge at '{private_knowledge.path}', "
                    "but git-backed private knowledge must use a dedicated subtree outside the private root "
                    "and outside scaffolded private workspace content"
                )
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

    def authored_model_dump(self) -> dict[str, Any]:
        """Serialize authored config."""
        payload = cast("dict[str, Any]", self.model_dump(exclude_unset=True))
        return _strip_empty_root_sections(payload)

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
        """Extract all configured room references.

        Returns:
            Set of all unique room references from room, agent, and team configurations

        """
        all_room_aliases = set(self.rooms)
        for agent_config in self.agents.values():
            all_room_aliases.update(agent_config.rooms)
        for team_config in self.teams.values():
            all_room_aliases.update(team_config.rooms)
        return all_room_aliases


class RuntimeConfig(Config):
    """Frozen authored config bound to validated runtime paths and tool state."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    runtime_paths: RuntimePaths = Field(exclude=True, repr=False)
    source_files: frozenset[Path] = Field(default_factory=lambda: frozenset[Path](), exclude=True)
    unavailable_plugin_tool_names: frozenset[str] = Field(default_factory=frozenset, exclude=True, repr=False)
    agent_tool_runtime_overrides: tuple[tuple[str, _RuntimeToolOverrides], ...] = Field(
        default_factory=tuple,
        exclude=True,
        repr=False,
    )
    runtime_approved_egress_injected_default_tool: bool = Field(default=False, exclude=True, repr=False)
    runtime_approved_egress_injected_approval_rule: bool = Field(default=False, exclude=True, repr=False)

    @classmethod
    def from_authored(
        cls,
        authored: Config,
        runtime_paths: RuntimePaths,
        *,
        tolerate_plugin_load_errors: bool = False,
        source_files: frozenset[Path] = frozenset(),
        tool_validation_snapshot: Mapping[str, Any] | None = None,
    ) -> RuntimeConfig:
        """Run the single runtime validation and resolution stage."""
        from mindroom.config.runtime import build_runtime_config  # noqa: PLC0415

        return build_runtime_config(
            authored,
            runtime_paths,
            runtime_config_type=cls,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
            source_files=source_files,
            tool_validation_snapshot=tool_validation_snapshot,
        )

    def authored_model_dump(self) -> dict[str, Any]:
        """Serialize only authored values, excluding runtime overlays and metadata."""
        from mindroom.config.runtime import strip_runtime_overlay_from_dump  # noqa: PLC0415

        return _strip_empty_root_sections(strip_runtime_overlay_from_dump(self, super().authored_model_dump()))

    def get_domain(self) -> str:
        """Return the Matrix domain bound to this runtime config."""
        from mindroom.config.runtime import get_domain  # noqa: PLC0415

        return get_domain(self)

    def resolve_entity(self, entity_name: str | None) -> ResolvedEntityView:
        """Return a materialized frozen snapshot for one validated entity name."""
        from mindroom.config.runtime import resolve_entity  # noqa: PLC0415

        return resolve_entity(self, entity_name)

    def get_model_context_window(self, model_name: str) -> int | None:
        """Return the configured context window for one model name, when known."""
        from mindroom.config.runtime import get_model_context_window  # noqa: PLC0415

        return get_model_context_window(self, model_name)

    def get_worker_grantable_credentials(self) -> frozenset[str]:
        """Return credential services allowed inside isolated workers."""
        from mindroom.config.runtime import get_worker_grantable_credentials  # noqa: PLC0415

        return get_worker_grantable_credentials(self)

    def get_private_knowledge_base_agent(self, base_id: str) -> str | None:
        """Return the owning agent for a synthetic private knowledge base ID."""
        from mindroom.config.runtime import get_private_knowledge_base_agent  # noqa: PLC0415

        return get_private_knowledge_base_agent(self, base_id)

    def get_knowledge_base_config(self, base_id: str) -> KnowledgeBaseConfig:
        """Return one effective shared or synthetic private knowledge base config."""
        from mindroom.config.runtime import get_knowledge_base_config  # noqa: PLC0415

        return get_knowledge_base_config(self, base_id)

    def get_entities_referencing_tools(self, tool_names: set[str]) -> set[str]:
        """Return agents and teams that depend on any of the given tools."""
        from mindroom.config.runtime import get_entities_referencing_tools  # noqa: PLC0415

        return get_entities_referencing_tools(self, tool_names)

    def expand_tool_names(self, tool_names: list[str]) -> list[str]:
        """Expand authored presets and implied tools in runtime order."""
        from mindroom.config.runtime import expand_tool_names  # noqa: PLC0415

        return expand_tool_names(self, tool_names)

    def is_tool_preset(self, tool_name: str) -> bool:
        """Return whether a tool name is an authored preset."""
        return tool_name in self.TOOL_PRESETS

    def get_agent_delegation_closure(
        self,
        agent_name: str,
        *,
        closures: dict[str, frozenset[str]] | None = None,
    ) -> frozenset[str]:
        """Return one agent plus all transitively delegated agents."""
        from mindroom.config.runtime import get_agent_delegation_closure  # noqa: PLC0415

        return get_agent_delegation_closure(self, agent_name, closures=closures)

    def get_unsupported_team_agents(
        self,
        agent_names: list[str],
        *,
        closures: dict[str, frozenset[str]] | None = None,
        allow_direct_private_agents: bool = False,
    ) -> dict[str, tuple[str, ...] | None]:
        """Return unsupported team members keyed by agent name."""
        from mindroom.config.runtime import get_unsupported_team_agents  # noqa: PLC0415

        return get_unsupported_team_agents(
            self,
            agent_names,
            closures=closures,
            allow_direct_private_agents=allow_direct_private_agents,
        )

    @staticmethod
    def unsupported_team_agent_message(
        agent_name: str,
        *,
        prefix: str,
        private_targets: tuple[str, ...] | None,
    ) -> str:
        """Return the user-facing error for one unsupported team member."""
        return unsupported_team_agent_message(
            agent_name,
            prefix=prefix,
            private_targets=private_targets,
        )

    def assert_team_agents_supported(
        self,
        agent_names: list[str],
        *,
        team_name: str | None = None,
        allow_direct_private_agents: bool = False,
    ) -> None:
        """Reject unknown or currently unsupported team members."""
        from mindroom.config.runtime import assert_team_agents_supported  # noqa: PLC0415

        assert_team_agents_supported(
            self,
            agent_names,
            team_name=team_name,
            allow_direct_private_agents=allow_direct_private_agents,
        )

    def uses_file_memory(self) -> bool:
        """Return whether any configured agent uses file-backed memory."""
        from mindroom.config.runtime import uses_file_memory  # noqa: PLC0415

        return uses_file_memory(self)

    def get_entity_thread_mode(
        self,
        entity_name: str,
        room_id: str | None = None,
    ) -> Literal["thread", "room"]:
        """Return the effective thread mode in this runtime context."""
        from mindroom.config.runtime import get_entity_thread_mode  # noqa: PLC0415

        return get_entity_thread_mode(self, entity_name, room_id)

    def resolve_runtime_model(
        self,
        *,
        entity_name: str | None,
        active_model_name: str | None = None,
        active_context_window: int | None = None,
        room_id: str | None = None,
        thread_id: str | None = None,
        default_model_name: str = "default",
    ) -> ResolvedRuntimeModel:
        """Resolve the active model and context window in this runtime context."""
        from mindroom.config.runtime import resolve_runtime_model  # noqa: PLC0415

        return resolve_runtime_model(
            self,
            entity_name=entity_name,
            active_model_name=active_model_name,
            active_context_window=active_context_window,
            room_id=room_id,
            thread_id=thread_id,
            default_model_name=default_model_name,
        )


def load_config(
    runtime_paths: RuntimePaths,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> RuntimeConfig:
    """Load and validate one config against an explicit runtime context."""
    path = runtime_paths.config_path
    if not path.exists():
        msg = f"Agent configuration file not found: {path}"
        raise FileNotFoundError(msg)

    data, source_files = load_yaml_config_source(path)

    try:
        authored = Config.model_validate(data)
        config = RuntimeConfig.from_authored(
            authored,
            runtime_paths,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
            source_files=source_files,
        )
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        # Parsing succeeded, so the full file set is known; expose it the same
        # way as parse-time failures so reload watchers keep covering it.
        attach_partial_source_files(exc, source_files)
        raise
    logger.info("loaded_agent_configuration", path=str(path), source_file_count=len(source_files))
    logger.info("loaded_agent_configuration_count", agent_count=len(config.agents))
    return config


def load_config_or_user_error(
    runtime_paths: RuntimePaths,
    *,
    footer: str | None = None,
    tolerate_plugin_load_errors: bool = False,
) -> tuple[RuntimeConfig | None, str | None]:
    """Load config or return one shared user-facing invalid-configuration message."""
    try:
        return load_config(
            runtime_paths,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
        ), None
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        return None, format_invalid_config_message(exc, footer=footer)
