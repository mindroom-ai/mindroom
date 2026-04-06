"""Dynamic toolkit session state helpers and runtime merge logic."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from agno.db.base import SessionType
from agno.session.agent import AgentSession

from mindroom.config.models import ResolvedToolConfig
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from agno.db.sqlite import SqliteDb

    from mindroom.config.main import Config


logger = get_logger(__name__)

_MINDROOM_SESSION_KEY = "mindroom"
_DYNAMIC_TOOLKITS_KEY = "dynamic_toolkits"
_DYNAMIC_TOOLKITS_VERSION = 1


@dataclass(frozen=True)
class DynamicToolkitSelection:
    """Resolved dynamic-toolkit session selection for one agent runtime."""

    loaded_toolkits: tuple[str, ...]
    runtime_tool_configs: tuple[ResolvedToolConfig, ...]


class DynamicToolkitMergeError(ValueError):
    """Raised when one runtime toolkit selection cannot be merged safely."""


class DynamicToolkitConflictError(DynamicToolkitMergeError):
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


def _ordered_loaded_toolkits(
    allowed_toolkits: list[str],
    loaded_toolkits: list[str],
) -> list[str]:
    loaded = set(loaded_toolkits)
    return [toolkit_name for toolkit_name in allowed_toolkits if toolkit_name in loaded]


def _initial_loaded_toolkits(config: Config, agent_name: str) -> list[str]:
    agent_config = config.get_agent(agent_name)
    return _ordered_loaded_toolkits(agent_config.allowed_toolkits, agent_config.initial_toolkits)


def _session_payload(loaded_toolkits: list[str]) -> dict[str, object]:
    return {
        "version": _DYNAMIC_TOOLKITS_VERSION,
        "loaded": list(loaded_toolkits),
    }


def _new_agent_session(*, session_id: str, agent_name: str) -> AgentSession:
    now = int(time.time())
    return AgentSession(
        session_id=session_id,
        agent_id=agent_name,
        session_data={},
        created_at=now,
        updated_at=now,
    )


def _get_agent_session(storage: SqliteDb, session_id: str) -> AgentSession | None:
    raw = storage.get_session(session_id, SessionType.AGENT)
    if raw is None:
        return None
    if isinstance(raw, AgentSession):
        return raw
    if isinstance(raw, dict):
        return AgentSession.from_dict(cast("dict[str, Any]", raw))
    return None


def _ensure_mindroom_session_data(session: AgentSession) -> dict[str, object]:
    session_data = session.session_data
    if not isinstance(session_data, dict):
        session_data = {}
        session.session_data = session_data

    mindroom_data = session_data.get(_MINDROOM_SESSION_KEY)
    if not isinstance(mindroom_data, dict):
        mindroom_data = {}
        session_data[_MINDROOM_SESSION_KEY] = mindroom_data
    return cast("dict[str, object]", mindroom_data)


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
    from mindroom.tool_system.metadata import validate_authored_overrides  # noqa: PLC0415

    return validate_authored_overrides(tool_name, overrides)


def get_loaded_toolkits_for_session(
    storage: SqliteDb | None,
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
) -> list[str]:
    """Return one session's loaded dynamic toolkits, initializing persisted state when needed."""
    if storage is None or session_id is None:
        return _initial_loaded_toolkits(config, agent_name)

    session = _get_agent_session(storage, session_id)
    if session is None:
        session = _new_agent_session(session_id=session_id, agent_name=agent_name)
        loaded_toolkits = _initial_loaded_toolkits(config, agent_name)
        _ensure_mindroom_session_data(session)[_DYNAMIC_TOOLKITS_KEY] = _session_payload(loaded_toolkits)
        storage.upsert_session(session)
        return loaded_toolkits

    mindroom_data = _ensure_mindroom_session_data(session)
    stored_state_object = mindroom_data.get(_DYNAMIC_TOOLKITS_KEY)
    stored_state = (
        cast("dict[str, object] | None", stored_state_object) if isinstance(stored_state_object, dict) else None
    )
    if stored_state is None or stored_state.get("version") != _DYNAMIC_TOOLKITS_VERSION:
        loaded_toolkits = _initial_loaded_toolkits(config, agent_name)
        mindroom_data[_DYNAMIC_TOOLKITS_KEY] = _session_payload(loaded_toolkits)
        storage.upsert_session(session)
        return loaded_toolkits

    loaded_toolkits, invalid_toolkits = _sanitize_loaded_toolkits(
        config,
        agent_name,
        _coerce_loaded_toolkits(stored_state.get("loaded")),
    )
    if invalid_toolkits:
        logger.warning(
            "Dropping invalid dynamic toolkits from session state",
            agent=agent_name,
            session_id=session_id,
            invalid_toolkits=invalid_toolkits,
        )

    if stored_state.get("loaded") != loaded_toolkits:
        mindroom_data[_DYNAMIC_TOOLKITS_KEY] = _session_payload(loaded_toolkits)
        storage.upsert_session(session)

    return loaded_toolkits


