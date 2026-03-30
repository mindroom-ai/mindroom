"""Runtime helpers for compaction-aware history preparation.

This module keeps the compaction engine in ``compaction.py`` and the response
entrypoints in ``ai.py`` while centralizing the Agno-specific session/history
bookkeeping that runs immediately before and after an agent execution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agents import _get_agent_session, create_session_storage
from mindroom.compaction import (
    CompactionOutcome,
    PendingCompaction,
    apply_pending_compaction,
    clear_pending_compaction,
    compact_session_now,
    estimate_history_tokens,
    estimate_static_tokens,
    find_fitting_run_limit,
    get_last_compacted_run_id,
    get_replayable_runs,
)
from mindroom.logging_config import get_logger
from mindroom.token_budget import estimate_text_tokens

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.session.agent import AgentSession

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_BOUND_AGENT_NAME_ATTR = "_mindroom_requested_agent_name"
_BOUND_PENDING_COMPACTION_BUFFER_ATTR = "_mindroom_pending_compaction_buffer"


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


def _apply_compaction_cutoff_to_history(agent: Agent, session: AgentSession | None) -> None:
    """Restrict Agno history replay to runs that remain visible after compaction."""
    if session is None or get_last_compacted_run_id(session) is None:
        return

    visible_run_count = len(get_replayable_runs(session, agent))
    if visible_run_count <= 0:
        agent.add_history_to_context = False
        return

    current_limit = agent.num_history_runs
    if current_limit is None or current_limit > visible_run_count:
        agent.num_history_runs = visible_run_count


def _apply_context_window_limit(  # noqa: C901
    agent: Agent,
    agent_name: str,
    config: Config,
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    session: AgentSession | None = None,
) -> None:
    """Reduce ``agent.num_history_runs`` when estimated context approaches the window."""
    if agent.num_history_messages is not None or not session_id:
        return

    model_name = config.get_entity_model_name(agent_name)
    model_config = config.models.get(model_name)
    if model_config is None or model_config.context_window is None:
        return

    context_window = model_config.context_window
    threshold = int(context_window * 0.8)
    static_tokens = estimate_static_tokens(agent, full_prompt)

    if session is None:
        storage = create_session_storage(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
        session = _get_agent_session(storage, session_id)
    if not session or not session.runs:
        return

    replayable_runs = get_replayable_runs(session, agent)
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


async def _apply_manual_compaction_if_queued(
    *,
    pending_compaction_buffer: list[PendingCompaction] | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None,
) -> None:
    manual_outcome = await apply_pending_compaction(
        pending_override=_latest_pending_compaction(pending_compaction_buffer),
    )
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
        session = _get_agent_session(storage, session_id)
    return storage, session


def _estimate_session_summary_tokens(agent: Agent, session: AgentSession) -> int:
    """Estimate tokens contributed by the stored Agno session summary."""
    if not agent.add_session_summary_to_context or session.summary is None:
        return 0
    return estimate_text_tokens(session.summary.summary)


def _resolve_effective_compaction_threshold(compaction_config: CompactionConfig, context_window: int) -> int:
    """Resolve the absolute token threshold that should trigger auto-compaction."""
    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is not None:
        return threshold_tokens
    threshold_percent = compaction_config.threshold_percent
    if threshold_percent is not None:
        return int(context_window * threshold_percent)
    return int(context_window * 0.8)


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
) -> AgentSession | None:
    """Apply configured auto-compaction before an agent run when the session is near budget."""
    if session_id is None or storage is None or session is None or not _session_has_prior_runs(session):
        return session
    if agent_name not in config.agents:
        return session

    compaction_config = config.get_agent_compaction_config(agent_name)
    if not config.has_authored_agent_compaction_config(agent_name) or not compaction_config.enabled:
        return session

    model_name = config.get_entity_model_name(agent_name)
    model_config = config.models.get(model_name)
    context_window = model_config.context_window if model_config is not None else None
    if context_window is None:
        return session

    history_run_limit = agent.num_history_runs if agent.num_history_runs and agent.num_history_runs > 0 else None
    history_message_limit = (
        agent.num_history_messages
        if agent.num_history_messages is not None and agent.num_history_messages > 0
        else None
    )
    static_tokens = estimate_static_tokens(agent, full_prompt)
    history_tokens = estimate_history_tokens(
        session,
        agent,
        history_run_limit,
        message_limit=history_message_limit,
    )
    summary_tokens = _estimate_session_summary_tokens(agent, session)
    effective_threshold = _resolve_effective_compaction_threshold(compaction_config, context_window)
    effective_reserve = min(compaction_config.reserve_tokens, context_window // 2)
    effective_keep_recent = min(compaction_config.keep_recent_tokens, context_window // 2)
    target_ceiling = min(
        effective_threshold,
        max(0, context_window - effective_reserve),
    )
    if static_tokens + history_tokens + summary_tokens <= target_ceiling or target_ceiling < 0:
        return session

    try:
        from mindroom.ai import get_model_instance  # noqa: PLC0415

        summary_model_name = compaction_config.model or model_name
        summary_model = get_model_instance(config, runtime_paths, summary_model_name)
        summary_model_config = config.models.get(summary_model_name)
        compaction_model_context_window = (
            summary_model_config.context_window
            if summary_model_config and summary_model_config.context_window
            else None
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
    )

    _apply_compaction_cutoff_to_history(agent, session)
    _apply_context_window_limit(
        agent,
        agent_name,
        config,
        full_prompt,
        session_id,
        runtime_paths,
        execution_identity=execution_identity,
        session=session,
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
        await _apply_manual_compaction_if_queued(
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
