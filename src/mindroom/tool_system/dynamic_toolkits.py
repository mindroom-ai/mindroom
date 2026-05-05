"""Dynamic toolkit session state helpers and runtime merge logic."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.config.models import ResolvedToolConfig
from mindroom.logging_config import get_logger
from mindroom.tool_system.catalog import validate_authored_tool_entry_overrides

if TYPE_CHECKING:
    from mindroom.config.main import Config


logger = get_logger(__name__)

_loaded_toolkits: dict[str, list[str]] = {}


@dataclass(frozen=True)
class DynamicToolkitSelection:
    """Resolved dynamic-toolkit session selection for one agent runtime."""

    loaded_toolkits: tuple[str, ...]
    runtime_tool_configs: tuple[ResolvedToolConfig, ...]


class _DynamicToolkitMergeError(ValueError):
    """Raised when one runtime toolkit selection cannot be merged safely."""


class DynamicToolkitConflictError(_DynamicToolkitMergeError):
    """Raised when two active tool sources define one tool with conflicting overrides."""

    def __init__(
        self,
        *,
        toolkit_name: str,
        tool_name: str,
        existing_overrides: dict[str, object],
        candidate_overrides: dict[str, object],
    ) -> None:
        self.toolkit_name = toolkit_name
        self.tool_name = tool_name
        self.existing_overrides = dict(existing_overrides)
        self.candidate_overrides = dict(candidate_overrides)
        msg = (
            f"Toolkit '{toolkit_name}' conflicts on tool '{tool_name}' because its overrides do not match "
            "the already active definition."
        )
        super().__init__(msg)


def _toolkit_scope_key(session_id: str) -> str:
    """Normalize one session id to the toolkit state scope used in memory."""
    separator = session_id.find(":$")
    if session_id.startswith("!") and separator != -1:
        return session_id[:separator]
    return session_id


def _ordered_loaded_toolkits(
    allowed_toolkits: list[str],
    loaded_toolkits: list[str],
) -> list[str]:
    loaded = set(loaded_toolkits)
    return [toolkit_name for toolkit_name in allowed_toolkits if toolkit_name in loaded]


def _initial_loaded_toolkits(config: Config, agent_name: str) -> list[str]:
    agent_config = config.get_agent(agent_name)
    return _ordered_loaded_toolkits(agent_config.allowed_toolkits, agent_config.initial_toolkits)


def _coerce_loaded_toolkits(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _sanitize_loaded_toolkits(
    config: Config,
    agent_name: str,
    loaded_toolkits: list[str],
) -> tuple[list[str], list[str]]:
    agent_config = config.get_agent(agent_name)
    invalid_toolkits: list[str] = []
    valid: list[str] = []
    for toolkit_name in loaded_toolkits:
        if toolkit_name not in config.toolkits or toolkit_name not in agent_config.allowed_toolkits:
            invalid_toolkits.append(toolkit_name)
            continue
        if config.get_toolkit_scope_incompatible_tools(agent_name, toolkit_name):
            invalid_toolkits.append(toolkit_name)
            continue
        valid.append(toolkit_name)
    ordered = _ordered_loaded_toolkits(agent_config.allowed_toolkits, valid)
    return ordered, invalid_toolkits


def _normalize_effective_tool_config_overrides(
    tool_name: str,
    overrides: dict[str, object],
) -> dict[str, object]:
    return validate_authored_tool_entry_overrides(tool_name, overrides)


def resolve_special_tool_names(
    agent_name: str,
    config: Config,
    delegation_depth: int,
    enable_dynamic_tools_manager: bool,
) -> list[str]:
    """Resolve the ordered special-case tool names for one agent runtime."""
    agent_config = config.get_agent(agent_name)
    tool_names: list[str] = []

    if agent_config.delegate_to:
        from mindroom.custom_tools.delegate import MAX_DELEGATION_DEPTH  # noqa: PLC0415

        if delegation_depth < MAX_DELEGATION_DEPTH:
            tool_names.append("delegate")

    allow_self_config = (
        agent_config.allow_self_config
        if agent_config.allow_self_config is not None
        else config.defaults.allow_self_config
    )
    if allow_self_config:
        tool_names.append("self_config")

    if enable_dynamic_tools_manager and agent_config.allowed_toolkits:
        tool_names.append("dynamic_tools")

    return tool_names


def get_loaded_toolkits_for_session(
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
) -> list[str]:
    """Return one session's loaded dynamic toolkits, initializing in-memory state when needed."""
    if session_id is None:
        return []

    scope_key = _toolkit_scope_key(session_id)
    raw_loaded_toolkits = _loaded_toolkits.get(scope_key)
    if raw_loaded_toolkits is None:
        raw_loaded_toolkits = _initial_loaded_toolkits(config, agent_name)
    else:
        raw_loaded_toolkits = _coerce_loaded_toolkits(raw_loaded_toolkits)

    loaded_toolkits, invalid_toolkits = _sanitize_loaded_toolkits(
        config,
        agent_name,
        raw_loaded_toolkits,
    )
    if invalid_toolkits:
        logger.warning(
            "Dropping invalid dynamic toolkits from in-memory session state",
            agent=agent_name,
            session_id=session_id,
            scope_key=scope_key,
            invalid_toolkits=invalid_toolkits,
        )

    if scope_key not in _loaded_toolkits or _loaded_toolkits[scope_key] != loaded_toolkits:
        _loaded_toolkits[scope_key] = list(loaded_toolkits)

    return loaded_toolkits


