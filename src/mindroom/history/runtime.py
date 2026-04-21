"""Runtime integration for destructive history compaction."""

from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom import ai_runtime, model_loading
from mindroom.agent_storage import create_state_storage_db, get_agent_runtime_sqlite_dbs
from mindroom.agents import (
    create_session_storage,
    get_agent_session,
    get_team_session,
)
from mindroom.history.compaction import (
    compact_scope_history,
    completed_top_level_runs,
    estimate_agent_static_tokens,
    estimate_prompt_visible_history_tokens,
    estimate_team_static_tokens,
    runs_for_scope,
)
from mindroom.history.policy import (
    describe_compaction_unavailability,
    resolve_history_execution_plan,
    should_attempt_destructive_compaction,
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
    ResolvedHistoryExecutionPlan,
    ResolvedHistorySettings,
    ResolvedReplayPlan,
)
from mindroom.logging_config import get_logger
from mindroom.timing import timed
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.models.base import Model
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import CompactionOutcome
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_TEAM_STATE_ROOT_DIRNAME = "teams"
_TEAM_STORAGE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]+")


@timed("system_prompt_assembly.history_prepare.compaction_model_init")
def _load_compaction_model(
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str,
) -> Model:
    """Load the compaction model with dedicated history-preparation timing."""
    return model_loading.get_model_instance(config, runtime_paths, model_name)


def scrub_scope_session_queued_notices(
    scope_context: ScopeSessionContext | None,
    *,
    entity_name: str,
) -> None:
    """Strip queued-message notices from one loaded scope session before replay."""
    ai_runtime.scrub_queued_notice_session_context(
        scope_context=scope_context,
        entity_name=entity_name,
    )


@dataclass(frozen=True)
class ScopeSessionContext:
    """Resolved storage/session context for one logical history scope."""

    scope: HistoryScope
    storage: SqliteDb
    session: AgentSession | TeamSession | None


@dataclass(frozen=True)
class BoundTeamScopeContext:
    """Resolved stable owner and scope for one live team run."""

    owner_agent: Agent
    owner_agent_name: str
    scope: HistoryScope


@dataclass(frozen=True)
class _ResolvedPreparationInputs:
    history_settings: ResolvedHistorySettings
    compaction_config: CompactionConfig
    has_authored_compaction_config: bool
    active_model_name: str
    active_context_window: int | None
    static_prompt_tokens: int
    execution_plan: ResolvedHistoryExecutionPlan


@dataclass(frozen=True)
class PreparedScopeHistory:
    """Durable history preparation result before final replay planning."""

    scope: HistoryScope | None
    session: AgentSession | TeamSession | None
    resolved_inputs: _ResolvedPreparationInputs
    compaction_outcomes: list[CompactionOutcome] = field(default_factory=list)


