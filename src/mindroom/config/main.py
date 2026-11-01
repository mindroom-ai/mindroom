"""Root configuration model and helpers."""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from functools import cache
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from mindroom import yaml_io
from mindroom.access_policy import resolve_responder_access
from mindroom.agent_policy import (
    PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    PolicyInputs,
    build_agent_policy_seeds,
    get_agent_delegation_closure,
    get_unsupported_team_agents,
    resolve_agent_policy_index,
    unsupported_team_agent_message,
)
from mindroom.config.access import OwnerPlaceholderUserIds, RoomDefaultsConfig
from mindroom.config.agent import AgentConfig, RoomConfig, TeamConfig  # noqa: TC001
from mindroom.config.approval import ToolApprovalConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.budgets import BudgetsConfig  # noqa: TC001
from mindroom.config.calls import CallsConfig, CascadedCallProfile, LiveCallProfile
from mindroom.config.egress_broker import EgressBrokerConfig
from mindroom.config.external_trigger_policy import ExternalTriggerPolicyConfig
from mindroom.config.generated_agents import GeneratedAgentInfo, expand_generated_agents
from mindroom.config.judgment import LLMJudgmentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeRerankerConfig
from mindroom.config.knowledge_graph import KnowledgeGraphConfig
from mindroom.config.legacy_access import (
    AccessMigrationError,
    migrate_access_config_data,
    persist_access_migration,
    validate_access_migration_source,
)
from mindroom.config.matrix import (
    EventJournalConfig,
    MatrixSpaceConfig,
    MatrixSyncConfig,
    MindRoomUserConfig,
    ServerEmojiConfig,
)
from mindroom.config.memory import MemoryConfig
from mindroom.config.models import (
    TOOL_PRESETS,
    BackgroundToolJobsConfig,
    DebugConfig,
    DefaultsConfig,
    ModelConfig,
    RouterConfig,
    ToolConfigEntry,
)
from mindroom.config.personal_agents import (
    PersonalAgentInstance,
    PersonalAgentTemplateConfig,
    expand_personal_agents,
    personal_agent_roster_lock_path,
    read_personal_agent_roster_file,
)
from mindroom.config.personal_rooms import PersonalRoomsConfig  # noqa: TC001
from mindroom.config.plugin import PluginEntryConfig  # noqa: TC001
from mindroom.config.resolution import (
    ResolvedAgent,
    ResolvedScope,
    agent_policy,
    agent_tool_facts,
    authored_compaction_model_fields,
    authored_tool_entries,
    automation_block_reason,
    configured_entity,
    memory_backend,
    resolve_agent,
    resolve_scope,
    uses_knowledge_graph,
)
from mindroom.config.resolution import cheap_model as agent_cheap_model
from mindroom.config.schema_hints import DashboardJsonSchema, dashboard_hint, redact_config_for_display
from mindroom.config.slack import SlackConfig
from mindroom.config.validation import TimezoneName  # noqa: TC001
from mindroom.config.voice import VoiceConfig
from mindroom.config.yaml_includes import (
    ConfigIncludeError,
    attach_partial_source_files,
    load_yaml_config_source_with_digests,
    source_files_fingerprint,
)
from mindroom.constants import (
    AGENT_NAME_PATTERN,
    DEFAULT_WORKER_GRANTABLE_CREDENTIALS,
    RESERVED_ENTITY_NAMES,
    ROUTER_AGENT_NAME,
    ROUTER_DISPLAY_NAME,
    RuntimePaths,
    config_relative_path,
    matrix_state_file,
    resolve_config_relative_path,
    runtime_matrix_homeserver,
)
from mindroom.credentials.manager import SERVICE_NAME_CHARACTERS, SERVICE_NAME_PATTERN
from mindroom.entity_resolution import (
    canonical_room_reference,
    names_room,
    resolve_agent_thread_mode,
    router_agents_for_room,
)
from mindroom.file_locks import advisory_file_lock
from mindroom.git_urls import credential_free_repo_url

# config layer loads BEFORE the history runtime; import leaf types so config load does not drag in agents+tools.
from mindroom.integrations import validate_integrations
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix_identifiers import (
    extract_server_name_from_homeserver,
    is_managed_room_key,
    managed_room_alias_localpart,
    managed_space_alias_localpart,
    server_emoji_alias_localpart,
)
from mindroom.mcp.config import MCPServerConfig, normalize_mcp_server_id
from mindroom.prompt_templates import render_prompt_template, validate_prompt_template_fields
from mindroom.prompts import PROMPT_DEFAULT_NAMES, PROMPT_DEFAULTS
from mindroom.room_thread_modes import resolve_room_thread_mode_override
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.tool_system.plugin_imports import PluginValidationError
from mindroom.tool_system.worker_routing import unsupported_shared_only_integration_names
from mindroom.workspaces import control_plane_owns_private_templates, validate_workspace_template_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from pydantic_core import ErrorDetails

    from mindroom.agent_policy import AgentPolicySeed, ResolvedAgentPolicy
    from mindroom.config.personal_agents import PersonalAgentRoster
    from mindroom.tool_system.registry import ToolRegistry
    from mindroom.tool_system.worker_routing import WorkerScope

_APPROVED_EGRESS_ENABLED_ENV = "MINDROOM_APPROVED_EGRESS_ENABLED"


logger = get_logger(__name__)