def save_loaded_toolkits_for_session(
    *,
    session_id: str | None,
    loaded_toolkits: list[str],
) -> None:
    """Persist one session's loaded toolkit set in memory."""
    if session_id is None:
        return

    _loaded_toolkits[_toolkit_scope_key(session_id)] = _coerce_loaded_toolkits(loaded_toolkits)


def _clear_session_toolkits(session_id: str) -> None:
    """Clear one session's loaded toolkit state."""
    _loaded_toolkits.pop(_toolkit_scope_key(session_id), None)


def merge_runtime_tool_configs(
    *,
    agent_name: str,
    config: Config,
    loaded_toolkits: list[str],
    delegation_depth: int = 0,
    enable_dynamic_tools_manager: bool = True,
) -> list[ResolvedToolConfig]:
    """Merge static and dynamic toolkit selections for one agent runtime."""
    merged_tool_configs = [
        ResolvedToolConfig(
            name=entry.name,
            tool_config_overrides=_normalize_effective_tool_config_overrides(
                entry.name,
                dict(entry.tool_config_overrides),
            ),
        )
        for entry in config.get_agent_tool_configs(agent_name)
    ]
    merged_by_name = {entry.name: entry.tool_config_overrides for entry in merged_tool_configs}

    for toolkit_name in _sanitize_loaded_toolkits(config, agent_name, loaded_toolkits)[0]:
        for toolkit_entry in config.get_toolkit_tool_configs(toolkit_name):
            candidate_overrides = _normalize_effective_tool_config_overrides(
                toolkit_entry.name,
                dict(toolkit_entry.tool_config_overrides),
            )
            existing_overrides = merged_by_name.get(toolkit_entry.name)
            if existing_overrides is None:
                appended = ResolvedToolConfig(
                    name=toolkit_entry.name,
                    tool_config_overrides=candidate_overrides,
                )
                merged_tool_configs.append(appended)
                merged_by_name[appended.name] = appended.tool_config_overrides
                continue
            if existing_overrides != candidate_overrides:
                raise DynamicToolkitConflictError(
                    toolkit_name=toolkit_name,
                    tool_name=toolkit_entry.name,
                    existing_overrides=existing_overrides,
                    candidate_overrides=candidate_overrides,
                )

    return _inject_special_tool_configs(
        merged_tool_configs,
        agent_name=agent_name,
        config=config,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=enable_dynamic_tools_manager,
    )


def _inject_special_tool_configs(
    resolved_tool_configs: list[ResolvedToolConfig],
    *,
    agent_name: str,
    config: Config,
    delegation_depth: int,
    enable_dynamic_tools_manager: bool,
) -> list[ResolvedToolConfig]:
    tool_names = [entry.name for entry in resolved_tool_configs]
    for tool_name in resolve_special_tool_names(
        agent_name=agent_name,
        config=config,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=enable_dynamic_tools_manager,
    ):
        if tool_name in tool_names:
            continue
        resolved_tool_configs.append(ResolvedToolConfig(name=tool_name, tool_config_overrides={}))
        tool_names.append(tool_name)

    return resolved_tool_configs


def resolve_dynamic_toolkit_selection(
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
    delegation_depth: int = 0,
) -> DynamicToolkitSelection:
    """Return the current loaded toolkits and final runtime tool selection for one session."""
    loaded_toolkits = get_loaded_toolkits_for_session(
        agent_name=agent_name,
        config=config,
        session_id=session_id,
    )
    runtime_tool_configs = merge_runtime_tool_configs(
        agent_name=agent_name,
        config=config,
        loaded_toolkits=loaded_toolkits,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=session_id is not None,
    )
    return DynamicToolkitSelection(
        loaded_toolkits=tuple(loaded_toolkits),
        runtime_tool_configs=tuple(runtime_tool_configs),
    )
