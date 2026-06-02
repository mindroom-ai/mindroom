"""Per-tool dynamic loading state helpers and runtime selection."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING

from mindroom.config.models import EffectiveToolConfig
from mindroom.logging_config import get_logger
from mindroom.tool_system.catalog import TOOL_METADATA, validate_authored_tool_entry_overrides

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config


logger = get_logger(__name__)

_loaded_tools: dict[tuple[str, str], list[str]] = {}
_loaded_tools_lock = RLock()


@dataclass(frozen=True)
class VisibleToolSurface:
    """Provider-visible runtime tool surface for one agent/session."""

    loaded_tools: tuple[str, ...]
    runtime_tool_configs: tuple[EffectiveToolConfig, ...]


@dataclass(frozen=True)
class LoadToolResult:
    """Result of one locked dynamic-tool load attempt."""

    status: str
    loaded_tools: tuple[str, ...]
    available_tools: tuple[str, ...] = ()
    unsupported_tools: tuple[str, ...] = ()
    collision_messages: tuple[str, ...] = ()
    unavailable_messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadToolValidationFailure:
    """Failure returned by runtime validators before a dynamic tool is persisted."""

    status: str
    messages: tuple[str, ...]


@dataclass(frozen=True)
class DeferredToolCatalogEntry:
    """Prompt/search metadata for one authored deferred tool."""

    name: str
    description: str
    display_name: str
    category: str
    function_names: tuple[str, ...]
    loaded: bool
    sticky: bool


def _dynamic_tool_scope_key(session_id: str) -> str:
    """Normalize one session id to the dynamic-tool state scope used in memory."""
    return session_id


def _state_key(agent_name: str, session_id: str) -> tuple[str, str]:
    return (agent_name, _dynamic_tool_scope_key(session_id))


def _coerce_loaded_tools(value: object) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _ordered_deferred_tools(
    deferred_tool_names: list[str],
    loaded_tools: list[str],
) -> list[str]:
    loaded = set(loaded_tools)
    return [tool_name for tool_name in deferred_tool_names if tool_name in loaded]


def _deferred_tool_names(config: Config, agent_name: str) -> list[str]:
    return [entry.name for entry in config.get_agent_authored_deferred_tool_configs(agent_name)]


def _initial_loaded_tools(config: Config, agent_name: str) -> list[str]:
    return [entry.name for entry in config.get_agent_authored_deferred_tool_configs(agent_name) if entry.initial]


def _sanitize_loaded_tools(
    config: Config,
    agent_name: str,
    loaded_tools: list[str],
) -> tuple[list[str], list[str]]:
    deferred_tool_names = _deferred_tool_names(config, agent_name)
    deferred_tool_name_set = set(deferred_tool_names)
    invalid_tools: list[str] = []
    valid: list[str] = []
    for tool_name in loaded_tools:
        if tool_name not in deferred_tool_name_set:
            invalid_tools.append(tool_name)
            continue
        if config.get_deferred_tool_scope_incompatible_tools(agent_name, tool_name):
            invalid_tools.append(tool_name)
            continue
        valid.append(tool_name)
    return _ordered_deferred_tools(deferred_tool_names, valid), invalid_tools


def _sanitize_loaded_tools_with_current_initials(
    config: Config,
    agent_name: str,
    loaded_tools: list[str],
) -> tuple[list[str], list[str]]:
    return _sanitize_loaded_tools(
        config,
        agent_name,
        [*_initial_loaded_tools(config, agent_name), *loaded_tools],
    )


def _normalize_effective_tool_config_overrides(
    tool_name: str,
    overrides: dict[str, object],
) -> dict[str, object]:
    return validate_authored_tool_entry_overrides(tool_name, overrides)


def has_deferred_tools(config: Config, agent_name: str) -> bool:
    """Return whether one agent has at least one authored deferred tool."""
    return bool(config.get_agent_authored_deferred_tool_configs(agent_name))


def _special_tool_names(
    agent_name: str,
    config: Config,
    delegation_depth: int,
    enable_dynamic_tools_manager: bool,
) -> list[str]:
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

    if enable_dynamic_tools_manager and has_deferred_tools(config, agent_name):
        tool_names.append("dynamic_tools")

    return tool_names


def _tool_config_owner_precedence(config: Config, entry: EffectiveToolConfig) -> tuple[int, int, int]:
    directly_authored = entry.name == (entry.authored_name or entry.name)
    authored_from_individual_tool = not config.is_tool_preset(entry.authored_name or entry.name)
    return (int(directly_authored), int(authored_from_individual_tool), -entry.authored_order)


def _expanded_authored_entry_config(entry: EffectiveToolConfig, tool_name: str) -> EffectiveToolConfig:
    return EffectiveToolConfig(
        name=tool_name,
        tool_config_overrides=(dict(entry.tool_config_overrides) if tool_name == entry.name else {}),
        defer=entry.defer,
        initial=entry.initial,
        authored_order=entry.authored_order,
        authored_name=entry.name,
    )


def _visible_authored_tool_configs(
    config: Config,
    agent_name: str,
    *,
    loaded_deferred_tools: set[str],
) -> list[EffectiveToolConfig]:
    visible_by_name: dict[str, EffectiveToolConfig] = {}
    hidden_deferred_by_name: dict[str, EffectiveToolConfig] = {}

    for authored_entry in config._get_agent_authored_tool_configs(agent_name):
        is_loaded_deferred = authored_entry.defer and authored_entry.name in loaded_deferred_tools
        is_visible_owner = not authored_entry.defer or is_loaded_deferred
        for tool_name in config.expand_tool_names([authored_entry.name]):
            expanded = _expanded_authored_entry_config(authored_entry, tool_name)
            expanded_precedence = _tool_config_owner_precedence(config, expanded)
            if is_visible_owner:
                current = visible_by_name.get(tool_name)
                if current is None or expanded_precedence > _tool_config_owner_precedence(config, current):
                    visible_by_name[tool_name] = expanded
                continue
            current_hidden = hidden_deferred_by_name.get(tool_name)
            if current_hidden is None or expanded_precedence > _tool_config_owner_precedence(config, current_hidden):
                hidden_deferred_by_name[tool_name] = expanded

    resolved: list[EffectiveToolConfig] = []
    for tool_name in config.get_agent_available_tools(agent_name):
        visible = visible_by_name.get(tool_name)
        if visible is None:
            continue
        hidden = hidden_deferred_by_name.get(tool_name)
        if (
            hidden is not None
            and not visible.defer
            and _tool_config_owner_precedence(config, hidden) > _tool_config_owner_precedence(config, visible)
        ):
            continue
        resolved.append(visible)
    return resolved


def _append_injected_special_tool_configs(
    resolved_tool_configs: list[EffectiveToolConfig],
    *,
    agent_name: str,
    config: Config,
    delegation_depth: int,
    enable_dynamic_tools_manager: bool,
) -> list[EffectiveToolConfig]:
    tool_names = [entry.name for entry in resolved_tool_configs]
    for tool_name in _special_tool_names(
        agent_name=agent_name,
        config=config,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=enable_dynamic_tools_manager,
    ):
        if tool_name in tool_names:
            continue
        if config.get_agent_authored_deferred_tool_config(agent_name, tool_name) is not None:
            continue
        resolved_tool_configs.append(
            EffectiveToolConfig(
                name=tool_name,
                tool_config_overrides={},
                authored_order=len(resolved_tool_configs),
                authored_name=tool_name,
            ),
        )
        tool_names.append(tool_name)
    return resolved_tool_configs


def visible_tool_surface(
    *,
    agent_name: str,
    config: Config,
    session_id: str | None = None,
    loaded_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None = None,
    delegation_depth: int = 0,
    enable_dynamic_tools_manager: bool | None = None,
) -> VisibleToolSurface:
    """Return the canonical provider-visible runtime tool surface for one agent/session."""
    if loaded_tools is None:
        loaded_tool_names = get_loaded_tools_for_session(
            agent_name=agent_name,
            config=config,
            session_id=session_id,
        )
    else:
        loaded_tool_names = _sanitize_loaded_tools_with_current_initials(
            config,
            agent_name,
            _coerce_loaded_tools(list(loaded_tools)),
        )[0]

    manager_enabled = session_id is not None if enable_dynamic_tools_manager is None else enable_dynamic_tools_manager
    loaded_deferred_tools = set(loaded_tool_names)
    runtime_tool_configs = [
        EffectiveToolConfig(
            name=entry.name,
            tool_config_overrides=_normalize_effective_tool_config_overrides(
                entry.name,
                dict(entry.tool_config_overrides),
            ),
            defer=entry.defer,
            initial=entry.initial,
            authored_order=entry.authored_order,
            authored_name=entry.authored_name,
        )
        for entry in _visible_authored_tool_configs(
            config,
            agent_name,
            loaded_deferred_tools=loaded_deferred_tools,
        )
    ]

    runtime_tool_configs = _append_injected_special_tool_configs(
        runtime_tool_configs,
        agent_name=agent_name,
        config=config,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=manager_enabled,
    )
    return VisibleToolSurface(
        loaded_tools=tuple(loaded_tool_names),
        runtime_tool_configs=tuple(runtime_tool_configs),
    )


def get_loaded_tools_for_session(
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
) -> list[str]:
    """Return one agent/session's loaded dynamic tools, initializing in-memory state when needed."""
    if session_id is None:
        loaded_tools, _invalid_tools = _sanitize_loaded_tools_with_current_initials(config, agent_name, [])
        return list(loaded_tools)

    key = _state_key(agent_name, session_id)
    with _loaded_tools_lock:
        raw_loaded_tools = _loaded_tools.get(key)
        if raw_loaded_tools is None:
            raw_loaded_tools = _initial_loaded_tools(config, agent_name)
        else:
            raw_loaded_tools = _coerce_loaded_tools(raw_loaded_tools)

        loaded_tools, invalid_tools = _sanitize_loaded_tools_with_current_initials(config, agent_name, raw_loaded_tools)
        if invalid_tools:
            logger.warning(
                "Dropping invalid dynamic tools from in-memory session state",
                agent=agent_name,
                session_id=session_id,
                scope_key=key[1],
                invalid_tools=invalid_tools,
            )

        if key not in _loaded_tools or _loaded_tools[key] != loaded_tools:
            save_loaded_tools_for_session(
                agent_name=agent_name,
                session_id=session_id,
                loaded_tools=loaded_tools,
            )

        return list(loaded_tools)


