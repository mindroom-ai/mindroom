"""Runtime helpers for compaction-aware history preparation.

This module keeps the compaction engine in ``compaction.py`` and the response
entrypoints in ``ai.py`` while centralizing the Agno-specific session/history
bookkeeping that runs immediately before and after an agent execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MethodType
from typing import TYPE_CHECKING, Literal

from agno.session.agent import AgentSession

from mindroom.agents import create_session_storage, get_agent_session
from mindroom.compaction import (
    CompactionOutcome,
    PendingCompaction,
    apply_pending_compaction,
    clear_pending_compaction,
    compact_session_now,
    estimate_history_tokens,
    estimate_static_tokens,
    find_fitting_run_limit,
    get_replayable_history_messages,
    resolve_agent_replay_state,
)
from mindroom.logging_config import get_logger
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.run.base import RunStatus

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_BOUND_AGENT_NAME_ATTR = "_mindroom_requested_agent_name"
_BOUND_PENDING_COMPACTION_BUFFER_ATTR = "_mindroom_pending_compaction_buffer"
_HistoryReplayMode = Literal["all", "runs", "messages"]


@dataclass(frozen=True)
class _HistoryReplayPlan:
    """Effective history replay contract for one prepared run."""

    source_mode: _HistoryReplayMode
    add_history_to_context: bool
    add_session_summary_to_context: bool
    num_history_runs: int | None
    num_history_messages: int | None


def _current_history_replay_mode(agent: Agent) -> _HistoryReplayMode:
    """Return the configured history replay mode for one live agent."""
    if agent.num_history_messages is not None:
        return "messages"
    if agent.num_history_runs is not None:
        return "runs"
    return "all"


def _current_history_replay_plan(agent: Agent) -> _HistoryReplayPlan:
    """Return the live agent's current replay configuration as a plan."""
    return _HistoryReplayPlan(
        source_mode=_current_history_replay_mode(agent),
        add_history_to_context=agent.add_history_to_context,
        add_session_summary_to_context=bool(agent.add_session_summary_to_context),
        num_history_runs=agent.num_history_runs,
        num_history_messages=agent.num_history_messages,
    )


def _summary_only_history_replay_plan(source_mode: _HistoryReplayMode) -> _HistoryReplayPlan:
    """Return a plan that replays only the persisted summary."""
    return _HistoryReplayPlan(
        source_mode=source_mode,
        add_history_to_context=False,
        add_session_summary_to_context=True,
        num_history_runs=None,
        num_history_messages=None,
    )


def _build_history_replay_plan(
    agent: Agent,
    session: AgentSession | None,
) -> _HistoryReplayPlan:
    """Resolve the effective history replay plan for the current stored session."""
    current_plan = _current_history_replay_plan(agent)
    if session is None:
        return current_plan

    replay_state = resolve_agent_replay_state(session, agent)
    if replay_state.last_compacted_run_id is None:
        return current_plan
    if replay_state.summary is None:
        logger.warning(
            "Skipping persisted compaction cutoff without a stored summary",
            session_id=session.session_id,
        )
        return current_plan

    source_mode = current_plan.source_mode
    visible_run_count = len(replay_state.visible_runs)
    if visible_run_count <= 0:
        return _summary_only_history_replay_plan(source_mode)

    return _HistoryReplayPlan(
        source_mode=source_mode,
        add_history_to_context=current_plan.add_history_to_context,
        add_session_summary_to_context=True,
        num_history_runs=current_plan.num_history_runs,
        num_history_messages=current_plan.num_history_messages,
    )


def _apply_history_replay_plan(agent: Agent, plan: _HistoryReplayPlan) -> None:
    """Project one replay plan onto the live Agno agent instance."""
    agent.add_history_to_context = plan.add_history_to_context
    agent.add_session_summary_to_context = plan.add_session_summary_to_context
    agent.num_history_runs = plan.num_history_runs
    agent.num_history_messages = plan.num_history_messages