def resolve_history_scope(agent: Agent) -> HistoryScope | None:
    """Return the persisted history scope addressed by one live agent."""
    team_id = agent.team_id
    if isinstance(team_id, str) and team_id:
        return HistoryScope(kind="team", scope_id=team_id)
    agent_id = agent.id
    if isinstance(agent_id, str) and agent_id:
        return HistoryScope(kind="agent", scope_id=agent_id)
    return None


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_scope_history(
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    scope_context: ScopeSessionContext | None = None,
    history_settings: ResolvedHistorySettings | None = None,
    compaction_config: CompactionConfig | None = None,
    has_authored_compaction_config: bool | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    static_prompt_tokens: int | None = None,
    available_history_budget: int | None = None,
    scope: HistoryScope | None = None,
    execution_plan: ResolvedHistoryExecutionPlan | None = None,
    timing_scope: str | None = None,
) -> PreparedScopeHistory:
    """Prepare durable scope history before final replay planning."""
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
        execution_plan=execution_plan,
    )
    resolved_scope = scope or resolve_history_scope(agent)
    if scope_context is None or scope_context.session is None:
        return PreparedScopeHistory(
            scope=resolved_scope,
            session=None,
            resolved_inputs=resolved_inputs,
        )

    execution_plan = resolved_inputs.execution_plan
    history_budget = available_history_budget
    if history_budget is None:
        history_budget = execution_plan.replay_budget_tokens

    session = scope_context.session
    state = _prepare_scope_state_for_run(
        storage=scope_context.storage,
        session=session,
        scope=scope_context.scope,
        execution_plan=execution_plan,
    )
    compaction_outcomes: list[CompactionOutcome] = []
    current_history_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope_context.scope,
        history_settings=resolved_inputs.history_settings,
    )
    should_compact = should_attempt_destructive_compaction(
        plan=execution_plan,
        force_compact_before_next_run=state.force_compact_before_next_run,
        current_history_tokens=current_history_tokens,
        replay_budget_tokens=history_budget,
    )
    logger.info(
        "History preparation check",
        agent=agent_name,
        auto_enabled=execution_plan.authored_compaction_enabled and execution_plan.destructive_compaction_available,
        compaction_available=execution_plan.destructive_compaction_available,
        replay_budget=history_budget,
        current_tokens=current_history_tokens,
        force=state.force_compact_before_next_run,
        compaction_requested=should_compact,
        unavailable_reason=execution_plan.unavailable_reason,
    )

    if should_compact:
        assert execution_plan.summary_input_budget_tokens is not None
        summary_model = _load_compaction_model(
            config,
            runtime_paths,
            execution_plan.compaction_model_name,
        )
        try:
            _next_state, outcome = await compact_scope_history(
                storage=scope_context.storage,
                session=session,
                scope=scope_context.scope,
                state=state,
                history_settings=resolved_inputs.history_settings,
                available_history_budget=history_budget,
                summary_input_budget=execution_plan.summary_input_budget_tokens,
                summary_model=summary_model,
                summary_model_name=execution_plan.compaction_model_name,
                active_context_window=resolved_inputs.active_context_window,
                replay_window_tokens=execution_plan.replay_window_tokens,
                threshold_tokens=execution_plan.trigger_threshold_tokens,
                reserve_tokens=execution_plan.reserve_tokens,
                notify=resolved_inputs.compaction_config.notify,
                timing_scope=timing_scope,
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
    if compaction_outcomes_collector is not None:
        compaction_outcomes_collector.extend(compaction_outcomes)
    return PreparedScopeHistory(
        scope=scope_context.scope,
        session=scope_context.session,
        resolved_inputs=resolved_inputs,
        compaction_outcomes=compaction_outcomes,
    )


def finalize_history_preparation(
    *,
    prepared_scope_history: PreparedScopeHistory,
    config: Config,
    static_prompt_tokens: int | None = None,
    available_history_budget: int | None = None,
) -> PreparedHistoryState:
    """Return the final persisted-replay decision after durable history prep."""
    resolved_inputs = prepared_scope_history.resolved_inputs
    resolved_static_prompt_tokens = (
        resolved_inputs.static_prompt_tokens if static_prompt_tokens is None else static_prompt_tokens
    )
    execution_plan = resolve_history_execution_plan(
        config=config,
        compaction_config=resolved_inputs.compaction_config,
        has_authored_compaction_config=resolved_inputs.has_authored_compaction_config,
        active_model_name=resolved_inputs.active_model_name,
        active_context_window=resolved_inputs.active_context_window,
        static_prompt_tokens=resolved_static_prompt_tokens,
    )
    history_budget = available_history_budget
    if history_budget is None:
        history_budget = execution_plan.replay_budget_tokens
        if execution_plan.authored_compaction_enabled and execution_plan.unavailable_reason is not None:
            description = describe_compaction_unavailability(execution_plan)
            logger.warning(
                "Compaction unavailable for this run",
                compaction_model=execution_plan.compaction_model_name,
                reason=description,
            )

    if prepared_scope_history.scope is None or prepared_scope_history.session is None:
        return PreparedHistoryState(
            compaction_outcomes=prepared_scope_history.compaction_outcomes,
            replay_plan=_configured_replay_plan(
                history_settings=resolved_inputs.history_settings,
                estimated_tokens=0,
            ),
            replays_persisted_history=False,
        )

    current_history_tokens = estimate_prompt_visible_history_tokens(
        session=prepared_scope_history.session,
        scope=prepared_scope_history.scope,
        history_settings=resolved_inputs.history_settings,
    )
    if history_budget is not None:
        replay_plan = plan_replay_that_fits(
            session=prepared_scope_history.session,
            scope=prepared_scope_history.scope,
            history_settings=resolved_inputs.history_settings,
            available_history_budget=history_budget,
        )
        _log_replay_plan(
            replay_plan=replay_plan,
            scope=prepared_scope_history.scope,
            available_history_budget=history_budget,
            current_history_tokens=current_history_tokens,
        )
    else:
        replay_plan = _configured_replay_plan(
            history_settings=resolved_inputs.history_settings,
            estimated_tokens=current_history_tokens,
        )

    return PreparedHistoryState(
        compaction_outcomes=prepared_scope_history.compaction_outcomes,
        replay_plan=replay_plan,
        replays_persisted_history=_has_effective_persisted_replay(
            session=prepared_scope_history.session,
            scope=prepared_scope_history.scope,
            replay_plan=replay_plan,
        ),
    )


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
    execution_plan: ResolvedHistoryExecutionPlan | None = None,
) -> PreparedHistoryState:
    """Prepare one scope by compacting durable history and planning safe replay for the run."""
    resolved_scope = scope or resolve_history_scope(agent)
    if storage is not None and resolved_scope is not None and session_id is not None:
        persisted_session = session
        if persisted_session is None:
            persisted_session = (
                get_team_session(storage, session_id)
                if resolved_scope.kind == "team"
                else get_agent_session(storage, session_id)
            )
        scope_context: ScopeSessionContext | None = ScopeSessionContext(
            scope=resolved_scope,
            storage=storage,
            session=persisted_session,
        )
        prepared_scope_history = await prepare_scope_history(
            agent=agent,
            agent_name=agent_name,
            full_prompt=full_prompt,
            runtime_paths=runtime_paths,
            config=config,
            compaction_outcomes_collector=compaction_outcomes_collector,
            scope_context=scope_context,
            history_settings=history_settings,
            compaction_config=compaction_config,
            has_authored_compaction_config=has_authored_compaction_config,
            active_model_name=active_model_name,
            active_context_window=active_context_window,
            static_prompt_tokens=static_prompt_tokens,
            available_history_budget=available_history_budget,
            scope=resolved_scope,
            execution_plan=execution_plan,
        )
    else:
        with open_scope_session_context(
            agent=agent,
            agent_name=agent_name,
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            scope=resolved_scope,
        ) as scope_context:
            prepared_scope_history = await prepare_scope_history(
                agent=agent,
                agent_name=agent_name,
                full_prompt=full_prompt,
                runtime_paths=runtime_paths,
                config=config,
                compaction_outcomes_collector=compaction_outcomes_collector,
                scope_context=scope_context,
                history_settings=history_settings,
                compaction_config=compaction_config,
                has_authored_compaction_config=has_authored_compaction_config,
                active_model_name=active_model_name,
                active_context_window=active_context_window,
                static_prompt_tokens=static_prompt_tokens,
                available_history_budget=available_history_budget,
                scope=resolved_scope,
                execution_plan=execution_plan,
            )
    return finalize_history_preparation(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=static_prompt_tokens,
        available_history_budget=available_history_budget,
    )