def load_tool_for_session(
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
    tool_name: str,
    validate_loaded_tools: Callable[[list[str]], LoadToolValidationFailure | None] | None = None,
) -> LoadToolResult:
    """Atomically validate and add one loaded tool to an agent/session state."""
    deferred_tool_names = _deferred_tool_names(config, agent_name)
    if session_id is None:
        loaded_tools, _invalid_tools = _sanitize_loaded_tools_with_current_initials(config, agent_name, [])
        return LoadToolResult(status="error", loaded_tools=tuple(loaded_tools))

    key = _state_key(agent_name, session_id)
    with _loaded_tools_lock:
        raw_loaded_tools = _loaded_tools.get(key)
        if raw_loaded_tools is None:
            raw_loaded_tools = _initial_loaded_tools(config, agent_name)
        else:
            raw_loaded_tools = _coerce_loaded_tools(raw_loaded_tools)

        loaded_tools, invalid_tools = _sanitize_loaded_tools_with_current_initials(config, agent_name, raw_loaded_tools)
        if invalid_tools:
            logger.warning(
                "Dropping invalid dynamic tools from in-memory session state",
                agent=agent_name,
                session_id=session_id,
                scope_key=key[1],
                invalid_tools=invalid_tools,
            )
            _loaded_tools[key] = list(loaded_tools)

        if tool_name not in deferred_tool_names:
            result = LoadToolResult(
                status="unknown",
                loaded_tools=tuple(loaded_tools),
                available_tools=tuple(deferred_tool_names),
            )
        elif incompatible_tools := config.get_deferred_tool_scope_incompatible_tools(agent_name, tool_name):
            result = LoadToolResult(
                status="scope_incompatible",
                loaded_tools=tuple(loaded_tools),
                unsupported_tools=tuple(incompatible_tools),
            )
        elif tool_name in loaded_tools:
            result = LoadToolResult(status="already_loaded", loaded_tools=tuple(loaded_tools))
        else:
            candidate_loaded_tools = _ordered_deferred_tools(deferred_tool_names, [*loaded_tools, tool_name])
            validation_failure = (
                validate_loaded_tools(candidate_loaded_tools) if validate_loaded_tools is not None else None
            )
            if validation_failure is not None and validation_failure.status == "function_name_collision":
                result = LoadToolResult(
                    status="function_name_collision",
                    loaded_tools=tuple(loaded_tools),
                    collision_messages=tuple(sorted(validation_failure.messages)),
                )
            elif validation_failure is not None and validation_failure.status == "tool_unavailable":
                result = LoadToolResult(
                    status="tool_unavailable",
                    loaded_tools=tuple(loaded_tools),
                    unavailable_messages=tuple(sorted(validation_failure.messages)),
                )
            else:
                _loaded_tools[key] = list(candidate_loaded_tools)
                result = LoadToolResult(status="loaded", loaded_tools=tuple(candidate_loaded_tools))
        return result