def _bind_replayable_history_session(
    agent: Agent,
    session: AgentSession | None,
    replay_plan: _HistoryReplayPlan,
) -> None:
    """Force the live run to read history from the replayable post-cutoff slice."""
    if session is None:
        return

    replay_state = resolve_agent_replay_state(session, agent)
    if (
        replay_state.last_compacted_run_id is None
        or replay_state.summary is None
        or not replay_plan.add_history_to_context
    ):
        return

    def _get_messages_override(
        self_session: AgentSession,
        agent_id: str | None = None,
        team_id: str | None = None,
        last_n_runs: int | None = None,
        limit: int | None = None,
        skip_roles: list[str] | None = None,
        skip_statuses: list[RunStatus] | None = None,
        skip_history_messages: bool = True,
    ) -> list[Message]:
        if not skip_history_messages or skip_statuses is not None:
            return AgentSession.get_messages(
                self_session,
                agent_id=agent_id,
                team_id=team_id,
                last_n_runs=last_n_runs,
                limit=limit,
                skip_roles=skip_roles,
                skip_statuses=skip_statuses,
                skip_history_messages=skip_history_messages,
            )
        run_limit = None if limit is not None else last_n_runs
        return get_replayable_history_messages(
            self_session,
            agent,
            run_limit,
            message_limit=limit,
            skip_roles=skip_roles,
        )

    session.__dict__["get_messages"] = MethodType(_get_messages_override, session)
    agent.__dict__["_cached_session"] = session


def _disable_history_for_run(
    agent: Agent,
    *,
    reason: str,
    agent_name: str,
    context_window: int,
    threshold: int,
) -> None:
    """Disable history context for this run when no safe history budget remains."""
    if not agent.add_history_to_context:
        return
    agent.add_history_to_context = False
    logger.warning(
        "Context window limit approaching, disabling history for this run",
        agent=agent_name,
        reason=reason,
        context_window=context_window,
        threshold=threshold,
    )


def _resolve_history_budget_target(
    config: Config,
    agent_name: str,
    context_window: int,
) -> int:
    """Resolve the prompt-history ceiling for one run."""
    default_threshold = int(context_window * 0.8)
    if agent_name not in config.agents:
        return default_threshold

    compaction_config = config.get_agent_compaction_config(agent_name)
    if not config.has_authored_agent_compaction_config(agent_name) or not compaction_config.enabled:
        return default_threshold

    reserve_tokens = normalize_compaction_budget_tokens(compaction_config.reserve_tokens, context_window)
    threshold_tokens = resolve_effective_compaction_threshold(compaction_config, context_window)
    return min(threshold_tokens, max(0, context_window - reserve_tokens))