def save_loaded_toolkits_for_session(
    storage: SqliteDb | None,
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
    loaded_toolkits: list[str],
) -> list[str]:
    """Persist one session's loaded toolkit set in canonical allowed-toolkit order."""
    if storage is None or session_id is None:
        msg = "Dynamic toolkit changes require a stable session_id."
        raise ValueError(msg)

    sanitized_loaded_toolkits, invalid_toolkits = _sanitize_loaded_toolkits(config, agent_name, loaded_toolkits)
    if invalid_toolkits:
        msg = f"Cannot persist unknown, disallowed, or scope-incompatible toolkits: {', '.join(invalid_toolkits)}"
        raise ValueError(msg)

    session = _get_agent_session(storage, session_id)
    if session is None:
        session = _new_agent_session(session_id=session_id, agent_name=agent_name)

    _ensure_mindroom_session_data(session)[_DYNAMIC_TOOLKITS_KEY] = _session_payload(sanitized_loaded_toolkits)
    storage.upsert_session(session)
    return sanitized_loaded_toolkits


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
    agent_config = config.get_agent(agent_name)

    if agent_config.delegate_to and "delegate" not in tool_names:
        from mindroom.custom_tools.delegate import MAX_DELEGATION_DEPTH  # noqa: PLC0415

        if delegation_depth < MAX_DELEGATION_DEPTH:
            resolved_tool_configs.append(ResolvedToolConfig(name="delegate", tool_config_overrides={}))
            tool_names.append("delegate")

    allow_self_config = (
        agent_config.allow_self_config
        if agent_config.allow_self_config is not None
        else config.defaults.allow_self_config
    )
    if allow_self_config and "self_config" not in tool_names:
        resolved_tool_configs.append(ResolvedToolConfig(name="self_config", tool_config_overrides={}))
        tool_names.append("self_config")

    if enable_dynamic_tools_manager and agent_config.allowed_toolkits and "dynamic_tools" not in tool_names:
        resolved_tool_configs.append(ResolvedToolConfig(name="dynamic_tools", tool_config_overrides={}))

    return resolved_tool_configs


def resolve_dynamic_toolkit_selection(
    storage: SqliteDb | None,
    *,
    agent_name: str,
    config: Config,
    session_id: str | None,
    delegation_depth: int = 0,
) -> DynamicToolkitSelection:
    """Return the current loaded toolkits and final runtime tool selection for one session."""
    loaded_toolkits = get_loaded_toolkits_for_session(
        storage,
        agent_name=agent_name,
        config=config,
        session_id=session_id,
    )
    runtime_tool_configs = merge_runtime_tool_configs(
        agent_name=agent_name,
        config=config,
        loaded_toolkits=loaded_toolkits,
        delegation_depth=delegation_depth,
        enable_dynamic_tools_manager=storage is not None and session_id is not None,
    )
    return DynamicToolkitSelection(
        loaded_toolkits=tuple(loaded_toolkits),
        runtime_tool_configs=tuple(runtime_tool_configs),
    )