def unload_tool_for_session(
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
    tool_name: str,
) -> list[str]:
    """Atomically remove one loaded tool from an agent/session state."""
    if session_id is None:
        return []

    key = _state_key(agent_name, session_id)
    with _loaded_tools_lock:
        raw_loaded_tools = _loaded_tools.get(key)
        if raw_loaded_tools is None:
            raw_loaded_tools = _initial_loaded_tools(config, agent_name)
        else:
            raw_loaded_tools = _coerce_loaded_tools(raw_loaded_tools)

        loaded_tools, invalid_tools = _sanitize_loaded_tools_with_current_initials(config, agent_name, raw_loaded_tools)
        if invalid_tools:
            logger.warning(
                "Dropping invalid dynamic tools from in-memory session state",
                agent=agent_name,
                session_id=session_id,
                scope_key=key[1],
                invalid_tools=invalid_tools,
            )
        initial_tools = set(_initial_loaded_tools(config, agent_name))
        loaded_tools = [name for name in loaded_tools if name != tool_name or name in initial_tools]
        save_loaded_tools_for_session(
            agent_name=agent_name,
            session_id=session_id,
            loaded_tools=loaded_tools,
        )
        return list(loaded_tools)


def save_loaded_tools_for_session(
    *,
    agent_name: str,
    session_id: str | None,
    loaded_tools: list[str],
) -> None:
    """Persist one agent/session's loaded tool set in memory."""
    if session_id is None:
        return

    key = _state_key(agent_name, session_id)
    with _loaded_tools_lock:
        _loaded_tools[key] = _coerce_loaded_tools(loaded_tools)


def deferred_tool_catalog_entries(
    *,
    agent_name: str,
    config: Config,
    loaded_tools: list[str],
) -> list[DeferredToolCatalogEntry]:
    """Return model-visible catalog metadata for one agent's authored deferred tools."""
    loaded = set(loaded_tools)
    entries: list[DeferredToolCatalogEntry] = []
    for entry in config.get_agent_authored_deferred_tool_configs(agent_name):
        metadata = TOOL_METADATA.get(entry.name)
        entries.append(
            DeferredToolCatalogEntry(
                name=entry.name,
                description=(metadata.description if metadata else ""),
                display_name=(metadata.display_name if metadata else ""),
                category=(metadata.category.value if metadata else ""),
                function_names=tuple(metadata.function_names or ()) if metadata else (),
                loaded=entry.name in loaded,
                sticky=entry.initial,
            ),
        )
    return entries


def resolve_dynamic_tool_selection(
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
    delegation_depth: int = 0,
) -> VisibleToolSurface:
    """Return the current loaded tools and final runtime tool selection for one session."""
    return visible_tool_surface(
        agent_name=agent_name,
        config=config,
        session_id=session_id,
        delegation_depth=delegation_depth,
    )