def _apply_context_window_limit(  # noqa: C901
    agent: Agent,
    agent_name: str,
    config: Config,
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    session: AgentSession | None = None,
    history_mode: _HistoryReplayMode | None = None,
) -> None:
    """Reduce ``agent.num_history_runs`` when estimated context approaches the window."""
    if history_mode == "messages" or agent.num_history_messages is not None or not session_id:
        return

    model_name = config.get_entity_model_name(agent_name)
    model_config = config.models.get(model_name)
    if model_config is None or model_config.context_window is None:
        return

    context_window = model_config.context_window
    threshold = _resolve_history_budget_target(config, agent_name, context_window)
    static_tokens = estimate_static_tokens(agent, full_prompt)

    if session is None:
        storage = create_session_storage(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
        session = get_agent_session(storage, session_id)
    if not session or not session.runs:
        return

    replayable_runs = resolve_agent_replay_state(session, agent).visible_runs
    if not replayable_runs:
        return

    current_limit = agent.num_history_runs
    max_considered_runs = (
        min(current_limit, len(replayable_runs))
        if current_limit is not None and current_limit > 0
        else len(replayable_runs)
    )
    initial_run_limit = max_considered_runs if current_limit is not None and current_limit > 0 else None
    history_tokens = estimate_history_tokens(session, agent, initial_run_limit)
    summary_tokens = 0
    if agent.add_session_summary_to_context and session.summary is not None:
        summary_tokens = estimate_text_tokens(session.summary.summary)
    total_tokens = static_tokens + history_tokens + summary_tokens
    if total_tokens <= threshold:
        return

    original = current_limit if current_limit is not None else len(replayable_runs)
    budget = threshold - static_tokens - summary_tokens
    if budget <= 0:
        new_limit = 0
        if static_tokens + summary_tokens > threshold and summary_tokens > 0:
            agent.add_session_summary_to_context = False
            logger.warning(
                "Session summary exceeds context budget, disabling for this run",
                agent=agent_name,
                summary_tokens=summary_tokens,
                static_tokens=static_tokens,
                context_window=context_window,
                threshold=threshold,
            )
        reason = "no_history_budget"
    else:
        new_limit = find_fitting_run_limit(session, agent, max_considered_runs, budget)
        reason = "history_exceeds_budget"

    if new_limit == 0:
        _disable_history_for_run(
            agent,
            reason=reason,
            agent_name=agent_name,
            context_window=context_window,
            threshold=threshold,
        )
        return

    if new_limit < original:
        agent.num_history_runs = new_limit
        logger.warning(
            "Context window limit approaching, reducing history",
            agent=agent_name,
            original_runs=original,
            reduced_runs=new_limit,
            estimated_tokens=total_tokens,
            context_window=context_window,
            threshold=threshold,
        )


def _latest_pending_compaction(
    pending_compaction_buffer: list[PendingCompaction] | None,
) -> PendingCompaction | None:
    if pending_compaction_buffer:
        return pending_compaction_buffer[-1]
    return None


async def apply_manual_compaction_if_queued(
    *,
    pending_compaction_buffer: list[PendingCompaction] | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None,
) -> None:
    """Apply the newest queued manual compaction and record its outcome."""
    manual_outcome = await apply_pending_compaction(_latest_pending_compaction(pending_compaction_buffer))
    if pending_compaction_buffer is not None:
        pending_compaction_buffer.clear()
    if manual_outcome is not None and compaction_outcomes_collector is not None:
        compaction_outcomes_collector.append(manual_outcome)


def bind_agent_compaction_state(
    agent: Agent,
    *,
    agent_name: str,
    pending_compaction_buffer: list[PendingCompaction] | None = None,
) -> list[PendingCompaction]:
    """Attach per-run compaction bookkeeping to one materialized agent instance."""
    bound_buffer = pending_compaction_buffer if pending_compaction_buffer is not None else []
    agent.__dict__[_BOUND_AGENT_NAME_ATTR] = agent_name
    agent.__dict__[_BOUND_PENDING_COMPACTION_BUFFER_ATTR] = bound_buffer
    return bound_buffer


def _get_bound_agent_compaction_state(
    agent: Agent,
) -> tuple[str | None, list[PendingCompaction] | None]:
    agent_name = agent.__dict__.get(_BOUND_AGENT_NAME_ATTR)
    pending_compaction_buffer = agent.__dict__.get(_BOUND_PENDING_COMPACTION_BUFFER_ATTR)
    return (
        agent_name if isinstance(agent_name, str) else None,
        pending_compaction_buffer if isinstance(pending_compaction_buffer, list) else None,
    )


def _session_has_prior_runs(session: AgentSession | None) -> bool:
    """Return whether the stored Agno session has replayable history."""
    return session is not None and (bool(session.runs) or session.summary is not None)


def _ensure_agent_storage_and_session(
    *,
    agent_name: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None = None,
    storage: SqliteDb | None = None,
    session: AgentSession | None = None,
) -> tuple[SqliteDb | None, AgentSession | None]:
    """Materialize the session storage and current stored session when needed."""
    if session_id is None:
        return storage, session
    if storage is None:
        storage = create_session_storage(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
    if session is None:
        session = get_agent_session(storage, session_id)
    return storage, session


def resolve_effective_compaction_threshold(compaction_config: CompactionConfig, context_window: int) -> int:
    """Resolve the absolute token threshold that should trigger auto-compaction."""
    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is not None:
        return threshold_tokens
    threshold_percent = compaction_config.threshold_percent
    if threshold_percent is not None:
        return int(context_window * threshold_percent)
    return int(context_window * 0.8)


def normalize_compaction_budget_tokens(tokens: int, context_window: int | None) -> int:
    """Clamp one compaction budget knob against half of the available window."""
    if context_window is None or context_window <= 0:
        return tokens
    return min(tokens, context_window // 2)


def resolve_compaction_model(
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
    compaction_config: CompactionConfig,
) -> tuple[Model, int | None]:
    """Resolve the compaction model instance and its context window."""
    from mindroom.ai import get_model_instance  # noqa: PLC0415

    model_name = compaction_config.model or config.get_entity_model_name(agent_name)
    model = get_model_instance(config, runtime_paths, model_name)
    model_config = config.models.get(model_name)
    context_window = model_config.context_window if model_config and model_config.context_window else None
    return model, context_window


def _estimate_replay_plan_tokens(
    *,
    session: AgentSession,
    agent: Agent,
    full_prompt: str,
    replay_plan: _HistoryReplayPlan,
) -> tuple[int, int, int]:
    """Estimate static, history, and summary tokens for one replay plan."""
    static_tokens = estimate_static_tokens(agent, full_prompt)
    history_run_limit = (
        replay_plan.num_history_runs if replay_plan.num_history_runs and replay_plan.num_history_runs > 0 else None
    )
    history_message_limit = (
        replay_plan.num_history_messages
        if replay_plan.num_history_messages is not None and replay_plan.num_history_messages > 0
        else None
    )
    history_tokens = 0
    if replay_plan.add_history_to_context:
        history_tokens = estimate_history_tokens(
            session,
            agent,
            history_run_limit,
            message_limit=history_message_limit,
        )
    summary_tokens = 0
    if replay_plan.add_session_summary_to_context and session.summary is not None:
        summary_tokens = estimate_text_tokens(session.summary.summary)
    return static_tokens, history_tokens, summary_tokens


def _resolve_auto_compaction_budget_settings(
    *,
    config: Config,
    agent_name: str,
    compaction_config: CompactionConfig,
) -> tuple[int, int, int, int] | None:
    """Return context and budget settings for one auto-compaction decision."""
    model_name = config.get_entity_model_name(agent_name)
    model_config = config.models.get(model_name)
    context_window = model_config.context_window if model_config is not None else None
    if context_window is None:
        return None

    compaction_model_name = compaction_config.model or model_name
    compaction_model_config = config.models.get(compaction_model_name)
    compaction_budget_window = (
        compaction_model_config.context_window
        if compaction_model_config and compaction_model_config.context_window
        else context_window
    )
    effective_threshold = resolve_effective_compaction_threshold(compaction_config, context_window)
    effective_reserve = normalize_compaction_budget_tokens(compaction_config.reserve_tokens, compaction_budget_window)
    effective_keep_recent = normalize_compaction_budget_tokens(
        compaction_config.keep_recent_tokens,
        compaction_budget_window,
    )
    return context_window, effective_threshold, effective_reserve, effective_keep_recent


async def _maybe_auto_compact_agent_history(  # noqa: PLR0911
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    storage: SqliteDb | None = None,
    session: AgentSession | None = None,
    replay_plan: _HistoryReplayPlan,
) -> AgentSession | None:
    """Apply configured auto-compaction before an agent run when the session is near budget."""
    if session_id is None or storage is None or session is None or not _session_has_prior_runs(session):
        return session
    if agent_name not in config.agents:
        return session

    compaction_config = config.get_agent_compaction_config(agent_name)
    if not config.has_authored_agent_compaction_config(agent_name) or not compaction_config.enabled:
        return session

    budget_settings = _resolve_auto_compaction_budget_settings(
        config=config,
        agent_name=agent_name,
        compaction_config=compaction_config,
    )
    if budget_settings is None:
        return session
    context_window, effective_threshold, effective_reserve, effective_keep_recent = budget_settings

    static_tokens, history_tokens, summary_tokens = _estimate_replay_plan_tokens(
        session=session,
        agent=agent,
        full_prompt=full_prompt,
        replay_plan=replay_plan,
    )
    target_ceiling = min(
        effective_threshold,
        max(0, context_window - effective_reserve),
    )
    if static_tokens + history_tokens + summary_tokens <= target_ceiling or target_ceiling < 0:
        return session

    try:
        summary_model, compaction_model_context_window = resolve_compaction_model(
            config,
            runtime_paths,
            agent_name,
            compaction_config,
        )
        compaction_result = await compact_session_now(
            storage=storage,
            session_id=session_id,
            agent=agent,
            model=summary_model,
            mode="auto",
            window_tokens=context_window,
            threshold_tokens=effective_threshold,
            reserve_tokens=effective_reserve,
            keep_recent_tokens=effective_keep_recent,
            notify=compaction_config.notify,
            compaction_model_context_window=compaction_model_context_window,
        )
    except Exception:
        logger.exception("Auto-compaction failed, falling back to context window limit")
        return session

    if compaction_result is None:
        return session

    updated_session, outcome = compaction_result
    if compaction_outcomes_collector is not None:
        compaction_outcomes_collector.append(outcome)
    return updated_session


async def prepare_agent_history_for_run(
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    storage: SqliteDb | None = None,
    session: AgentSession | None = None,
) -> AgentSession | None:
    """Apply auto-compaction and dynamic history limiting for one agent run."""
    storage, session = _ensure_agent_storage_and_session(
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        storage=storage,
        session=session,
    )
    replay_plan = _build_history_replay_plan(agent, session)
    session = await _maybe_auto_compact_agent_history(
        agent=agent,
        agent_name=agent_name,
        full_prompt=full_prompt,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        compaction_outcomes_collector=compaction_outcomes_collector,
        storage=storage,
        session=session,
        replay_plan=replay_plan,
    )
    replay_plan = _build_history_replay_plan(agent, session)
    _apply_history_replay_plan(agent, replay_plan)
    _bind_replayable_history_session(agent, session, replay_plan)
    _apply_context_window_limit(
        agent,
        agent_name,
        config,
        full_prompt,
        session_id,
        runtime_paths,
        execution_identity=execution_identity,
        session=session,
        history_mode=replay_plan.source_mode,
    )
    return session


async def prepare_bound_agents_for_run(
    *,
    agents: list[Agent],
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
) -> None:
    """Apply compaction-aware history preparation for bound materialized agents."""
    for agent in agents:
        bound_agent_name, _pending_compaction_buffer = _get_bound_agent_compaction_state(agent)
        if bound_agent_name is None:
            continue
        await prepare_agent_history_for_run(
            agent=agent,
            agent_name=bound_agent_name,
            full_prompt=full_prompt,
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
            compaction_outcomes_collector=compaction_outcomes_collector,
        )


async def apply_bound_agent_compactions(
    *,
    agents: list[Agent],
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
) -> None:
    """Commit queued manual compactions for bound materialized agents."""
    for agent in agents:
        _, pending_compaction_buffer = _get_bound_agent_compaction_state(agent)
        if pending_compaction_buffer is None:
            continue
        await apply_manual_compaction_if_queued(
            pending_compaction_buffer=pending_compaction_buffer,
            compaction_outcomes_collector=compaction_outcomes_collector,
        )


async def stream_with_bound_agent_compactions(
    stream: AsyncIterator[object],
    *,
    agents: list[Agent],
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
) -> AsyncGenerator[object, None]:
    """Yield a stream and commit queued bound-agent compactions when it ends."""
    try:
        async for event in stream:
            yield event
    finally:
        await apply_bound_agent_compactions(
            agents=agents,
            compaction_outcomes_collector=compaction_outcomes_collector,
        )


def clear_bound_agent_compactions(agents: list[Agent]) -> None:
    """Discard queued manual compactions for bound materialized agents."""
    for agent in agents:
        _, pending_compaction_buffer = _get_bound_agent_compaction_state(agent)
        if pending_compaction_buffer is None:
            continue
        clear_pending_compaction(pending_compaction_buffer)
