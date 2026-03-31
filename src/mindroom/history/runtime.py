"""Runtime integration for destructive history compaction."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agents import create_session_storage, create_state_storage_db, get_agent_session, get_team_session
from mindroom.history.compaction import (
    compact_scope_history,
    estimate_prompt_visible_history_tokens,
    estimate_static_tokens,
    estimate_team_static_tokens,
    normalize_compaction_budget_tokens,
    resolve_compaction_runtime_settings,
    resolve_effective_compaction_threshold,
)
from mindroom.history.storage import (
    clear_force_compaction_state,
    consume_pending_force_compaction_scope,
    read_scope_state,
    write_scope_state,
)
from mindroom.history.types import (
    HistoryPolicy,
    HistoryScope,
    HistoryScopeState,
    PreparedHistoryState,
    ResolvedHistorySettings,
)
from mindroom.logging_config import get_logger
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import CompactionOutcome
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_TEAM_STATE_ROOT_DIRNAME = "teams"
_TEAM_STORAGE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass(frozen=True)
class ScopeSessionContext:
    """Resolved storage/session context for one logical history scope."""

    scope: HistoryScope
    storage: SqliteDb
    session: AgentSession | TeamSession | None


@dataclass(frozen=True)
class BoundTeamScopeContext:
    """Resolved stable scope/storage for one live team run."""

    owner_agent: Agent
    owner_agent_name: str
    scope: HistoryScope
    storage: SqliteDb


@dataclass(frozen=True)
class _ResolvedPreparationInputs:
    history_settings: ResolvedHistorySettings
    compaction_config: CompactionConfig
    has_authored_compaction_config: bool
    active_model_name: str
    active_context_window: int | None
    compaction_context_window: int | None
    static_prompt_tokens: int


def resolve_history_scope(agent: Agent) -> HistoryScope | None:
    """Return the persisted history scope addressed by one live agent."""
    team_id = agent.team_id
    if isinstance(team_id, str) and team_id:
        return HistoryScope(kind="team", scope_id=team_id)
    agent_id = agent.id
    if isinstance(agent_id, str) and agent_id:
        return HistoryScope(kind="agent", scope_id=agent_id)
    return None


async def prepare_history_for_run(
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    storage: SqliteDb | None = None,
    session: AgentSession | TeamSession | None = None,
    history_settings: ResolvedHistorySettings | None = None,
    compaction_config: CompactionConfig | None = None,
    has_authored_compaction_config: bool | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    static_prompt_tokens: int | None = None,
    available_history_budget: int | None = None,
    scope: HistoryScope | None = None,
) -> PreparedHistoryState:
    """Prepare one scope by compacting its persisted session before the run."""
    resolved_scope = scope or resolve_history_scope(agent)
    scope_context = load_scope_session_context(
        agent=agent,
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        storage=storage,
        session=session,
        scope=resolved_scope,
    )
    if scope_context is None or scope_context.session is None:
        return PreparedHistoryState()

    resolved_inputs = _resolve_preparation_inputs(
        agent=agent,
        agent_name=agent_name,
        full_prompt=full_prompt,
        config=config,
        history_settings=history_settings,
        compaction_config=compaction_config,
        has_authored_compaction_config=has_authored_compaction_config,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
        static_prompt_tokens=static_prompt_tokens,
    )
    history_budget = available_history_budget
    if history_budget is None:
        history_budget = _resolve_available_history_budget(
            compaction_config=resolved_inputs.compaction_config,
            has_authored_compaction_config=resolved_inputs.has_authored_compaction_config,
            active_context_window=resolved_inputs.active_context_window,
            compaction_context_window=resolved_inputs.compaction_context_window,
            static_prompt_tokens=resolved_inputs.static_prompt_tokens,
        )

    session = scope_context.session
    state = _prepare_scope_state_for_run(
        storage=scope_context.storage,
        session=session,
        scope=scope_context.scope,
        history_budget=history_budget,
    )
    compaction_outcomes: list[CompactionOutcome] = []
    auto_compaction_enabled = (
        resolved_inputs.has_authored_compaction_config and resolved_inputs.compaction_config.enabled
    )
    current_history_tokens = (
        estimate_prompt_visible_history_tokens(
            session=session,
            scope=scope_context.scope,
            history_settings=resolved_inputs.history_settings,
        )
        if history_budget is not None
        else None
    )
    should_attempt_compaction = (state.force_compact_before_next_run and history_budget is not None) or (
        auto_compaction_enabled
        and history_budget is not None
        and current_history_tokens is not None
        and current_history_tokens > history_budget
    )
    logger.info(
        "Compaction check",
        agent=agent_name,
        auto_enabled=auto_compaction_enabled,
        budget=history_budget,
        current_tokens=current_history_tokens,
        force=state.force_compact_before_next_run,
        will_compact=should_attempt_compaction,
    )

    if should_attempt_compaction:
        compaction_budget = history_budget
        assert compaction_budget is not None
        try:
            _next_state, outcome = await compact_scope_history(
                storage=scope_context.storage,
                session=session,
                scope=scope_context.scope,
                state=state,
                config=config,
                runtime_paths=runtime_paths,
                compaction_config=resolved_inputs.compaction_config,
                history_settings=resolved_inputs.history_settings,
                available_history_budget=compaction_budget,
                active_model_name=resolved_inputs.active_model_name,
                active_context_window=resolved_inputs.active_context_window,
            )
        except Exception:
            clear_force_compaction_state(session, scope_context.scope, state)
            scope_context.storage.upsert_session(session)
            logger.exception(
                "Compaction failed; continuing without compaction",
                session_id=session.session_id,
                scope=scope_context.scope.key,
                force_compact_before_next_run=state.force_compact_before_next_run,
            )
        else:
            if outcome is not None:
                compaction_outcomes.append(outcome)
                logger.info(
                    "Compaction completed",
                    agent=agent_name,
                    outcome_mode=outcome.mode,
                    before_tokens=outcome.before_tokens,
                    after_tokens=outcome.after_tokens,
                    runs_compacted=outcome.compacted_run_count,
                )

    if not resolved_inputs.has_authored_compaction_config and history_budget is not None:
        _apply_implicit_context_window_guard(
            target=agent,
            session=session,
            scope=scope_context.scope,
            history_settings=resolved_inputs.history_settings,
            available_history_budget=history_budget,
        )

    prepared = PreparedHistoryState(
        compaction_outcomes=compaction_outcomes,
        has_persisted_history=_has_persisted_history(session, scope_context.scope),
    )
    if compaction_outcomes_collector is not None:
        compaction_outcomes_collector.extend(compaction_outcomes)
    return prepared


async def prepare_bound_agents_for_run(
    *,
    agents: list[Agent],
    team: Team | None = None,
    full_prompt: str,
    fallback_full_prompt: str | None = None,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    team_name: str | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
) -> PreparedHistoryState:
    """Prepare one team-owned scope by compacting its persisted session before the run."""
    bound_scope = resolve_bound_team_scope_context(
        agents=agents,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        team_name=team_name,
    )
    if bound_scope is None:
        return PreparedHistoryState()

    if team_name is not None and team_name in config.teams:
        history_settings = config.get_entity_history_settings(team_name)
        compaction_config = config.get_entity_compaction_config(team_name)
        has_authored_compaction_config = config.has_authored_entity_compaction_config(team_name)
        resolved_active_model_name = active_model_name or config.get_entity_model_name(team_name)
    else:
        history_settings = config.get_default_history_settings()
        compaction_config = config.get_default_compaction_config()
        has_authored_compaction_config = config.has_authored_default_compaction_config()
        resolved_active_model_name = active_model_name or "default"

    resolved_active_context_window = active_context_window
    if resolved_active_context_window is None:
        resolved_active_context_window = config.get_model_context_window(resolved_active_model_name)
    resolved_compaction_context_window = resolve_compaction_runtime_settings(
        config=config,
        compaction_config=compaction_config,
        active_model_name=resolved_active_model_name,
        active_context_window=resolved_active_context_window,
    ).context_window
    static_prompt_tokens = (
        estimate_preparation_static_tokens_for_team(
            team,
            full_prompt=full_prompt,
            fallback_full_prompt=fallback_full_prompt,
        )
        if team is not None
        else estimate_preparation_prompt_tokens(
            full_prompt=full_prompt,
            fallback_full_prompt=fallback_full_prompt,
        )
    )
    available_history_budget = _resolve_available_history_budget(
        compaction_config=compaction_config,
        has_authored_compaction_config=has_authored_compaction_config,
        active_context_window=resolved_active_context_window,
        compaction_context_window=resolved_compaction_context_window,
        static_prompt_tokens=static_prompt_tokens,
    )

    prepared = await prepare_history_for_run(
        agent=bound_scope.owner_agent,
        agent_name=bound_scope.owner_agent_name,
        full_prompt=full_prompt,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        compaction_outcomes_collector=compaction_outcomes_collector,
        storage=bound_scope.storage,
        history_settings=history_settings,
        compaction_config=compaction_config,
        has_authored_compaction_config=has_authored_compaction_config,
        active_model_name=resolved_active_model_name,
        active_context_window=resolved_active_context_window,
        static_prompt_tokens=static_prompt_tokens,
        available_history_budget=available_history_budget,
        scope=bound_scope.scope,
    )
    if team is not None and not has_authored_compaction_config and available_history_budget is not None:
        scope_context = load_scope_session_context(
            agent=bound_scope.owner_agent,
            agent_name=bound_scope.owner_agent_name,
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            storage=bound_scope.storage,
            scope=bound_scope.scope,
        )
        if scope_context is not None and scope_context.session is not None:
            _apply_implicit_context_window_guard(
                target=team,
                session=scope_context.session,
                scope=bound_scope.scope,
                history_settings=history_settings,
                available_history_budget=available_history_budget,
            )
    return prepared


def resolve_bound_history_owner(agents: list[Agent]) -> tuple[Agent | None, str | None]:
    """Return the canonical storage owner for one bound team run."""
    candidates = [(agent_id, agent) for agent in agents if isinstance((agent_id := agent.id), str) and agent_id]
    if not candidates:
        return None, None

    owner_agent_name = min(agent_id for agent_id, _agent in candidates)
    for agent_id, agent in candidates:
        if agent_id == owner_agent_name:
            return agent, owner_agent_name
    return None, None


def resolve_bound_team_scope_context(
    *,
    agents: list[Agent],
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    team_name: str | None = None,
) -> BoundTeamScopeContext | None:
    """Resolve the stable scope/storage backing one live team run."""
    owner_agent, owner_agent_name = resolve_bound_history_owner(agents)
    if owner_agent is None or owner_agent_name is None:
        return None

    team_scope_id = team_name if team_name is not None and team_name in config.teams else _ad_hoc_team_scope_id(agents)
    if team_scope_id is None:
        return None
    scope = HistoryScope(kind="team", scope_id=team_scope_id)
    storage = create_scope_session_storage(
        agent_name=owner_agent_name,
        scope=scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    return BoundTeamScopeContext(
        owner_agent=owner_agent,
        owner_agent_name=owner_agent_name,
        scope=scope,
        storage=storage,
    )


def estimate_preparation_static_tokens(
    agent: Agent,
    *,
    full_prompt: str,
    fallback_full_prompt: str | None = None,
) -> int:
    """Estimate static prompt tokens using the largest prompt variant this run may send."""
    primary_tokens = estimate_static_tokens(agent, full_prompt)
    if fallback_full_prompt is None:
        return primary_tokens
    return max(primary_tokens, estimate_static_tokens(agent, fallback_full_prompt))


def estimate_preparation_prompt_tokens(
    *,
    full_prompt: str,
    fallback_full_prompt: str | None = None,
) -> int:
    """Estimate prompt-only tokens using the largest prompt variant this run may send."""
    primary_tokens = estimate_text_tokens(full_prompt)
    if fallback_full_prompt is None:
        return primary_tokens
    return max(primary_tokens, estimate_text_tokens(fallback_full_prompt))


def estimate_preparation_static_tokens_for_team(
    team: Team,
    *,
    full_prompt: str,
    fallback_full_prompt: str | None = None,
) -> int:
    """Estimate team static tokens using the largest prompt variant this run may send."""
    primary_tokens = estimate_team_static_tokens(team, full_prompt)
    if fallback_full_prompt is None:
        return primary_tokens
    return max(primary_tokens, estimate_team_static_tokens(team, fallback_full_prompt))


def load_bound_scope_session_context(
    *,
    agents: list[Agent],
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    team_name: str | None = None,
    create_session_if_missing: bool = False,
) -> ScopeSessionContext | None:
    """Load the canonical scope-backed session context for one bound team run."""
    if session_id is None:
        return None
    bound_scope = resolve_bound_team_scope_context(
        agents=agents,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        team_name=team_name,
    )
    if bound_scope is None:
        return None
    return load_scope_session_context(
        agent=bound_scope.owner_agent,
        agent_name=bound_scope.owner_agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        storage=bound_scope.storage,
        scope=bound_scope.scope,
        create_session_if_missing=create_session_if_missing,
    )


def load_scope_session_context(
    *,
    agent: Agent,
    agent_name: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    storage: SqliteDb | None = None,
    session: AgentSession | TeamSession | None = None,
    scope: HistoryScope | None = None,
    create_session_if_missing: bool = False,
) -> ScopeSessionContext | None:
    """Load the canonical storage/session backing one scope for one live agent."""
    resolved_scope = scope or resolve_history_scope(agent)
    if session_id is None or resolved_scope is None:
        return None

    storage, session = _materialize_session(
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        scope=resolved_scope,
        storage=storage,
        session=session,
    )
    assert storage is not None
    if session is None and create_session_if_missing:
        created_at = int(datetime.now(UTC).timestamp())
        if resolved_scope.kind == "team":
            session = TeamSession(
                session_id=session_id,
                team_id=resolved_scope.scope_id,
                metadata={},
                runs=[],
                created_at=created_at,
                updated_at=created_at,
            )
        else:
            session = AgentSession(
                session_id=session_id,
                agent_id=_scope_session_agent_id(resolved_scope),
                metadata={},
                runs=[],
                created_at=created_at,
                updated_at=created_at,
            )
    return ScopeSessionContext(
        scope=resolved_scope,
        storage=storage,
        session=session,
    )


def _materialize_session(
    *,
    agent_name: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    scope: HistoryScope,
    storage: SqliteDb | None,
    session: AgentSession | TeamSession | None,
) -> tuple[SqliteDb | None, AgentSession | TeamSession | None]:
    if storage is None:
        storage = create_scope_session_storage(
            agent_name=agent_name,
            scope=scope,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
    if session is None:
        session = (
            get_team_session(storage, session_id) if scope.kind == "team" else get_agent_session(storage, session_id)
        )
    return storage, session


def create_scope_session_storage(
    *,
    agent_name: str,
    scope: HistoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> SqliteDb:
    """Create the canonical SQLite storage for one persisted history scope."""
    if scope.kind == "agent":
        return create_session_storage(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )

    storage_name = _scope_session_storage_name(scope)
    return create_state_storage_db(
        storage_name=storage_name,
        state_root=runtime_paths.storage_root / _TEAM_STATE_ROOT_DIRNAME / storage_name,
        subdir="sessions",
        session_table=f"{storage_name}_sessions",
    )


def _scope_session_storage_name(scope: HistoryScope) -> str:
    if scope.kind == "agent":
        return scope.scope_id
    normalized_scope_id = _TEAM_STORAGE_NAME_PATTERN.sub("_", scope.scope_id).strip("_") or "team"
    digest = hashlib.sha256(scope.key.encode()).hexdigest()[:12]
    return f"team_{normalized_scope_id}_{digest}"


def _scope_session_agent_id(scope: HistoryScope) -> str:
    if scope.kind == "agent":
        return scope.scope_id
    return _scope_session_storage_name(scope)


def _ad_hoc_team_scope_id(agents: list[Agent]) -> str | None:
    agent_names = [agent_id for agent in agents if isinstance((agent_id := agent.id), str) and agent_id]
    if not agent_names:
        return None
    return f"team_{'+'.join(sorted(agent_names))}"


def _history_settings_from_agent(agent: Agent) -> ResolvedHistorySettings:
    if agent.num_history_messages is not None:
        policy = HistoryPolicy(mode="messages", limit=agent.num_history_messages)
    elif agent.num_history_runs is not None:
        policy = HistoryPolicy(mode="runs", limit=agent.num_history_runs)
    else:
        policy = HistoryPolicy(mode="all")
    return ResolvedHistorySettings(
        policy=policy,
        max_tool_calls_from_history=agent.max_tool_calls_from_history,
        system_message_role=agent.system_message_role,
        skip_history_system_role=True,
    )


def _resolve_preparation_inputs(
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    config: Config,
    history_settings: ResolvedHistorySettings | None,
    compaction_config: CompactionConfig | None,
    has_authored_compaction_config: bool | None,
    active_model_name: str | None,
    active_context_window: int | None,
    static_prompt_tokens: int | None,
) -> _ResolvedPreparationInputs:
    resolved_history_settings = history_settings
    if resolved_history_settings is None:
        if agent_name in config.agents:
            resolved_history_settings = config.get_entity_history_settings(agent_name)
        else:
            resolved_history_settings = _history_settings_from_agent(agent)

    resolved_compaction_config = compaction_config
    if resolved_compaction_config is None:
        if agent_name in config.agents:
            resolved_compaction_config = config.get_entity_compaction_config(agent_name)
        else:
            resolved_compaction_config = config.get_default_compaction_config()

    resolved_has_authored_compaction_config = has_authored_compaction_config
    if resolved_has_authored_compaction_config is None:
        if agent_name in config.agents:
            resolved_has_authored_compaction_config = config.has_authored_entity_compaction_config(agent_name)
        else:
            resolved_has_authored_compaction_config = config.has_authored_default_compaction_config()

    resolved_active_model_name = active_model_name or config.get_entity_model_name(agent_name)
    resolved_active_context_window = active_context_window
    if resolved_active_context_window is None:
        resolved_active_context_window = config.get_model_context_window(resolved_active_model_name)
    resolved_compaction_context_window = resolve_compaction_runtime_settings(
        config=config,
        compaction_config=resolved_compaction_config,
        active_model_name=resolved_active_model_name,
        active_context_window=resolved_active_context_window,
    ).context_window

    resolved_static_prompt_tokens = static_prompt_tokens
    if resolved_static_prompt_tokens is None:
        resolved_static_prompt_tokens = estimate_static_tokens(agent, full_prompt)

    return _ResolvedPreparationInputs(
        history_settings=resolved_history_settings,
        compaction_config=resolved_compaction_config,
        has_authored_compaction_config=resolved_has_authored_compaction_config,
        active_model_name=resolved_active_model_name,
        active_context_window=resolved_active_context_window,
        compaction_context_window=resolved_compaction_context_window,
        static_prompt_tokens=resolved_static_prompt_tokens,
    )


def _resolve_available_history_budget(
    *,
    compaction_config: CompactionConfig,
    has_authored_compaction_config: bool,
    active_context_window: int | None,
    compaction_context_window: int | None,
    static_prompt_tokens: int,
) -> int | None:
    if compaction_context_window is None:
        logger.warning(
            "Compaction budget unavailable: no context_window configured for compaction model",
            compaction_model=compaction_config.model,
        )
        return None
    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is None:
        threshold_window = active_context_window
        if threshold_window is None:
            if not has_authored_compaction_config:
                return None
            threshold_window = compaction_context_window
        threshold_tokens = resolve_effective_compaction_threshold(compaction_config, threshold_window)

    ceiling = threshold_tokens
    if has_authored_compaction_config:
        ceiling_window = active_context_window if active_context_window is not None else compaction_context_window
        reserve_tokens = normalize_compaction_budget_tokens(compaction_config.reserve_tokens, ceiling_window)
        ceiling = min(ceiling, max(0, ceiling_window - reserve_tokens))

    return max(0, ceiling - static_prompt_tokens)


def _prepare_scope_state_for_run(
    *,
    storage: SqliteDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_budget: int | None,
) -> HistoryScopeState:
    state = read_scope_state(session, scope)
    if consume_pending_force_compaction_scope(session, scope):
        state = replace(state, force_compact_before_next_run=True)
        write_scope_state(session, scope, state)
        storage.upsert_session(session)
    if state.force_compact_before_next_run and history_budget is None:
        state = clear_force_compaction_state(session, scope, state)
        storage.upsert_session(session)
        logger.warning(
            "Forced compaction skipped because no history budget could be resolved",
            session_id=session.session_id,
            scope=scope.key,
        )
    return state


def _apply_implicit_context_window_guard(
    *,
    target: Agent | Team,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
) -> None:
    current_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    if current_tokens <= available_history_budget:
        return

    limit_mode, max_limit = _context_window_guard_limit_bounds(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    fitting_limit = _find_fitting_history_limit_for_budget(
        session=session,
        scope=scope,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        limit_mode=limit_mode,
        max_limit=max_limit,
    )
    if fitting_limit > 0:
        target.add_history_to_context = True
        target.add_session_summary_to_context = True
        if limit_mode == "messages":
            target.num_history_runs = None
            target.num_history_messages = fitting_limit
        else:
            target.num_history_runs = fitting_limit
            target.num_history_messages = None
        logger.warning(
            "Context window guard reduced replay for this run",
            scope=scope.key,
            limit_mode=limit_mode,
            new_limit=fitting_limit,
            estimated_tokens=current_tokens,
            available_history_budget=available_history_budget,
        )
        return

    target.add_history_to_context = False
    target.num_history_runs = None
    target.num_history_messages = None
    summary_tokens = _estimate_summary_tokens(session)
    target.add_session_summary_to_context = 0 < summary_tokens <= available_history_budget
    logger.warning(
        "Context window guard disabled persisted replay for this run",
        scope=scope.key,
        keep_summary_only=target.add_session_summary_to_context,
        estimated_tokens=current_tokens,
        available_history_budget=available_history_budget,
    )


def _context_window_guard_limit_bounds(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> tuple[Literal["runs", "messages"], int]:
    if history_settings.policy.mode == "messages":
        return "messages", history_settings.policy.limit or 0

    visible_run_count = len(_scope_completed_top_level_runs(session, scope))
    if history_settings.policy.mode == "all":
        return "runs", visible_run_count
    return "runs", min(history_settings.policy.limit or 0, visible_run_count)


def _find_fitting_history_limit_for_budget(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
    limit_mode: Literal["runs", "messages"],
    max_limit: int,
) -> int:
    if max_limit <= 0 or available_history_budget <= 0:
        return 0

    low = 1
    high = max_limit
    best = 0
    while low <= high:
        mid = (low + high) // 2
        candidate_tokens = estimate_prompt_visible_history_tokens(
            session=session,
            scope=scope,
            history_settings=ResolvedHistorySettings(
                policy=HistoryPolicy(mode=limit_mode, limit=mid),
                max_tool_calls_from_history=history_settings.max_tool_calls_from_history,
                system_message_role=history_settings.system_message_role,
                skip_history_system_role=history_settings.skip_history_system_role,
            ),
        )
        if candidate_tokens <= available_history_budget:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def _scope_completed_top_level_runs(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> list[RunOutput | TeamRunOutput]:
    skip_statuses = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}
    runs = [
        run
        for run in session.runs or []
        if isinstance(run, (RunOutput, TeamRunOutput)) and run.parent_run_id is None and run.status not in skip_statuses
    ]
    if scope.kind == "team":
        return [run for run in runs if isinstance(run, TeamRunOutput) and run.team_id == scope.scope_id]
    return [run for run in runs if isinstance(run, RunOutput) and run.agent_id == scope.scope_id]


def _estimate_summary_tokens(session: AgentSession | TeamSession) -> int:
    if session.summary is None:
        return 0
    summary_text = session.summary.summary
    if not isinstance(summary_text, str):
        return 0
    return estimate_text_tokens(summary_text.strip())


def _has_persisted_history(session: AgentSession | TeamSession, scope: HistoryScope) -> bool:
    summary = session.summary.summary if session.summary is not None else None
    if isinstance(summary, str) and summary.strip():
        return True

    skip_statuses = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}
    for run in session.runs or []:
        if not isinstance(run, (RunOutput, TeamRunOutput)):
            continue
        if run.parent_run_id is not None or run.status in skip_statuses:
            continue
        if scope.kind == "team" and isinstance(run, TeamRunOutput) and run.team_id == scope.scope_id:
            return True
        if scope.kind == "agent" and isinstance(run, RunOutput) and run.agent_id == scope.scope_id:
            return True
    return False