@timed("system_prompt_assembly.history_prepare.scope_history")
async def prepare_bound_scope_history(
    *,
    agents: list[Agent],
    team: Team | None = None,
    full_prompt: str,
    fallback_full_prompt: str | None = None,
    runtime_paths: RuntimePaths,
    config: Config,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    scope_context: ScopeSessionContext | None = None,
    team_name: str | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
) -> PreparedScopeHistory:
    """Prepare one team-owned scope by compacting its persisted session before the run."""
    bound_scope = resolve_bound_team_scope_context(
        agents=agents,
        config=config,
        team_name=team_name,
    )
    if bound_scope is None:
        resolved_inputs = _resolve_entity_preparation_inputs(
            config=config,
            entity_name=team_name if team_name in config.teams else None,
            static_prompt_tokens=(
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
            ),
            active_model_name=active_model_name,
            active_context_window=active_context_window,
        )
        return PreparedScopeHistory(
            scope=None,
            session=None,
            resolved_inputs=resolved_inputs,
        )

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
    resolved_inputs = _resolve_entity_preparation_inputs(
        config=config,
        entity_name=team_name if team_name in config.teams else None,
        static_prompt_tokens=static_prompt_tokens,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    available_history_budget = resolved_inputs.execution_plan.replay_budget_tokens

    return await prepare_scope_history(
        agent=bound_scope.owner_agent,
        agent_name=bound_scope.owner_agent_name,
        full_prompt=full_prompt,
        runtime_paths=runtime_paths,
        config=config,
        compaction_outcomes_collector=compaction_outcomes_collector,
        scope_context=scope_context,
        history_settings=resolved_inputs.history_settings,
        compaction_config=resolved_inputs.compaction_config,
        has_authored_compaction_config=resolved_inputs.execution_plan.authored_compaction_config,
        active_model_name=resolved_inputs.active_model_name,
        active_context_window=resolved_inputs.active_context_window,
        static_prompt_tokens=static_prompt_tokens,
        available_history_budget=available_history_budget,
        scope=bound_scope.scope,
        execution_plan=resolved_inputs.execution_plan,
    )


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
    config: Config,
    team_name: str | None = None,
) -> BoundTeamScopeContext | None:
    """Resolve the stable owner and scope backing one live team run."""
    owner_agent, owner_agent_name = resolve_bound_history_owner(agents)
    if owner_agent is None or owner_agent_name is None:
        return None

    team_scope_id = team_name if team_name is not None and team_name in config.teams else _ad_hoc_team_scope_id(agents)
    if team_scope_id is None:
        return None
    scope = HistoryScope(kind="team", scope_id=team_scope_id)
    return BoundTeamScopeContext(
        owner_agent=owner_agent,
        owner_agent_name=owner_agent_name,
        scope=scope,
    )