def _persisted_account_usernames(runtime_paths: RuntimePaths) -> dict[str, str]:
    state_file = matrix_state_file(runtime_paths=runtime_paths)
    if not state_file.exists():
        return {}
    data = yaml_io.safe_load(state_file.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    usernames: dict[str, str] = {}
    for account_key, account in accounts.items():
        if not isinstance(account_key, str) or not isinstance(account, dict):
            continue
        username = account.get("username")
        if isinstance(username, str) and username:
            usernames[account_key] = username
    return usernames


# Optional root sections an explicit YAML null leaves empty, and that authored serialization drops when empty.
_OPTIONAL_SECTION_NAMES = (
    "teams",
    "rooms",
    "room_models",
    "room_thread_summary_models",
    "knowledge_bases",
    "mcp_servers",
    "prompts",
    "matrix_space",
    "server_emoji",
    "debug",
    "external_trigger_policy",
    "matrix_sync",
    "tool_approval",
)


class ConfigRuntimeValidationError(ValueError):
    """Runtime-aware config validation failed after Pydantic schema validation."""


class ConfigFileChangedError(ConfigRuntimeValidationError):
    """The config file changed after a writer loaded the config its change came from."""

    def __init__(self) -> None:
        super().__init__("The configuration file changed after this change read it; retry the change.")


type ConfigLoadUserError = (
    ValidationError | ConfigRuntimeValidationError | AccessMigrationError | yaml.YAMLError | OSError | UnicodeError
)


CONFIG_LOAD_USER_ERROR_TYPES = (
    ValidationError,
    ConfigRuntimeValidationError,
    AccessMigrationError,
    yaml.YAMLError,
    OSError,
    UnicodeError,
)


def validation_issue_message(error: ErrorDetails) -> str:
    """Return what a user reads for one Pydantic error: how to fix an unknown key, or the message a validator raised."""
    if error["type"] == "extra_forbidden":
        return f"Unknown key '{error['loc'][-1]}': remove it or correct its spelling"
    # Pydantic writes a validator's raised message after this prefix.
    return error["msg"].removeprefix("Value error, ")


def config_validation_issues(
    exc: Exception,
) -> list[tuple[tuple[str | int, ...], str]]:
    """Return the user-facing messages of one config validation exception, each with its config location.

    An issue about the whole config is located at `config`.
    """
    if isinstance(exc, ValidationError):
        return [
            (error["loc"] or ("config",), validation_issue_message(error))
            for error in exc.errors(include_context=False)
        ]
    if isinstance(exc, (ConfigRuntimeValidationError, ConfigIncludeError)):
        return [(("config",), str(exc))]
    if isinstance(exc, yaml.YAMLError):
        return [(("config",), f"Could not parse configuration YAML: {exc}")]
    if isinstance(exc, UnicodeError):
        return [(("config",), f"Could not read configuration text: {exc}")]
    return [(("config",), f"Could not load configuration: {exc}")]


def iter_config_validation_messages(
    exc: Exception,
) -> list[tuple[str, str]]:
    """Return the messages of ``config_validation_issues`` with each location written as text."""
    return [
        (" → ".join(str(part) for part in location), message) for location, message in config_validation_issues(exc)
    ]


def config_validation_summary(exc: Exception) -> str:
    """Return a config validation error's issues on one line as ``<location>: <message>``, or another error's text."""
    if not isinstance(exc, (ValidationError, ConfigRuntimeValidationError)):
        return str(exc)
    return "; ".join(f"{location}: {message}" for location, message in iter_config_validation_messages(exc))


def format_invalid_config_message(
    exc: ConfigLoadUserError,
    *,
    footer: str | None = None,
) -> str:
    """Return one shared message for a refused config change: invalid, or the file changed since it was read."""
    if isinstance(exc, ConfigFileChangedError):
        response = f"❌ {exc}"
    else:
        errors = [f"• {location}: {message}" for location, message in iter_config_validation_messages(exc)]
        response = f"❌ Invalid configuration:\n{'\n'.join(errors)}"
    if footer:
        response = f"{response}\n\n{footer}"
    return response


def _strip_empty_root_sections(payload: dict[str, Any]) -> dict[str, Any]:
    """Drop normalized empty root sections from authored config serialization."""
    authored_payload = dict(payload)
    for name in _OPTIONAL_SECTION_NAMES:
        if authored_payload.get(name) == {}:
            authored_payload.pop(name, None)
    if authored_payload.get("plugins") == []:
        authored_payload.pop("plugins", None)
    return authored_payload


def _relative_paths_overlap(left: Path, right: Path) -> bool:
    """Return whether two relative paths overlap by equality, ancestry, or descent."""
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


@dataclass(frozen=True)
class _KnowledgeBaseSourceSemantics:
    """Source ownership semantics that must match for exact duplicate roots."""

    git_enabled: bool
    git_repo_identity: str
    git_branch: str
    git_credentials_service: str | None
    git_lfs: bool


def _knowledge_base_source_semantics(base_config: KnowledgeBaseConfig) -> _KnowledgeBaseSourceSemantics:
    """Return the source semantics for duplicate-path compatibility checks."""
    git_config = base_config.git
    return _KnowledgeBaseSourceSemantics(
        git_enabled=git_config is not None,
        git_repo_identity=credential_free_repo_url(git_config.repo_url) if git_config is not None else "",
        git_branch=git_config.branch if git_config is not None else "",
        git_credentials_service=git_config.credentials_service if git_config is not None else None,
        git_lfs=git_config.lfs if git_config is not None else False,
    )


def _template_contains_overlapping_subtree(template_dir: Path, target_path: Path) -> bool:
    """Return whether a template already seeds content at or around one target subtree."""
    if not template_dir.is_dir():
        return False
    return any(
        _relative_paths_overlap(source_path.relative_to(template_dir), target_path)
        for source_path in template_dir.rglob("*")
    )


def _context_runtime_paths(info: ValidationInfo) -> RuntimePaths | None:
    """Return the runtime context a caller passed to validation, if any."""
    return info.context.get("runtime_paths") if isinstance(info.context, dict) else None


class Config(BaseModel):
    """Complete configuration from YAML."""

    # Config carries API keys, so rendered validation errors must never echo input values.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    _source_files: frozenset[Path] = PrivateAttr(default=frozenset())
    _source_fingerprint: str | None = PrivateAttr(default=None)
    _uses_includes: bool = PrivateAttr(default=False)
    _approved_egress_enabled: bool = PrivateAttr(default=False)
    _runtime_knowledge_base_overlays: dict[str, KnowledgeBaseConfig] = PrivateAttr(default_factory=dict)
    _personal_agent_instances: dict[str, PersonalAgentInstance] = PrivateAttr(default_factory=dict)
    _personal_agent_call_templates: dict[str, str] = PrivateAttr(default_factory=dict)
    _personal_agent_call_agents: dict[str, str] = PrivateAttr(default_factory=dict)
    _generated_agent_dumps: dict[str, dict[str, Any]] = PrivateAttr(default_factory=dict)
    _generated_agents: dict[str, GeneratedAgentInfo] = PrivateAttr(default_factory=dict)
    _personal_agents_added_agents_section: bool = PrivateAttr(default=False)

    egress_broker: EgressBrokerConfig = Field(
        default_factory=EgressBrokerConfig,
        description="Credential-injecting egress broker for worker shell and python traffic",
    )

    IMPLIED_TOOLS: ClassVar[dict[str, tuple[str, ...]]] = {
        "matrix_message": ("attachments", "matrix_room"),
    }

    administrators: OwnerPlaceholderUserIds = Field(
        default_factory=list,
        description="Concrete Matrix user IDs with platform-wide administrative authority",
    )
    room_defaults: RoomDefaultsConfig = Field(
        default_factory=RoomDefaultsConfig,
        description="Desired Matrix state inherited by managed rooms",
    )
    agents: dict[str, AgentConfig] = Field(default_factory=dict, description="Agent configurations")
    teams: dict[str, TeamConfig] = Field(default_factory=dict, description="Team configurations")
    rooms: dict[str, RoomConfig] = Field(default_factory=dict, description="Managed Matrix room metadata")
    room_models: dict[str, str] = Field(
        default_factory=dict,
        description="Room-specific model overrides",
        json_schema_extra=dashboard_hint(key_reference="room", reference="model"),
    )
    room_thread_summary_models: dict[str, str] = Field(
        default_factory=dict,
        description="Room-specific model overrides for automatic thread summaries",
        json_schema_extra=dashboard_hint(key_reference="room", reference="model"),
    )
    plugins: list[PluginEntryConfig] = Field(default_factory=list, description="Plugin entries")
    debug: DebugConfig = Field(default_factory=DebugConfig, description="Debug and diagnostic settings")
    prompts: dict[str, str] = Field(
        default_factory=dict,
        description="Built-in prompt overrides keyed by the uppercase global name from mindroom.prompts",
        json_schema_extra=dashboard_hint(multiline=True),
    )
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig, description="Default values")
    memory: MemoryConfig = Field(default_factory=MemoryConfig, description="Memory configuration")
    knowledge_graph: KnowledgeGraphConfig = Field(
        default_factory=KnowledgeGraphConfig,
        description="Knowledge graph built from graph-enabled agents' conversations",
    )
    knowledge_bases: dict[str, KnowledgeBaseConfig] = Field(
        default_factory=dict,
        description="Knowledge base configurations keyed by base ID",
    )
    knowledge_reranker: KnowledgeRerankerConfig | None = Field(
        default=None,
        description="Optional reranker for search_knowledge_base results; searches keep hybrid ranking without one",
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
    personal_agents: dict[str, PersonalAgentTemplateConfig] = Field(
        default_factory=dict,
        description=(
            "Templates that give every onboarded user their own agent; "
            "select a template's key as the Personal rooms agent to use it"
        ),
        json_schema_extra=dashboard_hint(key_placeholder="mind"),
    )
    personal_rooms: PersonalRoomsConfig | None = Field(default=None, description="Optional native personal agent rooms")
    budgets: BudgetsConfig | None = Field(default=None, description="Optional per-user monthly spending budgets")
    voice: VoiceConfig = Field(default_factory=VoiceConfig, description="Voice configuration")
    calls: CallsConfig = Field(default_factory=CallsConfig, description="Voice call (MatrixRTC) configuration")
    background_tool_jobs: BackgroundToolJobsConfig = Field(
        default_factory=BackgroundToolJobsConfig,
        description="Opt-in managed background tool execution; turning it on or off and its exclusions need a restart.",
    )
    event_journal: EventJournalConfig = Field(
        default_factory=EventJournalConfig,
        description="Durable Matrix event-journal store",
    )
    matrix_sync: MatrixSyncConfig = Field(
        default_factory=MatrixSyncConfig,
        description="Matrix event sync transport configuration",
    )
    slack: SlackConfig = Field(
        default_factory=SlackConfig,
        description="Slack integration: how Slack workspace members map to Matrix users",
    )
    timezone: TimezoneName = Field(
        default="UTC",
        description="IANA timezone for dates, times, and schedules (e.g., 'America/New_York'); agents can override it",
    )
    scheduler_catch_up_grace_seconds: int = Field(
        default=3600,
        ge=0,
        description="Maximum lateness for recovering a missed recurring task; 0 disables catch-up",
    )
    mindroom_user: MindRoomUserConfig | None = Field(
        default=None,
        description="Configuration for the internal MindRoom user account (omit for hosted/public profiles)",
    )
    matrix_space: MatrixSpaceConfig = Field(
        default_factory=MatrixSpaceConfig,
        description="Optional root Matrix Space for grouping managed rooms",
    )
    server_emoji: ServerEmojiConfig = Field(
        default_factory=ServerEmojiConfig,
        description="Optional room whose emoji packs every MindRoom Chat user on the homeserver can use",
    )
    authorization: AuthorizationConfig = Field(
        default_factory=AuthorizationConfig,
        description="Authorization configuration with fine-grained permissions",
    )
    bot_accounts: list[str] = Field(
        default_factory=list,
        description="Matrix user IDs of non-MindRoom bots (e.g., bridge bots) that should be treated like agents for response logic — their messages won't trigger the multi-human-thread mention requirement",
    )

    @field_validator("models")
    @classmethod
    def validate_model_names(cls, models: dict[str, ModelConfig]) -> dict[str, ModelConfig]:
        """Reject an empty model name, or one with a character its dashboard key ``model:<name>`` cannot carry."""
        invalid = sorted(name for name in models if not SERVICE_NAME_PATTERN.fullmatch(name))
        if invalid:
            keys = ", ".join(f"models.{name}" for name in invalid)
            msg = f"Model names can only include {SERVICE_NAME_CHARACTERS}; rename {keys} and update every reference"
            raise ValueError(msg)
        return models

    @model_validator(mode="after")
    def validate_budgets(self) -> Config:
        """Require a configured fallback model for budgets."""
        if self.budgets is not None and self.budgets.fallback_model not in self.models:
            msg = f"Unknown budgets fallback_model: {self.budgets.fallback_model!r}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_memory_auto_flush_model(self) -> Config:
        """Require the auto-flush extractor model to name a configured model."""
        extractor_model = self.memory.auto_flush.extractor.model
        if extractor_model is not None and extractor_model not in self.models:
            msg = f"Unknown memory.auto_flush.extractor.model: {extractor_model!r}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_personal_rooms(self) -> Config:
        """Require a real target and onboarding rooms observed by the router."""
        if self.personal_rooms is not None:
            if self.personal_rooms.agent not in self.agents and self.personal_rooms.agent not in self.personal_agents:
                msg = "personal_rooms.agent must name a configured agent or personal_agents template"
                raise ValueError(msg)
            if set(self.personal_rooms.onboarding_rooms) - self.get_all_configured_rooms():
                msg = "personal_rooms.onboarding_rooms must name configured rooms"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_personal_agent_templates(self) -> Config:
        """Keep template keys valid agent names apart from agents and teams, whose names generated agents share."""
        invalid = sorted(
            key for key in self.personal_agents if not AGENT_NAME_PATTERN.fullmatch(key) or key in RESERVED_ENTITY_NAMES
        )
        if invalid:
            msg = f"personal_agents keys must be alphanumeric/underscore and not reserved, got: {', '.join(invalid)}"
            raise ValueError(msg)
        reused = sorted(set(self.personal_agents) & (set(self.agents) | set(self.teams)))
        if reused:
            msg = f"personal_agents keys must not equal an agent or team key: {', '.join(reused)}"
            raise ValueError(msg)
        return self

    @model_validator(mode="before")
    @classmethod
    def normalize_root_config(cls, data: object) -> object:
        """Replace explicit YAML nulls in optional root sections and migrate retired access settings."""
        if not isinstance(data, dict):
            return data
        normalized = cast("dict[str, Any]", data.copy())
        for name in _OPTIONAL_SECTION_NAMES:
            if normalized.get(name) is None:
                normalized[name] = {}
        if normalized.get("plugins") is None:
            normalized["plugins"] = []
        return migrate_access_config_data(normalized).data

    @field_validator("plugins", mode="before")
    @classmethod
    def normalize_plugins(cls, value: object) -> object:
        """Normalize legacy string plugin entries into structured config objects."""
        if not isinstance(value, list):
            return value
        return [{"path": plugin_entry} if isinstance(plugin_entry, str) else plugin_entry for plugin_entry in value]

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
        invalid_agents = [name for name in self.agents if not AGENT_NAME_PATTERN.fullmatch(name)]
        invalid_teams = [name for name in self.teams if not AGENT_NAME_PATTERN.fullmatch(name)]
        invalid_mcp_servers = [name for name in self.mcp_servers if not AGENT_NAME_PATTERN.fullmatch(name)]
        invalid = sorted(invalid_agents + invalid_teams + invalid_mcp_servers)
        if invalid:
            msg = f"Agent, team, and MCP server names must be alphanumeric/underscore only, got: {', '.join(invalid)}"
            raise ValueError(msg)
        overlapping_names = sorted(set(self.agents) & set(self.teams))
        if overlapping_names:
            msg = f"Agent and team names must be distinct, overlapping keys: {', '.join(overlapping_names)}"
            raise ValueError(msg)
        reserved_entity_names = sorted((set(self.agents) | set(self.teams)) & RESERVED_ENTITY_NAMES)
        if reserved_entity_names:
            msg = (
                f"Agent and team names must not use reserved internal entity names: {', '.join(reserved_entity_names)}"
            )
            raise ValueError(msg)
        for server_id in self.mcp_servers:
            normalize_mcp_server_id(server_id)
        return self

    @model_validator(mode="after")
    def validate_egress_service_agents(self) -> Config:
        """Ensure each egress service's `agents` list names configured agents."""
        for name, service in self.egress_broker.services.items():
            unknown_agents = sorted(set(service.agents or ()) - set(self.agents))
            if unknown_agents:
                msg = f"egress_broker.services.{name}.agents references unknown agent(s): {', '.join(unknown_agents)}"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_call_agents(self) -> Config:
        """Ensure call agents and normal-agent model references are valid."""
        unknown_agents = sorted(set(self.calls.agents) - set(self.agents))
        if unknown_agents:
            msg = f"calls.agents references unknown agent(s): {', '.join(unknown_agents)}"
            raise ValueError(msg)

        invalid_models = sorted(
            f"{profile_name} -> {profile.model}"
            for profile_name, profile in self.calls.profiles.items()
            if isinstance(profile, CascadedCallProfile)
            and profile.model is not None
            and profile.model not in self.models
        )
        if invalid_models:
            msg = "calls.profiles references unknown cascaded model(s): " + ", ".join(invalid_models)
            raise ValueError(msg)

        invalid_live_models = sorted(
            f"{profile_name} -> {profile.agent_model}"
            for profile_name, profile in self.calls.profiles.items()
            if isinstance(profile, LiveCallProfile)
            and profile.agent_model is not None
            and profile.agent_model not in self.models
        )
        if invalid_live_models:
            msg = "calls.profiles references unknown Live agent model(s): " + ", ".join(invalid_live_models)
            raise ValueError(msg)

        self._reject_call_agents_sharing_a_room(lambda room: room)
        return self

    def _reject_call_agents_sharing_a_room(self, room_key: Callable[[str], str]) -> None:
        """Reject a room, compared by ``room_key``, that more than one call agent joins."""
        agents_by_room: dict[str, list[str]] = {}
        for agent_name in self.calls.agents:
            for room in self.agents[agent_name].rooms:
                agents_by_room.setdefault(room_key(room), []).append(agent_name)
        conflicts = [
            f"{room} ({', '.join(sorted(agent_names))})"
            for room, agent_names in sorted(agents_by_room.items())
            if len(agent_names) > 1
        ]
        if conflicts:
            msg = "calls.agents configures multiple agents for room(s): " + "; ".join(conflicts)
            raise ConfigRuntimeValidationError(msg)

    @model_validator(mode="after")
    def validate_membership_access_room_references(self) -> Config:
        """Require membership grants to reference configured managed-room keys."""
        configured_managed_room_keys = self.managed_room_keys()
        responder_names = (*self.agents, *self.teams, ROUTER_AGENT_NAME)
        invalid_room_references = sorted(
            f"{entity_name} -> {room_key}"
            for entity_name in responder_names
            for room_key in resolve_responder_access(self, entity_name).members_of_rooms
            if room_key not in configured_managed_room_keys
        )
        if invalid_room_references:
            msg = "Responder access members_of_rooms must reference configured managed room keys: " + ", ".join(
                invalid_room_references,
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_integration_config(self) -> Config:
        """Run each built-in integration's own config checks."""
        validate_integrations(self)
        return self

    @model_validator(mode="after")
    def validate_delegate_to(self) -> Config:
        """Ensure delegate_to targets exist."""
        for agent_name, agent_config in self.agents.items():
            for target in agent_config.delegate_to:
                if target not in self.agents:
                    msg = f"Agent '{agent_name}' delegates to unknown agent '{target}'"
                    raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_team_agents(self) -> Config:
        """Ensure team members exist and do not use private requester-local state."""
        for team_name, team_config in self.teams.items():
            self.assert_team_agents_supported(team_config.agents, team_name=team_name)
        return self

    def _explicit_compaction_model_references(self) -> list[tuple[str, str]]:
        """Return `(label, model)` for each explicit compaction model reference in defaults, agents, and teams."""
        scoped_compactions = [
            ("defaults", self.defaults.compaction),
            *((f"agents.{agent_name}", agent_config.compaction) for agent_name, agent_config in self.agents.items()),
            *((f"teams.{team_name}", team_config.compaction) for team_name, team_config in self.teams.items()),
        ]
        return [
            (f"{scope_label}.compaction.{field_name}", authored_model.value)
            for scope_label, compaction in scoped_compactions
            if compaction is not None
            for field_name, authored_model in authored_compaction_model_fields(compaction).items()
            if authored_model.value is not None
        ]

    @model_validator(mode="after")
    def validate_compaction_model_references(self) -> Config:
        """Ensure explicit compaction model and fallback_model references are statically valid."""
        references = self._explicit_compaction_model_references()
        invalid_references = sorted(f"{label} -> {model}" for label, model in references if model not in self.models)
        if invalid_references:
            msg = "Compaction model references unknown models: " + ", ".join(invalid_references)
            raise ValueError(msg)

        missing_context_windows = sorted(
            f"{label} -> {model}" for label, model in references if self.models[model].context_window is None
        )
        if missing_context_windows:
            msg = "Explicit compaction model references require a model with context_window: " + ", ".join(
                missing_context_windows,
            )
            raise ValueError(msg)

        return self

    @model_validator(mode="after")
    def validate_shared_only_integration_assignments(self) -> Config:
        """Reject shared-only integrations on isolating scopes for static and dynamic tool assignments."""
        invalid_assignments: list[str] = []
        for agent_name in sorted(self.agents):
            policy = agent_policy(self, agent_name)
            facts = agent_tool_facts(self, agent_name, policy=policy)
            scope_label = policy.scope_label
            unsupported_tools = unsupported_shared_only_integration_names(
                self.expand_tool_names([entry.name for entry in facts.authored if not entry.defer]),
                policy.effective_execution_scope,
            )
            invalid_assignments.extend(
                f"{agent_name} -> {tool_name} ({scope_label})" for tool_name in unsupported_tools
            )
            for authored_name, incompatible_tools in facts.scope_incompatible.items():
                invalid_assignments.extend(
                    f"{agent_name} -> deferred tool '{authored_name}' -> {tool_name} ({scope_label})"
                    for tool_name in incompatible_tools
                )
        if invalid_assignments:
            msg = (
                "Shared-only integrations are supported only for unscoped agents or worker_scope=shared. "
                f"Invalid assignments: {', '.join(invalid_assignments)}"
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
            formatted = ", ".join(f"{agent_name} -> {base_id}" for agent_name, base_id in sorted(invalid_assignments))
            msg = f"Agents reference unknown knowledge bases: {formatted}"
            raise ValueError(msg)
        return self

    @classmethod
    def _validate_knowledge_base_ids(cls, base_ids: Collection[str]) -> None:
        """Apply authored knowledge-base ID constraints to any config entry point."""
        reserved_ids = sorted(base_id for base_id in base_ids if base_id.startswith(PRIVATE_KNOWLEDGE_BASE_ID_PREFIX))
        if reserved_ids:
            formatted = ", ".join(reserved_ids)
            msg = (
                "knowledge_bases keys must not use the reserved private prefix "
                f"'{PRIVATE_KNOWLEDGE_BASE_ID_PREFIX}'; invalid keys: {formatted}"
            )
            raise ValueError(msg)

        invalid_ids = sorted(base_id for base_id in base_ids if "\n" in base_id or "\r" in base_id)
        if invalid_ids:
            formatted = ", ".join(invalid_ids)
            msg = f"knowledge_bases keys must not contain line breaks; invalid keys: {formatted}"
            raise ValueError(msg)

        invalid_ids = sorted(
            base_id
            for base_id in base_ids
            if not base_id.strip() or base_id in {".", ".."} or "/" in base_id or "\\" in base_id
        )
        if invalid_ids:
            formatted = ", ".join(invalid_ids)
            msg = (
                "knowledge_bases keys must be non-empty single path components without path separators "
                f"or dot segments; invalid keys: {formatted}"
            )
            raise ValueError(msg)

    @model_validator(mode="after")
    def validate_knowledge_base_ids(self) -> Config:
        """Reject reserved, multiline, empty, and path-unsafe knowledge base IDs."""
        self._validate_knowledge_base_ids(self.knowledge_bases)
        return self

    @model_validator(mode="after")
    def validate_knowledge_base_paths_do_not_overlap(self, info: ValidationInfo) -> Config:
        """Reject parent/child top-level knowledge roots while allowing exact aliases."""
        runtime_paths = _context_runtime_paths(info)
        if runtime_paths is None or len(self.knowledge_bases) < 2:
            return self

        resolved_paths = [
            (base_id, resolve_config_relative_path(base_config.path, runtime_paths))
            for base_id, base_config in self.knowledge_bases.items()
        ]
        # Path ordering groups aliases and puts parents before their descendants;
        # any overlap therefore includes a neighboring pair.
        resolved_paths.sort(key=lambda entry: entry[1])
        for (base_id, root), (other_base_id, other_root) in pairwise(resolved_paths):
            if root == other_root:
                semantics = _knowledge_base_source_semantics(self.knowledge_bases[base_id])
                other_semantics = _knowledge_base_source_semantics(self.knowledge_bases[other_base_id])
                if semantics != other_semantics:
                    msg = (
                        "knowledge_bases exact duplicate aliases must use compatible source configuration; "
                        f"'{base_id}' and '{other_base_id}' both resolve to '{root}'"
                    )
                    raise ValueError(msg)
                continue
            if other_root.is_relative_to(root):
                msg = (
                    "knowledge_bases paths must not overlap unless they are exact duplicate aliases; "
                    f"'{base_id}' resolves to '{root}' and '{other_base_id}' resolves to '{other_root}'"
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
    def validate_private_git_knowledge_paths(self, info: ValidationInfo) -> Config:
        """Ensure git-backed private knowledge uses a dedicated subtree."""
        memory_notes_dir = Path("memory")
        memory_notes_entrypoint = Path("MEMORY.md")
        runtime_paths = _context_runtime_paths(info)
        for agent_name, agent_config in self.agents.items():
            private_config = agent_config.private
            if private_config is None or private_config.knowledge is None:
                continue
            private_knowledge = private_config.knowledge
            if private_knowledge.git is None or private_knowledge.path is None:
                continue
            knowledge_path = Path(private_knowledge.path)
            overlaps_memory_notes = _relative_paths_overlap(knowledge_path, memory_notes_dir)
            template_dir = private_config.template_dir
            if (
                knowledge_path == Path()
                or (
                    memory_backend(self, agent_name) == "file"
                    and (overlaps_memory_notes or _relative_paths_overlap(knowledge_path, memory_notes_entrypoint))
                )
                or (template_dir is not None and overlaps_memory_notes)
                or (
                    template_dir is not None
                    and runtime_paths is not None
                    and _template_contains_overlapping_subtree(
                        config_relative_path(template_dir, runtime_paths),
                        knowledge_path,
                    )
                )
            ):
                msg = (
                    f"Agent '{agent_name}' uses git-backed private knowledge at '{private_knowledge.path}', "
                    "but git-backed private knowledge must use a dedicated subtree outside the private root "
                    "and outside scaffolded private workspace content"
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_private_template_dirs(self, info: ValidationInfo) -> Config:
        """Ensure private template directories exist when runtime path resolution is available."""
        runtime_paths = _context_runtime_paths(info)
        if runtime_paths is None or control_plane_owns_private_templates(runtime_paths):
            return self
        for agent_name, agent_config in self.agents.items():
            private_config = agent_config.private
            if private_config is None or private_config.template_dir is None:
                continue
            template_dir = config_relative_path(private_config.template_dir, runtime_paths)
            try:
                validate_workspace_template_dir(template_dir)
            except ValueError as exc:
                msg = f"Agent '{agent_name}' has invalid private.template_dir: {exc}"
                raise ValueError(msg) from exc
        return self

    @model_validator(mode="after")
    def validate_internal_user_username_not_reserved(self, info: ValidationInfo) -> Config:
        """Ensure the internal user localpart does not collide with bot accounts."""
        if self.mindroom_user is None:
            return self
        runtime_paths = _context_runtime_paths(info)
        if runtime_paths is None:
            return self
        reserved_localparts: dict[str, str] = {}
        persisted_usernames = _persisted_account_usernames(runtime_paths)
        entity_names = [ROUTER_AGENT_NAME, *self.agents, *self.teams]
        for entity_name in entity_names:
            persisted_username = persisted_usernames.get(managed_account_key(entity_name))
            if persisted_username is None:
                continue
            if entity_name == ROUTER_AGENT_NAME:
                label = f"router '{ROUTER_AGENT_NAME}'"
            elif entity_name in self.agents:
                label = f"agent '{entity_name}'"
            else:
                label = f"team '{entity_name}'"
            reserved_localparts[persisted_username] = label
        conflict = reserved_localparts.get(self.mindroom_user.username)
        if conflict:
            msg = f"mindroom_user.username '{self.mindroom_user.username}' conflicts with {conflict} Matrix localpart"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_reserved_aliases_do_not_collide_with_managed_rooms(self, info: ValidationInfo) -> Config:
        """Ensure no managed room key maps to the reserved alias of the root Space or the server emoji room."""
        runtime_paths = _context_runtime_paths(info)
        if runtime_paths is None:
            return self
        reserved_aliases: dict[str, str] = {}
        if self.matrix_space.enabled:
            reserved_aliases[managed_space_alias_localpart(runtime_paths=runtime_paths)] = "root Space"
        if self.server_emoji.enabled:
            reserved_aliases[server_emoji_alias_localpart(runtime_paths)] = "server emoji room"
        for reserved_alias_localpart, owner in reserved_aliases.items():
            colliding_rooms = sorted(
                room_key
                for room_key in self.managed_room_keys()
                if managed_room_alias_localpart(room_key, runtime_paths=runtime_paths) == reserved_alias_localpart
            )
            if colliding_rooms:
                formatted = ", ".join(colliding_rooms)
                msg = f"Managed room keys conflict with the reserved {owner} alias '{reserved_alias_localpart}': {formatted}"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_room_references_name_rooms(self, info: ValidationInfo) -> Config:
        """Reject a room reference that the runtime cannot map to a room ID."""
        runtime_paths = _context_runtime_paths(info)
        if runtime_paths is None:
            return self
        references: list[tuple[str, str | None]] = [("rooms", room) for room in self.rooms]
        for name, agent in self.agents.items():
            references += [(f"agents.{name}.rooms", room) for room in agent.rooms]
            references += [(f"agents.{name}.room_thread_modes", room) for room in agent.room_thread_modes]
            references += [
                (f"agents.{name}.automations.{automation.name}.room", automation.room)
                for automation in agent.automations or ()
            ]
        references += [(f"teams.{name}.rooms", room) for name, team in self.teams.items() for room in team.rooms]
        references += [
            (f"defaults.automations.{automation.name}.room", automation.room)
            for automation in self.defaults.automations
        ]
        references += [("room_models", room) for room in self.room_models]
        # A summary model may also be keyed by the responding agent or team, for rooms without a configured key.
        references += [
            ("room_thread_summary_models", room)
            for room in self.room_thread_summary_models
            if room not in self.agents and room not in self.teams
        ]
        unknown_rooms = sorted(
            f"{location} -> {room}"
            for location, room in references
            if room is not None and not names_room(self, room, runtime_paths)
        )
        if unknown_rooms:
            msg = (
                "Each room reference must be a room key MindRoom manages, that room's full alias, or a ! room ID: "
                + ", ".join(unknown_rooms)
            )
            raise ValueError(msg)
        return self

    def get_domain(self, runtime_paths: RuntimePaths) -> str:
        """Extract the Matrix domain for one explicit runtime context."""
        homeserver = runtime_matrix_homeserver(runtime_paths)
        return extract_server_name_from_homeserver(homeserver, runtime_paths)

    @property
    def source_files(self) -> frozenset[Path]:
        """Files this config was loaded from: the top-level file plus every include.

        Empty when the config was constructed without YAML source metadata.
        """
        return self._source_files

    @property
    def source_fingerprint(self) -> str | None:
        """Fingerprint of source bytes captured at load time, including includes."""
        return self._source_fingerprint

    @property
    def uses_includes(self) -> bool:
        """Whether the loaded YAML source resolved include or expansion tags."""
        return self._uses_includes

    def record_source_metadata(
        self,
        config_path: Path,
        source_digests: dict[Path, str],
        *,
        uses_includes: bool,
    ) -> None:
        """Retain the source identity and topology of one validated YAML config."""
        self._source_files = frozenset(source_digests)
        self._source_fingerprint = source_files_fingerprint(config_path, source_digests)
        self._uses_includes = uses_includes

    @classmethod
    def validate_with_runtime(
        cls,
        data: object,
        runtime_paths: RuntimePaths,
        *,
        tolerate_plugin_load_errors: bool = False,
        roster: PersonalAgentRoster | None = None,
    ) -> Config:
        """Validate config data against one explicit runtime context, adding the agents of the personal agent roster.

        A candidate `roster` replaces the stored one, and generated agents it makes invalid raise instead of being left out.
        """
        candidate = roster is not None
        try:
            roster = read_personal_agent_roster_file(runtime_paths) if roster is None else roster
            expansion = expand_personal_agents(data, roster.entries)
        except ValueError as exc:
            raise ConfigRuntimeValidationError(str(exc)) from exc
        config = cls._validate_runtime_data(
            expansion.data,
            runtime_paths,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
        )
        if expansion.probe_data is not None:
            # A template or override nobody uses yet must fail now, not at a later reload that onboards someone.
            try:
                cls._validate_runtime_data(
                    expansion.probe_data,
                    runtime_paths,
                    tolerate_plugin_load_errors=tolerate_plugin_load_errors,
                )
            except (ValidationError, ConfigRuntimeValidationError) as exc:
                msg = (
                    f"personal_agents would generate an invalid agent for a new user: {config_validation_summary(exc)}"
                )
                raise ConfigRuntimeValidationError(msg) from exc
        settings = config.personal_rooms
        instance = expansion.instances.get(settings.agent) if settings is not None else None
        if settings is not None and instance is not None:
            # A generated agent serves personal rooms only through its template.
            msg = (
                f"personal_rooms.agent names {settings.agent}, which personal_agents.{instance.template} generates; "
                f"name the template {instance.template} instead"
            )
            raise ConfigRuntimeValidationError(msg)
        config._personal_agent_instances = expansion.instances
        config._personal_agent_call_templates = expansion.call_templates
        config._personal_agent_call_agents = expansion.call_agents
        # Pass two builds every generated agent from pass one, so a creator's own edits never change what it grants.
        generated_data, generated = expand_generated_agents(config, cast("dict[str, Any]", expansion.data), roster)
        if generated_data is not None:
            try:
                config = cls._validate_runtime_data(
                    generated_data,
                    runtime_paths,
                    tolerate_plugin_load_errors=tolerate_plugin_load_errors,
                )
            except (ValidationError, ConfigRuntimeValidationError) as exc:
                if candidate:
                    raise
                # Edits and created agents wait for a fix; the owners keep their agents and loading never breaks.
                logger.warning("generated_agents_invalid", error=str(exc))
            config._personal_agent_instances = expansion.instances
            config._personal_agent_call_templates = expansion.call_templates
            config._personal_agent_call_agents = expansion.call_agents
        config._generated_agents = {name: info for name, info in generated.items() if name in config.agents}
        # authored_model_dump drops generated agents while they match these dumps.
        config._generated_agent_dumps = {
            agent_name: config.agents[agent_name].model_dump(exclude_unset=True)
            for agent_name in config._generated_agents
        }
        config._personal_agents_added_agents_section = (
            bool(config._generated_agents) and isinstance(data, dict) and "agents" not in data
        )
        return config

    @classmethod
    def _validate_runtime_data(
        cls,
        data: object,
        runtime_paths: RuntimePaths,
        *,
        tolerate_plugin_load_errors: bool,
    ) -> Config:
        config = cls.model_validate(data, context={"runtime_paths": runtime_paths})
        config._reject_call_agents_sharing_a_room(lambda room: canonical_room_reference(room, runtime_paths))
        # A sandbox runner runs tool calls, never a model, and its config snapshot carries no models.
        if not runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]):
            config._validate_model_references()
        config._validate_model_extra_kwargs(runtime_paths)
        # A deployment switch, never authored: it gives agents the approved_egress tool and its approval rule.
        config._approved_egress_enabled = runtime_paths.env_flag(_APPROVED_EGRESS_ENABLED_ENV)
        # why-lazy: module-top catalog import pulls runtime tool registry paths and loads agents+tools at config import.
        from mindroom.tool_system.catalog import ToolConfigOverrideError, ToolMetadataValidationError  # noqa: PLC0415

        try:
            config._validate_authored_tool_entries(
                runtime_paths,
                tolerate_plugin_load_errors=tolerate_plugin_load_errors,
            )
        except (PluginValidationError, ToolConfigOverrideError, ToolMetadataValidationError) as exc:
            raise ConfigRuntimeValidationError(str(exc)) from exc
        return config

    def authored_model_dump(self) -> dict[str, Any]:
        """Serialize authored config, without the personal and created agents the roster generates."""
        payload = self.runtime_model_dump()
        agents = payload.get("agents")
        if isinstance(agents, dict):
            for agent_name, info in self._generated_agents.items():
                if agents.get(agent_name) == self._generated_agent_dumps[agent_name]:
                    agents.pop(agent_name)
                elif info.created:
                    # A created agent lives only in the roster, so saving a change made here would lose it.
                    msg = f"agents.{agent_name} was created by another agent; its owners change it with update_agent"
                    raise ConfigRuntimeValidationError(msg)
                # An edited personal agent stays, so saving it collides with its roster entry.
            if not agents and self._personal_agents_added_agents_section:
                payload.pop("agents")
        calls = payload.get("calls")
        if self._personal_agent_call_templates and isinstance(calls, dict):
            # Loading gave each generated agent its template's call profile; the saved config keeps the template key.
            calls["agents"] = {
                agent_name: profile
                for agent_name, profile in calls.get("agents", {}).items()
                if self._personal_agent_call_agents.get(agent_name) != profile
            } | self._personal_agent_call_templates
        return payload

    def runtime_model_dump(self) -> dict[str, Any]:
        """Serialize authored config plus the agents generated from personal_agents templates."""
        payload = cast("dict[str, Any]", self.model_dump(exclude_unset=True))
        knowledge_bases = payload.get("knowledge_bases")
        if isinstance(knowledge_bases, dict):
            for base_id in self._runtime_knowledge_base_overlays:
                knowledge_bases.pop(base_id, None)
            if not knowledge_bases:
                payload.pop("knowledge_bases", None)
        return _strip_empty_root_sections(payload)

    def redacted_authored_model_dump(self) -> dict[str, Any]:
        """Serialize authored config for display, masking schema-marked secrets and credential-named keys."""
        return redact_authored_config(self.authored_model_dump())

    def with_runtime_knowledge_base_overlay(
        self,
        base_id: str,
        base_config: KnowledgeBaseConfig,
    ) -> Config:
        """Return an effective config with one non-authored knowledge base."""
        self._validate_knowledge_base_ids([base_id])
        if base_id in self.knowledge_bases and base_id not in self._runtime_knowledge_base_overlays:
            msg = f"Runtime knowledge base '{base_id}' conflicts with an authored knowledge base"
            raise ValueError(msg)
        config = self.model_copy(
            update={"knowledge_bases": {**self.knowledge_bases, base_id: base_config}},
        )
        config._runtime_knowledge_base_overlays = {
            **self._runtime_knowledge_base_overlays,
            base_id: base_config,
        }
        return config

    def runtime_knowledge_base_overlay(self, base_id: str) -> KnowledgeBaseConfig | None:
        """Return one runtime-only knowledge base overlay, when present."""
        return self._runtime_knowledge_base_overlays.get(base_id)

    def personal_agent_instance(self, agent_name: str) -> PersonalAgentInstance | None:
        """Return the template and owner of one generated agent, or None for an authored agent."""
        return self._personal_agent_instances.get(agent_name)

    def generated_agent(self, agent_name: str) -> GeneratedAgentInfo | None:
        """Return whether one personal or created agent was created and its allowed tools, or None for an authored one."""
        return self._generated_agents.get(agent_name)

    def tool_ceiling(self, agent_name: str) -> tuple[str, ...]:
        """Return the tools this agent's edits, or the agents it creates, may choose from; pass one's if generated."""
        if (info := self._generated_agents.get(agent_name)) is not None:
            return info.allowed_tools
        return tuple(entry.name for entry in authored_tool_entries(self, agent_name))

    def personal_agent_name(self, template: str, user_id: str) -> str | None:
        """Return the agent generated from one template for one user, or None before that user is onboarded."""
        return next(
            (
                agent_name
                for agent_name, instance in self._personal_agent_instances.items()
                if instance.template == template and instance.user_id == user_id
            ),
            None,
        )

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

    def configured_team_name(self, team_name: str) -> str | None:
        """Return ``team_name`` when it names a configured team, else None: an ad hoc team is no configured entity."""
        return team_name if team_name in self.teams else None

    def entity_display_name(self, entity_name: str) -> str:
        """Return the display name of the router or of one configured agent or team."""
        if entity_name == ROUTER_AGENT_NAME:
            return ROUTER_DISPLAY_NAME
        return configured_entity(self, entity_name).display_name

    def resolve_scope(self, entity_name: str | None) -> ResolvedScope:
        """Return what one agent, team, or the router shares with every scope; None is an ad hoc team's defaults."""
        return resolve_scope(self, entity_name)

    def resolve_agent(self, agent_name: str) -> ResolvedAgent:
        """Return what one configured agent gets; an unknown name raises."""
        return resolve_agent(self, agent_name)

    @property
    def approved_egress_enabled(self) -> bool:
        """Whether this deployment gives agents the approved_egress tool, whose calls always need approval."""
        return self._approved_egress_enabled

    def get_model_context_window(self, model_name: str) -> int | None:
        """Return the configured context window for one model name, when known."""
        model_config = self.models.get(model_name)
        return model_config.context_window if model_config and model_config.context_window else None

    def get_worker_grantable_credentials(self) -> frozenset[str]:
        """Return shared credential service names allowed inside isolated workers."""
        configured = self.defaults.worker_grantable_credentials
        if configured is None:
            return DEFAULT_WORKER_GRANTABLE_CREDENTIALS
        return frozenset(configured)

    def _agent_policy_seeds(self) -> dict[str, AgentPolicySeed]:
        """Return the policy seed of every configured agent."""
        return build_agent_policy_seeds(
            {agent_name: PolicyInputs.from_agent(agent) for agent_name, agent in self.agents.items()},
            default_worker_scope=self.defaults.worker_scope,
        )

    def get_agent_policies(self) -> dict[str, ResolvedAgentPolicy]:
        """Return the canonical execution policy of every configured agent."""
        return resolve_agent_policy_index(self._agent_policy_seeds()).policies

    def agent_has_tool_at_execution_scope(
        self,
        agent_name: str,
        tool_name: str,
        execution_scope: WorkerScope | None,
    ) -> bool:
        """Return whether one agent can still reach a tool at an exact execution scope."""
        if agent_name not in self.agents:
            return False
        agent = resolve_agent(self, agent_name)
        return tool_name in agent.tools.tool_names and agent.policy.effective_execution_scope == execution_scope

    def get_private_knowledge_base_agent(self, base_id: str) -> str | None:
        """Return the private agent whose synthetic private knowledge base ``base_id`` names, or None."""
        agent_name = base_id.removeprefix(PRIVATE_KNOWLEDGE_BASE_ID_PREFIX)
        if agent_name == base_id or agent_name not in self.agents:
            return None
        return agent_name if agent_policy(self, agent_name).private_knowledge_base_id == base_id else None

    def get_knowledge_base_config(self, base_id: str) -> KnowledgeBaseConfig:
        """Return one effective knowledge base config, including synthetic private bases."""
        configured = self.knowledge_bases.get(base_id)
        if configured is not None:
            return configured

        agent_name = self.get_private_knowledge_base_agent(base_id)
        private_config = self.get_agent(agent_name).private if agent_name is not None else None
        private_knowledge = private_config.knowledge if private_config is not None else None
        if private_knowledge is None or not private_knowledge.enabled or private_knowledge.path is None:
            msg = f"Knowledge base '{base_id}' is not configured"
            raise ValueError(msg)

        return KnowledgeBaseConfig(
            description=private_knowledge.description,
            path=private_knowledge.path,
            watch=private_knowledge.watch,
            chunk_size=private_knowledge.chunk_size,
            chunk_overlap=private_knowledge.chunk_overlap,
            git=private_knowledge.git,
        )

    def _validate_model_references(self) -> None:
        """Reject a model choice of a configured entity, room, or enabled feature that names no configured model.

        Runtime validation of a model-running process only: a config without agents or teams runs no model.
        """
        references = [(f"agents.{name}.model", agent.model) for name, agent in self.agents.items()]
        references += [(f"teams.{name}.model", team.model) for name, team in self.teams.items()]
        if self.agents or self.teams:
            references.append(("router.model", self.router.model))
        references += [(f"room_models.{room}", model) for room, model in self.room_models.items()]
        references += [
            (f"room_thread_summary_models.{room}", model) for room, model in self.room_thread_summary_models.items()
        ]
        if self.defaults.thread_summary_model is not None:
            references.append(("defaults.thread_summary_model", self.defaults.thread_summary_model))
        if self.voice.enabled:
            references.append(("voice.intelligence.model", self.voice.intelligence.model))
        unknown = [f"{path}: {model!r}" for path, model in references if model not in self.models]
        if unknown:
            available = ", ".join(sorted(self.models)) or "none"
            msg = f"Unknown model for {'; '.join(unknown)}. Configured models: {available}"
            raise ConfigRuntimeValidationError(msg)

    def _validate_model_extra_kwargs(self, runtime_paths: RuntimePaths) -> None:
        """Reject an ``extra_kwargs`` key the route serving its model does not accept, naming the model."""
        # why-lazy: the provider registry imports every provider family's route tables, which config import must not load.
        from mindroom.providers.registry import check_extra_kwargs  # noqa: PLC0415

        for model_name, model_config in self.models.items():
            try:
                check_extra_kwargs(model_config, runtime_paths)
            except ValueError as exc:
                msg = f"models.{model_name}: {exc}"
                raise ConfigRuntimeValidationError(msg) from exc

    def _validate_authored_tool_entry(
        self,
        entry: ToolConfigEntry,
        *,
        config_path_prefix: str,
        tools: ToolRegistry,
    ) -> None:
        """Validate one authored tool entry against the validation snapshot of the tools this config can build."""
        # why-lazy: module-top catalog import pulls runtime tool registry paths and loads agents+tools at config import.
        from mindroom.tool_system.catalog import (  # noqa: PLC0415
            ToolConfigOverrideError,
            validate_authored_tool_entry_overrides,
        )

        tool_validation_snapshot = tools.validation_snapshot
        validation_info = tool_validation_snapshot.get(entry.name)
        if entry.name not in tool_validation_snapshot and not self.is_tool_preset(entry.name):
            if tools.unresolved_plugin_sources:
                logger.warning(
                    "Unknown tool may belong to a plugin whose tool names could not be resolved; "
                    "disabling it for this run (verify the tool name is not a typo)",
                    config_path=config_path_prefix,
                    tool_name=entry.name,
                    unresolved_plugin_sources=sorted(tools.unresolved_plugin_sources),
                )
                return
            msg = f"{config_path_prefix}.{entry.name}: Unknown tool '{entry.name}'."
            raise ToolConfigOverrideError(msg)
        if validation_info is not None and validation_info.unavailable_due_to_plugin_load_error:
            logger.warning(
                "Plugin tool unavailable because plugin failed to load",
                config_path=config_path_prefix,
                tool_name=entry.name,
            )
            return

        validate_authored_tool_entry_overrides(
            entry.name,
            entry.overrides,
            config_path_prefix=config_path_prefix,
            tool_metadata=tool_validation_snapshot,
        )

    def _validate_authored_tool_entries(
        self,
        runtime_paths: RuntimePaths,
        *,
        tolerate_plugin_load_errors: bool,
    ) -> None:
        """Validate authored tool references against the tools this candidate config can build.

        A plugin that fails to load fails validation unless ``tolerate_plugin_load_errors``; then its tools stay
        authored and unavailable.
        """
        # why-lazy: the plugin loader pulls the runtime tool registry, which config import must not load.
        from mindroom.tool_system.plugins import candidate_tool_registry  # noqa: PLC0415

        tools, skipped = candidate_tool_registry(self, runtime_paths)
        if skipped and not tolerate_plugin_load_errors:
            raise skipped[0].error
        entries = [(f"defaults.tools[{index}]", entry) for index, entry in enumerate(self.defaults.tools)] + [
            (f"agents.{agent_name}.tools[{index}]", entry)
            for agent_name, agent_config in self.agents.items()
            for index, entry in enumerate(agent_config.tools)
        ]
        for config_path_prefix, entry in entries:
            self._validate_authored_tool_entry(
                entry,
                config_path_prefix=config_path_prefix,
                tools=tools,
            )

    def get_entities_referencing_tools(self, tool_names: set[str]) -> set[str]:
        """Return agents and teams that depend on any of the given tools."""
        matching_agents = {
            agent_name
            for agent_name in self.agents
            if agent_tool_facts(self, agent_name, policy=agent_policy(self, agent_name)).startup_tool_names & tool_names
        }
        matching_teams = {
            team_name
            for team_name, team_config in self.teams.items()
            if any(agent_name in matching_agents for agent_name in team_config.agents)
        }
        return matching_agents | matching_teams

    def get_agent_delegation_closure(
        self,
        agent_name: str,
        *,
        closures: dict[str, frozenset[str]] | None = None,
    ) -> frozenset[str]:
        """Return one agent plus all agents reachable through transitive delegation."""
        return get_agent_delegation_closure(agent_name, self._agent_policy_seeds(), closures=closures)

    def get_unsupported_team_agents(
        self,
        agent_names: list[str],
        *,
        allow_direct_private_agents: bool = False,
    ) -> dict[str, tuple[str, ...] | None]:
        """Return unsupported team members keyed by agent name.

        Unknown agents map to `None`.
        Supported known agents are omitted.
        Private or transitively private members map to their reachable private targets.
        """
        return get_unsupported_team_agents(
            agent_names,
            self._agent_policy_seeds(),
            allow_direct_private_agents=allow_direct_private_agents,
        )

    def assert_team_agents_supported(
        self,
        agent_names: list[str],
        *,
        team_name: str | None = None,
        allow_direct_private_agents: bool = False,
    ) -> None:
        """Reject unknown or currently unsupported team members."""
        prefix = f"Team '{team_name}'" if team_name is not None else "Team request"
        unsupported_agents = self.get_unsupported_team_agents(
            agent_names,
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

    @classmethod
    def is_tool_preset(cls, tool_name: str) -> bool:
        """Return whether a tool name is a known config preset."""
        return tool_name in TOOL_PRESETS

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
            next_tools = [*TOOL_PRESETS.get(tool_name, ()), *cls.IMPLIED_TOOLS.get(tool_name, ())]
            queue.extend(implied_tool for implied_tool in next_tools if implied_tool not in seen)
        return expanded

    @model_validator(mode="after")
    def validate_agent_automations(self) -> Config:
        """Reject automations an agent lists but cannot run, or that name an unknown model."""
        for automation in self.defaults.automations:
            if automation.model is not None and automation.model not in self.models:
                msg = f"defaults.automations {automation.name!r} uses unknown model {automation.model!r}"
                raise ValueError(msg)
        for agent_name, agent in self.agents.items():
            for automation in agent.automations or []:
                if (reason := automation_block_reason(self, agent_name, automation)) is not None:
                    msg = f"Agent {agent_name!r} {reason}"
                    raise ValueError(msg)
                if automation.model is not None and automation.model not in self.models:
                    msg = f"Agent {agent_name!r} automation {automation.name!r} uses unknown model {automation.model!r}"
                    raise ValueError(msg)
        return self

    def knowledge_graph_agents(self) -> list[str]:
        """Return the agents that build a knowledge graph."""
        return [agent_name for agent_name in self.agents if uses_knowledge_graph(self, agent_name)]

    def uses_file_memory(self) -> bool:
        """Return whether any configured agent uses file-backed memory."""
        if not self.agents:
            return self.memory.backend == "file"
        return any(memory_backend(self, agent_name) == "file" for agent_name in self.agents)

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

    def managed_room_keys(self) -> set[str]:
        """Return configured room keys, excluding Matrix room IDs (`!…`) and aliases (`#…`)."""
        return {room_key for room_key in self.get_all_configured_rooms() if is_managed_room_key(room_key)}

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
        runtime_room_override = resolve_room_thread_mode_override(runtime_paths, room_id)
        if runtime_room_override is not None:
            return runtime_room_override

        if entity_name in self.agents:
            return resolve_agent_thread_mode(self.agents[entity_name], room_id, runtime_paths)

        if entity_name in self.teams:
            member_names = [name for name in self.teams[entity_name].agents if name in self.agents]
        elif entity_name == ROUTER_AGENT_NAME:
            member_names = router_agents_for_room(self.agents, self.teams, room_id, runtime_paths)
        else:
            return "thread"
        member_modes = {resolve_agent_thread_mode(self.agents[name], room_id, runtime_paths) for name in member_names}
        return next(iter(member_modes)) if len(member_modes) == 1 else "thread"

    @model_validator(mode="after")
    def validate_agent_helper_models(self) -> Config:
        """Validate dedicated judgment and skill-review model aliases for opted-in agents."""
        if self.defaults.cheap_model is not None and self.defaults.cheap_model not in self.models:
            msg = f"Unknown defaults.cheap_model: {self.defaults.cheap_model!r}"
            raise ValueError(msg)
        for agent_name, agent in self.agents.items():
            judgments = [
                settings.judgment
                for settings in (agent.participation, agent.mid_turn, agent.cold_cache)
                if settings is not None
            ]
            if agent.contacts is not None:
                judgments.append(agent.contacts.judge)
            for judgment in judgments:
                if isinstance(judgment, LLMJudgmentConfig) and judgment.model not in self.models:
                    msg = f"Unknown judgment model for agent {agent_name!r}: {judgment.model!r}"
                    raise ValueError(msg)
            cheap_model = agent_cheap_model(self, agent_name)
            # An inherited default is checked only where an agent uses it.
            if cheap_model is not None and (agent.cheap_model is not None or agent.cold_cache is not None):
                self._validate_cheap_model(agent_name, agent.model, cheap_model)
            elif agent.cold_cache is not None:
                msg = (
                    f"cold_cache needs a cheap_model for agent {agent_name!r}; "
                    "set agents.<name>.cheap_model or defaults.cheap_model"
                )
                raise ValueError(msg)
            review_model = agent.skill_learning.model
            if review_model is not None and review_model not in self.models:
                msg = f"Unknown skill_learning model for agent {agent_name!r}: {review_model!r}"
                raise ValueError(msg)
        return self

    def _validate_cheap_model(self, agent_name: str, agent_model: str, cheap_model: str) -> None:
        """Keep a cheap-model turn from compacting history the agent model still needs."""
        if cheap_model not in self.models:
            msg = f"Unknown cheap_model for agent {agent_name!r}: {cheap_model!r}"
            raise ValueError(msg)
        if cheap_model == agent_model:
            msg = f"cheap_model must differ from the model of agent {agent_name!r}"
            raise ValueError(msg)
        cheap_window = self.get_model_context_window(cheap_model)
        agent_window = self.get_model_context_window(agent_model)
        # Without a window on both sides, compaction cannot keep the cheap model's turns in bounds.
        if (cheap_window is None) != (agent_window is None) or (
            cheap_window is not None and agent_window is not None and cheap_window < agent_window
        ):
            msg = (
                f"cheap_model {cheap_model!r} for agent {agent_name!r} needs a context window "
                f"at least as large as {agent_model!r}, and both or neither must set one"
            )
            raise ValueError(msg)

    @model_validator(mode="after")
    def validate_knowledge_chunk_context_models(self) -> Config:
        """Validate that every knowledge base's chunk context model is a configured model."""
        for base_id, base in self.knowledge_bases.items():
            if base.chunk_context_model is not None and base.chunk_context_model not in self.models:
                msg = f"Unknown chunk_context_model for knowledge base {base_id!r}: {base.chunk_context_model!r}"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def validate_knowledge_graph(self) -> Config:
        """Validate that agents allowing graph edits have a graph, and the graph's model once any agent enables it."""
        for agent_name, agent in self.agents.items():
            if agent.knowledge_graph_edits and not uses_knowledge_graph(self, agent_name):
                msg = f"Agent {agent_name!r} knowledge_graph_edits needs knowledge_graph: true"
                raise ValueError(msg)
        settings = self.knowledge_graph
        if not self.knowledge_graph_agents():
            return self
        if settings.model is None:
            msg = "knowledge_graph.model is required when an agent enables knowledge_graph"
            raise ValueError(msg)
        if settings.model not in self.models:
            msg = f"Unknown knowledge_graph model: {settings.model!r}"
            raise ValueError(msg)
        return self


def _tag_automation_unions(node: Any) -> None:  # noqa: ANN401 - a JSON schema node
    """Mark each automation union as chosen by ``name``, which the built-ins pin and plugin automations leave free."""
    if isinstance(node, dict):
        branches = node.get("oneOf")
        if isinstance(branches, list) and {"$ref": "#/$defs/PluginAutomation"} in branches:
            node["discriminator"] = {"propertyName": "name"}
        for value in node.values():
            _tag_automation_unions(value)
    elif isinstance(node, list):
        for value in node:
            _tag_automation_unions(value)


def _describe_automation_union(schema: dict[str, Any]) -> None:
    """Shape the automation union for the dashboard's form: a pinned name per built-in and a free one for plugins."""
    defs = schema["$defs"]
    for built_in in ("PromptCurationAutomation", "DreamingAutomation", "ExplorationAutomation"):
        # The form fills required fields when a variant is picked, and the name is what selects the variant.
        defs[built_in]["required"] = ["name", *defs[built_in].get("required", [])]
    plugin = defs["PluginAutomation"]
    plugin["title"] = "Plugin automation"
    # Validation errors for a plugin entry are reported under this tag, since its name is not fixed.
    plugin.setdefault("x-mindroom", {})["union_tag"] = "plugin"
    _tag_automation_unions(schema)


@cache
def dashboard_config_schema() -> dict[str, Any]:
    """Return the Config JSON schema with dashboard hints and default-factory values."""
    schema = Config.model_json_schema(schema_generator=DashboardJsonSchema)
    _describe_automation_union(schema)
    return schema


def redact_authored_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact one authored config payload for display, letting the config schema decide for typed fields."""
    return cast("dict[str, Any]", redact_config_for_display(payload, dashboard_config_schema()))


def failed_config_source_fingerprint(exc: BaseException) -> str | None:
    """Read optional source metadata this loader attaches after parsing succeeds."""
    fingerprint = getattr(exc, "config_source_fingerprint", None)
    return fingerprint if isinstance(fingerprint, str) else None


def validate_loaded_config_source(
    data: dict[str, Any],
    source_digests: dict[Path, str],
    original: bytes,
    runtime_paths: RuntimePaths,
    *,
    uses_includes: bool,
    tolerate_plugin_load_errors: bool = False,
) -> Config:
    """Validate and, when needed, persist one already-parsed config source."""
    path = runtime_paths.config_path
    source_files = frozenset(source_digests)

    try:
        validate_access_migration_source(data, uses_includes=uses_includes)
        migration = migrate_access_config_data(data)
        config = Config.validate_with_runtime(
            migration.data,
            runtime_paths,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
        )
        if migration.changed:
            # Config commits write under this lock, so the migration's compare-and-replace cannot lose one.
            with advisory_file_lock(personal_agent_roster_lock_path(runtime_paths)):
                persisted = persist_access_migration(path, original, migration.data)
            source_digests = {path.resolve(): hashlib.sha256(persisted).hexdigest()}
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        # Parsing succeeded, so the full file set is known; expose it the same
        # way as parse-time failures so reload watchers keep covering it.
        attach_partial_source_files(exc, source_files, uses_includes=uses_includes)
        exc.config_source_fingerprint = source_files_fingerprint(path, source_digests)  # ty: ignore[invalid-assignment]
        raise
    config.record_source_metadata(path, source_digests, uses_includes=uses_includes)
    return config


def load_config(
    runtime_paths: RuntimePaths,
    *,
    tolerate_plugin_load_errors: bool = False,
) -> Config:
    """Load and validate one config against an explicit runtime context."""
    path = runtime_paths.config_path
    if not path.exists():
        msg = f"Agent configuration file not found: {path}"
        raise FileNotFoundError(msg)

    original = path.read_bytes()
    data, source_digests, uses_includes = load_yaml_config_source_with_digests(path, source=original)
    config = validate_loaded_config_source(
        data,
        source_digests,
        original,
        runtime_paths,
        uses_includes=uses_includes,
        tolerate_plugin_load_errors=tolerate_plugin_load_errors,
    )
    logger.info("loaded_agent_configuration", path=str(path), source_file_count=len(config.source_files))
    logger.info("loaded_agent_configuration_count", agent_count=len(config.agents))
    return config


def load_config_or_user_error(
    runtime_paths: RuntimePaths,
    *,
    footer: str | None = None,
    tolerate_plugin_load_errors: bool = False,
) -> tuple[Config | None, str | None]:
    """Load config or return one shared user-facing invalid-configuration message."""
    try:
        return load_config(
            runtime_paths,
            tolerate_plugin_load_errors=tolerate_plugin_load_errors,
        ), None
    except CONFIG_LOAD_USER_ERROR_TYPES as exc:
        return None, format_invalid_config_message(exc, footer=footer)
