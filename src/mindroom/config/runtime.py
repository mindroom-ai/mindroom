"""Explicit runtime validation and resolution for authored configuration."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml

from mindroom.agent_policy import (
    build_agent_policy_seeds,
    resolve_agent_policy_from_data,
    resolve_private_knowledge_base_agent,
)
from mindroom.agent_policy import (
    get_agent_delegation_closure as resolve_agent_delegation_closure,
)
from mindroom.agent_policy import (
    get_unsupported_team_agents as resolve_unsupported_team_agents,
)
from mindroom.agent_policy import (
    unsupported_team_agent_message as format_unsupported_team_agent_message,
)
from mindroom.config.entity_view import ResolvedEntityView, ResolvedRuntimeModel
from mindroom.config.errors import ConfigRuntimeValidationError
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.models import CompactionConfig, EffectiveToolConfig, ToolConfigEntry
from mindroom.config.runtime_overlays import (
    apply_runtime_approved_egress_overlay,
    strip_runtime_approved_egress_overlay_from_dump,
)
from mindroom.config.validation import relative_paths_overlap
from mindroom.constants import (
    DEFAULT_WORKER_GRANTABLE_CREDENTIALS,
    ROUTER_AGENT_NAME,
    config_relative_path,
    matrix_state_file,
    resolve_config_relative_path,
    runtime_matrix_homeserver,
)
from mindroom.git_urls import credential_free_repo_url
from mindroom.history.types import HistoryPolicy, ResolvedHistorySettings
from mindroom.logging_config import get_logger
from mindroom.matrix_identifiers import (
    extract_server_name_from_homeserver,
    managed_room_alias_localpart,
    managed_space_alias_localpart,
)
from mindroom.room_thread_modes import resolve_room_thread_mode_override
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY
from mindroom.thread_models import resolve_thread_model_override
from mindroom.tool_system.catalog import (
    ToolConfigOverrideError,
    ToolMetadataValidationError,
    ToolValidationInfo,
    apply_authored_overrides,
    authored_tool_overrides_to_runtime,
    bind_resolved_tool_state_cache,
    resolved_tool_runtime_state_for_runtime,
    validate_authored_tool_entry_overrides,
)
from mindroom.tool_system.plugin_imports import PluginValidationError
from mindroom.tool_system.worker_routing import unsupported_shared_only_integration_names
from mindroom.workspaces import validate_workspace_template_dir

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config, RuntimeConfig
    from mindroom.config.memory import MemoryBackend, MemorySearchConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import WorkerScope

logger = get_logger(__name__)


@dataclass(frozen=True)
class _KnowledgeBaseSourceSemantics:
    """Source ownership semantics that must match for exact duplicate roots."""

    git_enabled: bool
    git_repo_identity: str
    git_branch: str
    git_credentials_service: str | None
    git_lfs: bool


def _knowledge_base_source_semantics(base_config: KnowledgeBaseConfig) -> _KnowledgeBaseSourceSemantics:
    git_config = base_config.git
    return _KnowledgeBaseSourceSemantics(
        git_enabled=git_config is not None,
        git_repo_identity=credential_free_repo_url(git_config.repo_url) if git_config is not None else "",
        git_branch=git_config.branch if git_config is not None else "",
        git_credentials_service=git_config.credentials_service if git_config is not None else None,
        git_lfs=git_config.lfs if git_config is not None else False,
    )


def _persisted_entity_account_usernames(runtime_paths: RuntimePaths) -> dict[str, str]:
    state_file = matrix_state_file(runtime_paths=runtime_paths)
    if not state_file.exists():
        return {}
    data = yaml.safe_load(state_file.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return {}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    usernames: dict[str, str] = {}
    for account_key, account in accounts.items():
        if not isinstance(account_key, str) or not account_key.startswith("agent_"):
            continue
        if not isinstance(account, dict):
            continue
        username = account.get("username")
        if isinstance(username, str) and username:
            usernames[account_key] = username
    return usernames


def _template_contains_overlapping_subtree(template_dir: Path, target_path: Path) -> bool:
    if not template_dir.is_dir():
        return False
    return any(
        relative_paths_overlap(source_path.relative_to(template_dir), target_path)
        for source_path in template_dir.rglob("*")
    )


def _skip_private_template_dir_validation(runtime_paths: RuntimePaths) -> bool:
    return runtime_paths.env_flag(SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]) and bool(
        runtime_paths.env_value(SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"], default=""),
    )


def _validate_knowledge_base_paths(config: Config, runtime_paths: RuntimePaths) -> None:
    resolved_paths = [
        (base_id, resolve_config_relative_path(base_config.path, runtime_paths).resolve())
        for base_id, base_config in config.knowledge_bases.items()
    ]
    for index, (base_id, root) in enumerate(resolved_paths):
        for other_base_id, other_root in resolved_paths[index + 1 :]:
            if root == other_root:
                semantics = _knowledge_base_source_semantics(config.knowledge_bases[base_id])
                other_semantics = _knowledge_base_source_semantics(config.knowledge_bases[other_base_id])
                if semantics != other_semantics:
                    msg = (
                        "knowledge_bases exact duplicate aliases must use compatible source configuration; "
                        f"'{base_id}' and '{other_base_id}' both resolve to '{root}'"
                    )
                    raise ValueError(msg)
                continue
            if root.is_relative_to(other_root) or other_root.is_relative_to(root):
                msg = (
                    "knowledge_bases paths must not overlap unless they are exact duplicate aliases; "
                    f"'{base_id}' resolves to '{root}' and '{other_base_id}' resolves to '{other_root}'"
                )
                raise ValueError(msg)


def _validate_private_knowledge_runtime_paths(config: Config, runtime_paths: RuntimePaths) -> None:
    for agent_name, agent_config in config.agents.items():
        private_config = agent_config.private
        if private_config is None or private_config.knowledge is None:
            continue
        private_knowledge = private_config.knowledge
        if private_knowledge.git is None or private_knowledge.path is None or private_config.template_dir is None:
            continue
        template_dir = config_relative_path(private_config.template_dir, runtime_paths)
        if _template_contains_overlapping_subtree(template_dir, Path(private_knowledge.path)):
            msg = (
                f"Agent '{agent_name}' uses git-backed private knowledge at '{private_knowledge.path}', "
                "but git-backed private knowledge must use a dedicated subtree outside the private root "
                "and outside scaffolded private workspace content"
            )
            raise ValueError(msg)


def _validate_private_template_dirs(config: Config, runtime_paths: RuntimePaths) -> None:
    if _skip_private_template_dir_validation(runtime_paths):
        return
    for agent_name, agent_config in config.agents.items():
        private_config = agent_config.private
        if private_config is None or private_config.template_dir is None:
            continue
        template_dir = config_relative_path(private_config.template_dir, runtime_paths)
        try:
            validate_workspace_template_dir(template_dir)
        except ValueError as exc:
            msg = f"Agent '{agent_name}' has invalid private.template_dir: {exc}"
            raise ValueError(msg) from exc


def _validate_internal_user_username(config: Config, runtime_paths: RuntimePaths) -> None:
    if config.mindroom_user is None:
        return
    persisted_usernames = _persisted_entity_account_usernames(runtime_paths)
    reserved_localparts: dict[str, str] = {}
    for entity_name in [ROUTER_AGENT_NAME, *config.agents, *config.teams]:
        persisted_username = persisted_usernames.get(f"agent_{entity_name}")
        if persisted_username is None:
            continue
        if entity_name == ROUTER_AGENT_NAME:
            label = f"router '{ROUTER_AGENT_NAME}'"
        elif entity_name in config.agents:
            label = f"agent '{entity_name}'"
        else:
            label = f"team '{entity_name}'"
        reserved_localparts[persisted_username] = label
    conflict = reserved_localparts.get(config.mindroom_user.username)
    if conflict:
        msg = f"mindroom_user.username '{config.mindroom_user.username}' conflicts with {conflict} Matrix localpart"
        raise ValueError(msg)


def _validate_root_space_alias(config: Config, runtime_paths: RuntimePaths) -> None:
    if not config.matrix_space.enabled:
        return
    reserved_alias_localpart = managed_space_alias_localpart(runtime_paths=runtime_paths)
    colliding_rooms = sorted(
        room_key
        for room_key in config.get_all_configured_rooms()
        if not room_key.startswith(("!", "#"))
        and managed_room_alias_localpart(room_key, runtime_paths=runtime_paths) == reserved_alias_localpart
    )
    if colliding_rooms:
        formatted = ", ".join(colliding_rooms)
        msg = f"Managed room keys conflict with the reserved root Space alias '{reserved_alias_localpart}': {formatted}"
        raise ValueError(msg)


def _validate_runtime_paths(config: Config, runtime_paths: RuntimePaths) -> None:
    _validate_knowledge_base_paths(config, runtime_paths)
    _validate_private_knowledge_runtime_paths(config, runtime_paths)
    _validate_private_template_dirs(config, runtime_paths)
    _validate_internal_user_username(config, runtime_paths)
    _validate_root_space_alias(config, runtime_paths)


def _validate_authored_tool_entry(
    config: Config,
    entry: ToolConfigEntry,
    *,
    config_path_prefix: str,
    tool_validation_snapshot: Mapping[str, ToolValidationInfo],
) -> None:
    validation_info = tool_validation_snapshot.get(entry.name)
    if validation_info is None and entry.name not in config.TOOL_PRESETS:
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
    config: Config,
    tool_validation_snapshot: Mapping[str, ToolValidationInfo],
) -> None:
    for index, entry in enumerate(config.defaults.tools):
        _validate_authored_tool_entry(
            config,
            entry,
            config_path_prefix=f"defaults.tools[{index}]",
            tool_validation_snapshot=tool_validation_snapshot,
        )
    for agent_name, agent_config in config.agents.items():
        for index, entry in enumerate(agent_config.tools):
            _validate_authored_tool_entry(
                config,
                entry,
                config_path_prefix=f"agents.{agent_name}.tools[{index}]",
                tool_validation_snapshot=tool_validation_snapshot,
            )


def build_runtime_config(
    authored: Config,
    runtime_paths: RuntimePaths,
    *,
    runtime_config_type: type[RuntimeConfig],
    tolerate_plugin_load_errors: bool = False,
    source_files: frozenset[Path] = frozenset(),
    tool_validation_snapshot: Mapping[str, ToolValidationInfo] | None = None,
    plugin_oauth_providers: tuple[object, ...] | None = None,
) -> RuntimeConfig:
    """Validate one authored config against its explicit runtime context."""
    overlay = apply_runtime_approved_egress_overlay(authored.authored_model_dump(), runtime_paths)
    effective_authored = type(authored).model_validate(overlay.data)
    try:
        _validate_runtime_paths(effective_authored, runtime_paths)
    except ValueError as exc:
        raise ConfigRuntimeValidationError(str(exc)) from exc
    resolved_snapshot = tool_validation_snapshot
    resolved_tool_state = None
    try:
        if resolved_snapshot is None:
            resolved_tool_state = resolved_tool_runtime_state_for_runtime(
                runtime_paths,
                effective_authored,
                tolerate_plugin_load_errors=tolerate_plugin_load_errors,
            )
            resolved_snapshot = resolved_tool_state.validation_snapshot
        _validate_authored_tool_entries(effective_authored, resolved_snapshot)
    except (PluginValidationError, ToolConfigOverrideError, ToolMetadataValidationError) as exc:
        raise ConfigRuntimeValidationError(str(exc)) from exc

    unavailable_plugin_tool_names = frozenset(
        tool_name
        for tool_name, validation_info in resolved_snapshot.items()
        if validation_info.unavailable_due_to_plugin_load_error
    )
    runtime_config = runtime_config_type.model_validate(
        {
            **effective_authored.authored_model_dump(),
            "runtime_paths": runtime_paths,
            "source_files": source_files,
            "runtime_plugin_oauth_providers": plugin_oauth_providers,
            "unavailable_plugin_tool_names": unavailable_plugin_tool_names,
            "agent_tool_runtime_overrides": tuple(
                (
                    agent_name,
                    _resolve_agent_tool_runtime_overrides(
                        effective_authored,
                        agent_name,
                        resolved_snapshot,
                    ),
                )
                for agent_name in effective_authored.agents
            ),
            "runtime_approved_egress_injected_default_tool": overlay.injected_default_tool,
            "runtime_approved_egress_injected_approval_rule": overlay.injected_approval_rule,
        },
    )
    if resolved_tool_state is not None:
        bind_resolved_tool_state_cache(resolved_tool_state, runtime_config)
    try:
        _validate_shared_only_integration_assignments(runtime_config)
    except ValueError as exc:
        raise ConfigRuntimeValidationError(str(exc)) from exc
    return runtime_config


def strip_runtime_overlay_from_dump(config: RuntimeConfig, payload: dict[str, Any]) -> dict[str, Any]:
    """Remove runtime-only overlay entries from authored serialization."""
    return strip_runtime_approved_egress_overlay_from_dump(
        payload,
        injected_default_tool=config.runtime_approved_egress_injected_default_tool,
        injected_approval_rule=config.runtime_approved_egress_injected_approval_rule,
    )


def expand_tool_names(config: Config, tool_names: list[str]) -> list[str]:
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
        next_tools = list(config.TOOL_PRESETS.get(tool_name, ()))
        next_tools.extend(config.IMPLIED_TOOLS.get(tool_name, ()))
        queue.extend(implied_tool for implied_tool in next_tools if implied_tool not in seen)
    return expanded


def _tool_name_is_available(config: RuntimeConfig, tool_name: str) -> bool:
    return tool_name not in config.unavailable_plugin_tool_names


def _agent_authored_tool_configs(
    config: RuntimeConfig,
    agent_name: str,
    *,
    include_unavailable: bool = False,
) -> list[EffectiveToolConfig]:
    agent_config = config.get_agent(agent_name)
    default_entries_by_name = {entry.name: entry for entry in config.defaults.tools}
    agent_entry_names = {entry.name for entry in agent_config.tools}
    effective_entries: list[EffectiveToolConfig] = []

    def append(entry: ToolConfigEntry, base_overrides: dict[str, object] | None = None) -> None:
        if not include_unavailable and not _tool_name_is_available(config, entry.name):
            return
        effective_entries.append(
            EffectiveToolConfig(
                name=entry.name,
                tool_config_overrides=apply_authored_overrides(base_overrides or {}, entry.overrides),
                defer=entry.defer,
                initial=entry.initial,
                authored_order=len(effective_entries),
                authored_name=entry.name,
            ),
        )

    if agent_config.include_default_tools:
        for entry in agent_config.tools:
            default_entry = default_entries_by_name.get(entry.name)
            base_overrides = apply_authored_overrides({}, default_entry.overrides) if default_entry else {}
            append(entry, base_overrides)
        for entry in config.defaults.tools:
            if entry.name not in agent_entry_names:
                append(entry)
    else:
        for entry in agent_config.tools:
            append(entry)
    return effective_entries


def _agent_tool_configs(config: RuntimeConfig, agent_name: str) -> list[EffectiveToolConfig]:
    effective_entries: list[EffectiveToolConfig] = []
    for authored_entry in _agent_authored_tool_configs(config, agent_name):
        for tool_name in expand_tool_names(config, [authored_entry.name]):
            if not _tool_name_is_available(config, tool_name):
                continue
            effective_entries.append(
                EffectiveToolConfig(
                    name=tool_name,
                    tool_config_overrides=(
                        dict(authored_entry.tool_config_overrides) if tool_name == authored_entry.name else {}
                    ),
                    defer=authored_entry.defer,
                    initial=authored_entry.initial,
                    authored_order=authored_entry.authored_order,
                    authored_name=authored_entry.name,
                ),
            )
    return effective_entries


def _agent_available_tools(config: RuntimeConfig, agent_name: str) -> list[str]:
    agent_config = config.get_agent(agent_name)
    explicit_names = [name for name in agent_config.tool_names if _tool_name_is_available(config, name)]
    if agent_config.include_default_tools:
        explicit_names.extend(name for name in config.defaults.tool_names if _tool_name_is_available(config, name))
    return expand_tool_names(config, explicit_names)


def _agent_authored_deferred_tool_configs(config: RuntimeConfig, agent_name: str) -> list[EffectiveToolConfig]:
    return [
        EffectiveToolConfig(
            name=entry.name,
            tool_config_overrides=dict(entry.tool_config_overrides),
            defer=entry.defer,
            initial=entry.initial,
            authored_order=entry.authored_order,
            authored_name=entry.name,
        )
        for entry in _agent_authored_tool_configs(config, agent_name)
        if entry.defer and _tool_name_is_available(config, entry.name)
    ]


def _agent_execution_scope(config: Config, agent_name: str) -> WorkerScope | None:
    return resolve_agent_policy_from_data(
        agent_name,
        config.get_agent(agent_name),
        default_worker_scope=config.defaults.worker_scope,
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    ).effective_execution_scope


def _agent_scope_label(config: Config, agent_name: str) -> str:
    return resolve_agent_policy_from_data(
        agent_name,
        config.get_agent(agent_name),
        default_worker_scope=config.defaults.worker_scope,
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    ).scope_label


def _agent_private_knowledge_base_id(config: Config, agent_name: str) -> str | None:
    return resolve_agent_policy_from_data(
        agent_name,
        config.get_agent(agent_name),
        default_worker_scope=config.defaults.worker_scope,
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    ).private_knowledge_base_id


def get_private_knowledge_base_agent(config: Config, base_id: str) -> str | None:
    """Return the owning agent for a synthetic private knowledge base ID."""
    return resolve_private_knowledge_base_agent(
        base_id,
        build_agent_policy_seeds(
            config.agents,
            default_worker_scope=config.defaults.worker_scope,
        ),
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    )


def get_knowledge_base_config(config: Config, base_id: str) -> KnowledgeBaseConfig:
    """Return one effective knowledge base config, including synthetic private bases."""
    configured = config.knowledge_bases.get(base_id)
    if configured is not None:
        return configured
    agent_name = get_private_knowledge_base_agent(config, base_id)
    if agent_name is None:
        msg = f"Knowledge base '{base_id}' is not configured"
        raise ValueError(msg)
    private_config = config.get_agent(agent_name).private
    if private_config is None or private_config.knowledge is None or not private_config.knowledge.enabled:
        msg = f"Knowledge base '{base_id}' is not configured"
        raise ValueError(msg)
    private_knowledge = private_config.knowledge
    if private_knowledge.path is None:
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


def _history_policy_from_limits(
    *,
    num_history_runs: int | None,
    num_history_messages: int | None,
) -> HistoryPolicy:
    if num_history_messages is not None:
        return HistoryPolicy(mode="messages", limit=num_history_messages)
    if num_history_runs is not None:
        return HistoryPolicy(mode="runs", limit=num_history_runs)
    return HistoryPolicy(mode="all")


def _history_settings(config: Config, entity_name: str | None) -> ResolvedHistorySettings:
    if entity_name is None:
        entity = None
    elif entity_name in config.agents:
        entity = config.get_agent(entity_name)
    else:
        entity = config.teams[entity_name]
    num_history_runs = entity.num_history_runs if entity is not None else None
    num_history_messages = entity.num_history_messages if entity is not None else None
    if num_history_runs is None and num_history_messages is None:
        num_history_runs = config.defaults.num_history_runs
        num_history_messages = config.defaults.num_history_messages
    max_tool_calls = (
        entity.max_tool_calls_from_history
        if entity is not None and entity.max_tool_calls_from_history is not None
        else config.defaults.max_tool_calls_from_history
    )
    return ResolvedHistorySettings(
        policy=_history_policy_from_limits(
            num_history_runs=num_history_runs,
            num_history_messages=num_history_messages,
        ),
        max_tool_calls_from_history=max_tool_calls,
        system_message_role="system",
    )


def _effective_compaction_enabled(
    *,
    defaults_enabled: bool,
    override_enabled: bool | None,
    override_fields_set: set[str],
    model_is_explicitly_cleared: bool,
) -> bool:
    if "enabled" in override_fields_set:
        return override_enabled is True
    if model_is_explicitly_cleared and override_fields_set == {"model"}:
        return defaults_enabled
    if override_fields_set:
        return True
    return defaults_enabled


def _compaction_config(config: Config, entity_name: str | None) -> CompactionConfig:
    base = config.defaults.compaction
    defaults_enabled = base.enabled if base is not None else False
    merged = base.model_dump() if base is not None else {}
    if entity_name is None:
        return CompactionConfig.model_validate(merged)
    override = (
        config.get_agent(entity_name).compaction
        if entity_name in config.agents
        else config.teams[entity_name].compaction
    )
    if override is None:
        return CompactionConfig.model_validate(merged)
    authored_override = override.model_dump(exclude_unset=True)
    explicit_enabled = authored_override.pop(
        "enabled",
        override.enabled if "enabled" in override.model_fields_set else None,
    )
    for field_name, field_value in authored_override.items():
        if field_value is None:
            merged.pop(field_name, None)
        else:
            merged[field_name] = field_value
    if authored_override.get("threshold_tokens") is not None:
        merged.pop("threshold_percent", None)
    if authored_override.get("threshold_percent") is not None:
        merged.pop("threshold_tokens", None)
    merged["enabled"] = _effective_compaction_enabled(
        defaults_enabled=defaults_enabled,
        override_enabled=explicit_enabled,
        override_fields_set=override.model_fields_set,
        model_is_explicitly_cleared="model" in override.model_fields_set and override.model is None,
    )
    return CompactionConfig.model_validate(merged)


def _entity_model_name(config: Config, entity_name: str) -> str:
    if entity_name == ROUTER_AGENT_NAME:
        return config.router.model
    if entity_name in config.teams:
        model = config.teams[entity_name].model
        if model is None:
            msg = f"Team {entity_name} has no model configured"
            raise ValueError(msg)
        return model
    return config.get_agent(entity_name).model


def _agent_memory_backend(config: Config, agent_name: str) -> MemoryBackend:
    agent_config = config.get_agent(agent_name)
    return agent_config.memory_backend or config.memory.backend


def _agent_memory_search(config: Config, agent_name: str) -> MemorySearchConfig:
    override = config.get_agent(agent_name).memory_search
    if override is None:
        return config.memory.search.model_copy(deep=True)
    return config.memory.search.model_copy(update=override.model_dump(exclude_none=True), deep=True)


def _agent_culture(config: Config, agent_name: str) -> tuple[str, Any] | None:
    for culture_name, culture_config in config.cultures.items():
        if agent_name in culture_config.agents:
            return culture_name, culture_config.model_copy(deep=True)
    return None


def _resolve_agent_tool_runtime_overrides(
    config: Config,
    agent_name: str,
    tool_validation_snapshot: Mapping[str, ToolValidationInfo],
) -> tuple[tuple[str, tuple[tuple[str, object], ...]], ...]:
    resolved: list[tuple[str, tuple[tuple[str, object], ...]]] = []
    for entry in config.get_agent(agent_name).tools:
        validation_info = tool_validation_snapshot.get(entry.name)
        if validation_info is None or validation_info.unavailable_due_to_plugin_load_error:
            continue
        runtime_field_names = {field.name for field in validation_info.agent_override_fields}
        runtime_overrides = authored_tool_overrides_to_runtime(
            entry.name,
            {name: value for name, value in entry.overrides.items() if name in runtime_field_names},
            tool_metadata=tool_validation_snapshot,
        )
        if runtime_overrides:
            resolved.append((entry.name, tuple(runtime_overrides.items())))
    return tuple(resolved)


def _tool_runtime_overrides(
    config: RuntimeConfig,
    agent_name: str,
) -> tuple[tuple[str, tuple[tuple[str, object], ...]], ...]:
    return next(
        (
            overrides
            for configured_agent, overrides in config.agent_tool_runtime_overrides
            if configured_agent == agent_name
        ),
        (),
    )


def _deferred_scope_incompatible_tools(
    config: RuntimeConfig,
    agent_name: str,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    execution_scope = _agent_execution_scope(config, agent_name)
    return tuple(
        (
            entry.name,
            tuple(unsupported_shared_only_integration_names(expand_tool_names(config, [entry.name]), execution_scope)),
        )
        for entry in _agent_authored_deferred_tool_configs(config, agent_name)
    )


def _entity_kind(config: Config, entity_name: str | None) -> Literal["defaults", "agent", "team", "router"]:
    if entity_name is None:
        return "defaults"
    if entity_name in config.agents:
        return "agent"
    if entity_name in config.teams:
        return "team"
    if entity_name == ROUTER_AGENT_NAME:
        return "router"
    available = sorted({*config.agents, *config.teams, ROUTER_AGENT_NAME})
    msg = f"Unknown entity: {entity_name}. Available entities: {', '.join(available)}"
    raise ValueError(msg)


def resolve_entity(config: RuntimeConfig, entity_name: str | None) -> ResolvedEntityView:
    """Materialize all effective values for one validated entity name."""
    kind = _entity_kind(config, entity_name)
    is_history_scope = kind in {"defaults", "agent", "team"}
    is_agent = kind == "agent"
    agent_name = entity_name if is_agent else None
    authored_tools = _agent_authored_tool_configs(config, agent_name) if agent_name is not None else None
    deferred_tools = _agent_authored_deferred_tool_configs(config, agent_name) if agent_name is not None else None
    private_base_id = _agent_private_knowledge_base_id(config, agent_name) if agent_name is not None else None
    knowledge_base_ids = None
    if agent_name is not None:
        knowledge_base_ids = [*config.get_agent(agent_name).knowledge_bases]
        if private_base_id is not None:
            knowledge_base_ids.append(private_base_id)
    model_name = None
    if entity_name is not None:
        model_name = config.teams[entity_name].model if kind == "team" else _entity_model_name(config, entity_name)
    return ResolvedEntityView(
        name=entity_name,
        _kind=kind,
        _history_settings=_history_settings(config, entity_name) if is_history_scope else None,
        _compaction_config=_compaction_config(config, entity_name) if is_history_scope else None,
        _has_authored_compaction_config=(
            config.defaults.compaction is not None
            or (
                entity_name is not None
                and (
                    config.get_agent(entity_name).compaction
                    if kind == "agent"
                    else config.teams[entity_name].compaction
                )
                is not None
            )
            if is_history_scope
            else None
        ),
        memory_backend=_agent_memory_backend(config, agent_name) if agent_name is not None else config.memory.backend,
        _memory_search=_agent_memory_search(config, agent_name)
        if agent_name is not None
        else config.memory.search.model_copy(deep=True),
        _model_name=model_name,
        _available_tools=tuple(_agent_available_tools(config, agent_name)) if agent_name is not None else None,
        _tool_configs=tuple(deepcopy(_agent_tool_configs(config, agent_name))) if agent_name is not None else None,
        _authored_tool_configs=tuple(deepcopy(authored_tools)) if authored_tools is not None else None,
        _authored_deferred_tool_configs=tuple(deepcopy(deferred_tools)) if deferred_tools is not None else None,
        _tool_runtime_overrides=deepcopy(_tool_runtime_overrides(config, agent_name))
        if agent_name is not None
        else None,
        _deferred_scope_incompatible_tools=(
            _deferred_scope_incompatible_tools(config, agent_name) if agent_name is not None else None
        ),
        _culture=_agent_culture(config, agent_name) if agent_name is not None else None,
        _knowledge_base_ids=tuple(knowledge_base_ids) if knowledge_base_ids is not None else None,
        _private_knowledge_base_id=private_base_id,
        _execution_scope=_agent_execution_scope(config, agent_name) if agent_name is not None else None,
        _scope_label=_agent_scope_label(config, agent_name) if agent_name is not None else None,
    )


def _validate_shared_only_integration_assignments(config: RuntimeConfig) -> None:
    invalid_assignments: list[str] = []
    for agent_name in sorted(config.agents):
        scope_label = _agent_scope_label(config, agent_name)
        execution_scope = _agent_execution_scope(config, agent_name)
        eager_tools: list[str] = []
        for entry in _agent_authored_tool_configs(config, agent_name, include_unavailable=True):
            if not entry.defer:
                eager_tools.extend(expand_tool_names(config, [entry.name]))
        invalid_assignments.extend(
            f"{agent_name} -> {tool_name} ({scope_label})"
            for tool_name in unsupported_shared_only_integration_names(eager_tools, execution_scope)
        )
        for entry in _agent_authored_tool_configs(config, agent_name, include_unavailable=True):
            if not entry.defer:
                continue
            incompatible = unsupported_shared_only_integration_names(
                expand_tool_names(config, [entry.name]),
                execution_scope,
            )
            invalid_assignments.extend(
                f"{agent_name} -> deferred tool '{entry.name}' -> {tool_name} ({scope_label})"
                for tool_name in incompatible
            )
    if invalid_assignments:
        msg = (
            "Shared-only integrations are supported only for unscoped agents or worker_scope=shared. "
            f"Invalid assignments: {', '.join(invalid_assignments)}"
        )
        raise ValueError(msg)


def get_worker_grantable_credentials(config: Config) -> frozenset[str]:
    """Return credential service names allowed inside isolated workers."""
    configured = config.defaults.worker_grantable_credentials
    return DEFAULT_WORKER_GRANTABLE_CREDENTIALS if configured is None else frozenset(configured)


def uses_file_memory(config: RuntimeConfig) -> bool:
    """Return whether any configured agent uses file-backed memory."""
    if not config.agents:
        return config.memory.backend == "file"
    return any(_agent_memory_backend(config, agent_name) == "file" for agent_name in config.agents)


def get_entities_referencing_tools(config: RuntimeConfig, tool_names: set[str]) -> set[str]:
    """Return agents and teams with hard dependencies on the given tools."""
    matching_agents = {
        agent_name
        for agent_name in config.agents
        if {entry.name for entry in _agent_tool_configs(config, agent_name) if not entry.defer or entry.initial}
        & tool_names
    }
    return matching_agents | {
        team_name
        for team_name, team_config in config.teams.items()
        if any(agent_name in matching_agents for agent_name in team_config.agents)
    }


def get_agent_delegation_closure(
    config: Config,
    agent_name: str,
    *,
    closures: dict[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    """Return one agent plus all agents reachable through delegation."""
    return resolve_agent_delegation_closure(
        agent_name,
        build_agent_policy_seeds(config.agents, default_worker_scope=config.defaults.worker_scope),
        closures=closures,
    )


def get_unsupported_team_agents(
    config: Config,
    agent_names: list[str],
    *,
    closures: dict[str, frozenset[str]] | None = None,
    allow_direct_private_agents: bool = False,
) -> dict[str, tuple[str, ...] | None]:
    """Return unknown or private team members keyed by agent name."""
    return resolve_unsupported_team_agents(
        agent_names,
        build_agent_policy_seeds(config.agents, default_worker_scope=config.defaults.worker_scope),
        closures=closures,
        allow_direct_private_agents=allow_direct_private_agents,
    )


def assert_team_agents_supported(
    config: Config,
    agent_names: list[str],
    *,
    team_name: str | None = None,
    allow_direct_private_agents: bool = False,
) -> None:
    """Reject unknown or currently unsupported team members."""
    prefix = f"Team '{team_name}'" if team_name is not None else "Team request"
    unsupported_agents = get_unsupported_team_agents(
        config,
        agent_names,
        closures={},
        allow_direct_private_agents=allow_direct_private_agents,
    )
    if not unsupported_agents:
        return
    agent_name, private_targets = next(iter(unsupported_agents.items()))
    raise ValueError(
        format_unsupported_team_agent_message(
            agent_name,
            prefix=prefix,
            private_targets=private_targets,
        ),
    )


def get_domain(config: RuntimeConfig) -> str:
    """Return the Matrix domain bound to this runtime config."""
    homeserver = runtime_matrix_homeserver(config.runtime_paths)
    return extract_server_name_from_homeserver(homeserver, config.runtime_paths)


def get_entity_thread_mode(
    config: RuntimeConfig,
    entity_name: str,
    room_id: str | None = None,
) -> Literal["thread", "room"]:
    """Return the effective entity thread mode in this runtime context."""
    from mindroom.entity_resolution import resolve_agent_thread_mode, router_agents_for_room  # noqa: PLC0415

    runtime_override = resolve_room_thread_mode_override(config.runtime_paths, room_id)
    if runtime_override is not None:
        return runtime_override
    if entity_name in config.agents:
        return resolve_agent_thread_mode(config.agents[entity_name], room_id, config.runtime_paths)
    if entity_name in config.teams:
        team_modes = {
            resolve_agent_thread_mode(config.agents[name], room_id, config.runtime_paths)
            for name in config.teams[entity_name].agents
            if name in config.agents
        }
        if len(team_modes) == 1:
            return next(iter(team_modes))
    if entity_name == ROUTER_AGENT_NAME:
        router_agents = router_agents_for_room(config.agents, config.teams, room_id, config.runtime_paths)
        configured_modes = {
            resolve_agent_thread_mode(config.agents[agent_name], room_id, config.runtime_paths)
            for agent_name in router_agents
        }
        if len(configured_modes) == 1:
            return next(iter(configured_modes))
    return "thread"


def get_model_context_window(config: Config, model_name: str) -> int | None:
    """Return one configured model's context window when known."""
    model_config = config.models.get(model_name)
    return model_config.context_window if model_config and model_config.context_window else None


def resolve_runtime_model(
    config: RuntimeConfig,
    *,
    entity_name: str | None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    room_id: str | None = None,
    thread_id: str | None = None,
    default_model_name: str = "default",
) -> ResolvedRuntimeModel:
    """Resolve the active model and context window in this runtime context."""
    resolved_model_name = active_model_name
    if resolved_model_name is None and thread_id is not None:
        thread_override = resolve_thread_model_override(
            config.runtime_paths,
            thread_id,
            configured_models=config.models,
        ).active
        if thread_override is not None:
            resolved_model_name = thread_override
    if resolved_model_name is None:
        if entity_name is None:
            resolved_model_name = default_model_name
        elif room_id is not None:
            from mindroom.entity_resolution import effective_entity_model_name  # noqa: PLC0415

            resolved_model_name = effective_entity_model_name(
                config,
                entity_name,
                room_id,
                config.runtime_paths,
            )
        else:
            resolved_model_name = _entity_model_name(config, entity_name)
    resolved_context_window = active_context_window
    if resolved_context_window is None:
        resolved_context_window = get_model_context_window(config, resolved_model_name)
    return ResolvedRuntimeModel(model_name=resolved_model_name, context_window=resolved_context_window)