def estimate_preparation_static_tokens(
    agent: Agent,
    *,
    full_prompt: str,
    fallback_full_prompt: str | None = None,
) -> int:
    """Estimate static prompt tokens using the largest prompt variant this run may send."""
    primary_tokens = estimate_agent_static_tokens(agent, full_prompt)
    if fallback_full_prompt is None:
        return primary_tokens
    return max(primary_tokens, estimate_agent_static_tokens(agent, fallback_full_prompt))


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


@contextmanager
def open_scope_storage(
    *,
    agent_name: str,
    scope: HistoryScope,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> Iterator[SqliteDb]:
    """Open the canonical SQLite storage for one persisted history scope."""
    storage = create_scope_session_storage(
        agent_name=agent_name,
        scope=scope,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
    )
    try:
        yield storage
    finally:
        storage.close()


def _build_scope_session_context(
    *,
    scope: HistoryScope | None,
    session_id: str | None,
    storage: SqliteDb,
    create_session_if_missing: bool = False,
) -> ScopeSessionContext | None:
    """Build one scope/session context from an already-open storage handle."""
    if session_id is None or scope is None:
        return None

    session = get_team_session(storage, session_id) if scope.kind == "team" else get_agent_session(storage, session_id)
    if session is None and create_session_if_missing:
        created_at = int(datetime.now(UTC).timestamp())
        if scope.kind == "team":
            session = TeamSession(
                session_id=session_id,
                team_id=scope.scope_id,
                metadata={},
                runs=[],
                created_at=created_at,
                updated_at=created_at,
            )
        else:
            session = AgentSession(
                session_id=session_id,
                agent_id=_scope_session_agent_id(scope),
                metadata={},
                runs=[],
                created_at=created_at,
                updated_at=created_at,
            )
    return ScopeSessionContext(
        scope=scope,
        storage=storage,
        session=session,
    )


@contextmanager
def open_resolved_scope_session_context(
    *,
    agent_name: str,
    scope: HistoryScope | None,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open one already-resolved persisted history scope for the current request."""
    if session_id is None:
        yield None
        return
    if scope is None:
        yield None
        return
    with open_scope_storage(
        agent_name=agent_name,
        scope=scope,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
    ) as storage:
        yield _build_scope_session_context(
            scope=scope,
            session_id=session_id,
            storage=storage,
            create_session_if_missing=create_session_if_missing,
        )


@contextmanager
def open_scope_session_context(
    *,
    agent: Agent,
    agent_name: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    scope: HistoryScope | None = None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open the canonical persisted history scope for one live agent."""
    resolved_scope = scope or resolve_history_scope(agent)
    with open_resolved_scope_session_context(
        agent_name=agent_name,
        scope=resolved_scope,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        create_session_if_missing=create_session_if_missing,
    ) as scope_context:
        yield scope_context


@contextmanager
def open_bound_scope_session_context(
    *,
    agents: list[Agent],
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    team_name: str | None = None,
    create_session_if_missing: bool = False,
) -> Iterator[ScopeSessionContext | None]:
    """Open the canonical scope-backed session context for one bound team run."""
    if not agents and team_name is not None and team_name in config.teams:
        with open_resolved_scope_session_context(
            agent_name=team_name,
            scope=HistoryScope(kind="team", scope_id=team_name),
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            create_session_if_missing=create_session_if_missing,
        ) as scope_context:
            yield scope_context
        return

    bound_scope = resolve_bound_team_scope_context(
        agents=agents,
        config=config,
        team_name=team_name,
    )
    if bound_scope is None:
        yield None
        return
    with open_resolved_scope_session_context(
        agent_name=bound_scope.owner_agent_name,
        scope=bound_scope.scope,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        create_session_if_missing=create_session_if_missing,
    ) as scope_context:
        yield scope_context


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


def close_unique_sqlite_dbs(*storages: SqliteDb | None) -> None:
    """Close each distinct SQLite handle at most once."""
    seen: set[int] = set()
    for storage in storages:
        if storage is None:
            continue
        storage_id = id(storage)
        if storage_id in seen:
            continue
        seen.add(storage_id)
        storage.close()


def close_agent_runtime_sqlite_dbs(
    agent: Agent | None,
    *,
    shared_scope_storage: SqliteDb | None = None,
) -> None:
    """Close one agent's runtime-owned SQLite handles except a shared scope storage."""
    if agent is None:
        return
    close_unique_sqlite_dbs(
        *(storage for storage in get_agent_runtime_sqlite_dbs(agent) if storage is not shared_scope_storage),
    )


def close_team_runtime_sqlite_dbs(
    *,
    agents: list[Agent],
    team_db: SqliteDb | None,
    shared_scope_storage: SqliteDb | None = None,
) -> None:
    """Close all runtime-owned SQLite handles for one team request."""
    close_unique_sqlite_dbs(
        *(
            storage
            for agent in agents
            for storage in get_agent_runtime_sqlite_dbs(agent)
            if storage is not shared_scope_storage
        ),
        team_db if team_db is not shared_scope_storage else None,
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


def _resolve_entity_preparation_inputs(
    *,
    config: Config,
    entity_name: str | None,
    static_prompt_tokens: int,
    active_model_name: str | None,
    active_context_window: int | None,
    history_settings: ResolvedHistorySettings | None = None,
    compaction_config: CompactionConfig | None = None,
    has_authored_compaction_config: bool | None = None,
    execution_plan: ResolvedHistoryExecutionPlan | None = None,
) -> _ResolvedPreparationInputs:
    resolved_history_settings = history_settings
    if resolved_history_settings is None:
        resolved_history_settings = (
            config.get_entity_history_settings(entity_name)
            if entity_name is not None
            else config.get_default_history_settings()
        )

    resolved_compaction_config = compaction_config
    if resolved_compaction_config is None:
        resolved_compaction_config = (
            config.get_entity_compaction_config(entity_name)
            if entity_name is not None
            else config.get_default_compaction_config()
        )

    resolved_has_authored_compaction_config = has_authored_compaction_config
    if resolved_has_authored_compaction_config is None:
        resolved_has_authored_compaction_config = (
            config.has_authored_entity_compaction_config(entity_name)
            if entity_name is not None
            else config.has_authored_default_compaction_config()
        )

    runtime_model = config.resolve_runtime_model(
        entity_name=entity_name,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
    )
    resolved_execution_plan = execution_plan or resolve_history_execution_plan(
        config=config,
        compaction_config=resolved_compaction_config,
        has_authored_compaction_config=resolved_has_authored_compaction_config,
        active_model_name=runtime_model.model_name,
        active_context_window=runtime_model.context_window,
        static_prompt_tokens=static_prompt_tokens,
    )

    return _ResolvedPreparationInputs(
        history_settings=resolved_history_settings,
        compaction_config=resolved_compaction_config,
        has_authored_compaction_config=resolved_has_authored_compaction_config,
        active_model_name=runtime_model.model_name,
        active_context_window=runtime_model.context_window,
        static_prompt_tokens=static_prompt_tokens,
        execution_plan=resolved_execution_plan,
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
    execution_plan: ResolvedHistoryExecutionPlan | None,
) -> _ResolvedPreparationInputs:
    resolved_static_prompt_tokens = static_prompt_tokens
    if resolved_static_prompt_tokens is None:
        resolved_static_prompt_tokens = estimate_agent_static_tokens(agent, full_prompt)
    resolved_history_settings = history_settings
    if resolved_history_settings is None and agent_name not in config.agents:
        resolved_history_settings = _history_settings_from_agent(agent)
    return _resolve_entity_preparation_inputs(
        config=config,
        entity_name=agent_name if agent_name in config.agents else None,
        static_prompt_tokens=resolved_static_prompt_tokens,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
        history_settings=resolved_history_settings,
        compaction_config=compaction_config,
        has_authored_compaction_config=has_authored_compaction_config,
        execution_plan=execution_plan,
    )


def _prepare_scope_state_for_run(
    *,
    storage: SqliteDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    execution_plan: ResolvedHistoryExecutionPlan,
) -> HistoryScopeState:
    state = read_scope_state(session, scope)
    if consume_pending_force_compaction_scope(session, scope):
        state = replace(state, force_compact_before_next_run=True)
        write_scope_state(session, scope, state)
        storage.upsert_session(session)
    if state.force_compact_before_next_run and not execution_plan.destructive_compaction_available:
        state = clear_force_compaction_state(session, scope, state)
        storage.upsert_session(session)
        description = describe_compaction_unavailability(execution_plan)
        logger.warning(
            "Forced compaction skipped because destructive compaction is unavailable",
            session_id=session.session_id,
            scope=scope.key,
            reason=description,
        )
    return state


def plan_replay_that_fits(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
) -> ResolvedReplayPlan:
    """Return the safest persisted-replay plan that fits the current run budget."""
    current_tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    if current_tokens <= available_history_budget:
        return _configured_replay_plan(
            history_settings=history_settings,
            estimated_tokens=current_tokens,
        )

    limit_mode, max_limit = _context_window_guard_limit_bounds(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    fitting_limit, fitting_tokens = _find_fitting_history_limit_for_budget(
        session=session,
        scope=scope,
        history_settings=history_settings,
        available_history_budget=available_history_budget,
        limit_mode=limit_mode,
        max_limit=max_limit,
    )
    if fitting_limit > 0:
        num_history_runs, num_history_messages = _history_limit_fields(limit_mode, fitting_limit)
        return ResolvedReplayPlan(
            mode="limited",
            estimated_tokens=fitting_tokens,
            add_history_to_context=True,
            num_history_runs=num_history_runs,
            num_history_messages=num_history_messages,
            history_limit_mode=limit_mode,
            history_limit=fitting_limit,
        )

    return ResolvedReplayPlan(
        mode="disabled",
        estimated_tokens=0,
        add_history_to_context=False,
    )


def apply_replay_plan(
    *,
    target: Agent | Team,
    replay_plan: ResolvedReplayPlan,
) -> None:
    """Apply one resolved persisted-replay plan to a live Agent or Team."""
    target.add_history_to_context = replay_plan.add_history_to_context
    target.num_history_runs = replay_plan.num_history_runs
    target.num_history_messages = replay_plan.num_history_messages


def _context_window_guard_limit_bounds(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> tuple[Literal["runs", "messages"], int]:
    configured_limit = history_settings.policy.limit or 0
    if history_settings.policy.mode == "messages":
        return "messages", configured_limit

    visible_run_count = len(runs_for_scope(completed_top_level_runs(session), scope))
    if history_settings.policy.mode == "all":
        return "runs", visible_run_count
    return "runs", min(configured_limit, visible_run_count)


def _find_fitting_history_limit_for_budget(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
    available_history_budget: int,
    limit_mode: Literal["runs", "messages"],
    max_limit: int,
) -> tuple[int, int]:
    if max_limit <= 0 or available_history_budget <= 0:
        return 0, 0

    low = 1
    high = max_limit
    best = 0
    best_tokens = 0
    while low <= high:
        mid = (low + high) // 2
        candidate_tokens = estimate_prompt_visible_history_tokens(
            session=session,
            scope=scope,
            history_settings=_history_settings_with_limit(
                history_settings,
                mode=limit_mode,
                limit=mid,
            ),
        )
        if candidate_tokens <= available_history_budget:
            best = mid
            best_tokens = candidate_tokens
            low = mid + 1
        else:
            high = mid - 1
    return best, best_tokens


def _log_replay_plan(
    *,
    replay_plan: ResolvedReplayPlan,
    scope: HistoryScope,
    available_history_budget: int,
    current_history_tokens: int,
) -> None:
    if replay_plan.mode == "configured":
        return

    if replay_plan.mode == "limited":
        logger.warning(
            "Replay planner reduced persisted replay for this run",
            scope=scope.key,
            limit_mode=replay_plan.history_limit_mode,
            new_limit=replay_plan.history_limit,
            estimated_tokens=current_history_tokens,
            fitted_tokens=replay_plan.estimated_tokens,
            available_history_budget=available_history_budget,
        )
        return

    logger.warning(
        "Replay planner disabled raw persisted replay for this run",
        scope=scope.key,
        estimated_tokens=current_history_tokens,
        fitted_tokens=replay_plan.estimated_tokens,
        available_history_budget=available_history_budget,
    )


def _configured_replay_plan(
    *,
    history_settings: ResolvedHistorySettings,
    estimated_tokens: int,
) -> ResolvedReplayPlan:
    num_history_runs, num_history_messages = _history_limit_fields(
        history_settings.policy.mode,
        history_settings.policy.limit,
    )
    return ResolvedReplayPlan(
        mode="configured",
        estimated_tokens=estimated_tokens,
        add_history_to_context=True,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
    )


def _history_settings_with_limit(
    history_settings: ResolvedHistorySettings,
    *,
    mode: Literal["runs", "messages"],
    limit: int,
) -> ResolvedHistorySettings:
    return ResolvedHistorySettings(
        policy=HistoryPolicy(mode=mode, limit=limit),
        max_tool_calls_from_history=history_settings.max_tool_calls_from_history,
        system_message_role=history_settings.system_message_role,
        skip_history_system_role=history_settings.skip_history_system_role,
    )


def _history_limit_fields(
    mode: Literal["all", "runs", "messages"],
    limit: int | None,
) -> tuple[int | None, int | None]:
    if mode == "runs":
        return limit, None
    if mode == "messages":
        return None, limit
    return None, None


def _has_effective_persisted_replay(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    replay_plan: ResolvedReplayPlan,
) -> bool:
    return replay_plan.add_history_to_context and bool(
        runs_for_scope(completed_top_level_runs(session), scope),
    )
