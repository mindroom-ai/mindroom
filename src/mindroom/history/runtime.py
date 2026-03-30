"""Runtime integration for scoped replay and compaction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MethodType
from typing import TYPE_CHECKING, cast

from agno.run.agent import RunOutput
from agno.run.messages import RunMessages
from agno.session.agent import AgentSession

from mindroom.agents import create_session_storage, get_agent_session
from mindroom.history.compaction import (
    compact_scope_history,
    estimate_static_tokens,
    normalize_compaction_budget_tokens,
    resolve_effective_compaction_threshold,
)
from mindroom.history.replay import (
    apply_oldest_first_drop_policy,
    build_replay_plan,
    digest_prepared_replay,
    is_replay_message,
    resolve_history_scope,
    strip_replay_messages,
)
from mindroom.history.storage import read_scope_state, write_scope_state
from mindroom.history.types import PreparedHistory
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import CompactionOutcome
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_ACTIVE_HISTORY_STATE_ATTR = "_mindroom_active_history_state"


@dataclass
class _ActiveHistoryState:
    original_additional_input: list[object] | None
    original_get_run_messages: object
    original_aget_run_messages: object
    original_start_learning_future: object
    original_astart_learning_task: object
    original_cleanup_and_store: object
    original_acleanup_and_store: object


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
    session: AgentSession | None = None,
) -> PreparedHistory:
    """Prepare persisted replay state for one run and activate Agno guards."""
    clear_prepared_history(agent)

    scope = resolve_history_scope(agent)
    if session_id is None or scope is None:
        return PreparedHistory()

    storage, resolved_session = _materialize_session(
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        storage=storage,
        session=session,
    )
    if storage is None or resolved_session is None:
        return PreparedHistory()

    state = read_scope_state(resolved_session, scope)
    replay_plan = build_replay_plan(
        session=resolved_session,
        agent=agent,
        scope=scope,
        state=state,
    )

    compaction_outcomes: list[CompactionOutcome] = []
    available_history_budget = _resolve_available_history_budget(
        agent=agent,
        agent_name=agent_name,
        full_prompt=full_prompt,
        config=config,
    )
    auto_compaction_enabled = (
        agent_name in config.agents
        and config.has_authored_agent_compaction_config(agent_name)
        and config.get_agent_compaction_config(agent_name).enabled
    )
    should_attempt_compaction = state.force_compact_before_next_run or (
        auto_compaction_enabled
        and available_history_budget is not None
        and replay_plan.replay_tokens > available_history_budget
    )

    if should_attempt_compaction:
        if len(replay_plan.visible_runs) > 2:
            next_state, outcome = await compact_scope_history(
                storage=storage,
                session=resolved_session,
                scope=scope,
                state=state,
                visible_runs=replay_plan.visible_runs,
                agent=agent,
                agent_name=agent_name,
                config=config,
                runtime_paths=runtime_paths,
            )
            if next_state != state and outcome is None:
                write_scope_state(resolved_session, scope, next_state)
                storage.upsert_session(resolved_session)
            if outcome is not None:
                compaction_outcomes.append(outcome)
            state = next_state
            replay_plan = build_replay_plan(
                session=resolved_session,
                agent=agent,
                scope=scope,
                state=state,
            )
        elif state.force_compact_before_next_run:
            cleared_state = replace(state, force_compact_before_next_run=False)
            if cleared_state != state:
                write_scope_state(resolved_session, scope, cleared_state)
                storage.upsert_session(resolved_session)
            state = cleared_state
            replay_plan = build_replay_plan(
                session=resolved_session,
                agent=agent,
                scope=scope,
                state=state,
            )

    replay_plan = apply_oldest_first_drop_policy(
        replay_plan,
        budget_tokens=available_history_budget,
        max_tool_calls_from_history=agent.max_tool_calls_from_history,
    )

    prepared = PreparedHistory(
        summary_prompt_prefix=replay_plan.summary_prompt_prefix,
        history_messages=replay_plan.history_messages,
        cache_key_fragment=digest_prepared_replay(
            replay_plan.summary_prompt_prefix,
            replay_plan.history_messages,
        ),
        compaction_outcomes=compaction_outcomes,
        has_stored_replay_state=replay_plan.has_stored_replay_state,
    )
    if compaction_outcomes_collector is not None:
        compaction_outcomes_collector.extend(compaction_outcomes)
    _activate_prepared_history(agent, prepared.history_messages)
    return prepared


async def prepare_bound_agents_for_run(
    *,
    agents: list[Agent],
    full_prompt: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
) -> PreparedHistory:
    """Prepare persisted history for a team's member agents."""
    prepared_histories: list[PreparedHistory] = []
    for agent in agents:
        agent_id = agent.id
        if not isinstance(agent_id, str) or not agent_id:
            continue
        prepared_histories.append(
            await prepare_history_for_run(
                agent=agent,
                agent_name=agent_id,
                full_prompt=full_prompt,
                session_id=session_id,
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=execution_identity,
                compaction_outcomes_collector=compaction_outcomes_collector,
            ),
        )
    return _merge_bound_prepared_history(prepared_histories)


async def stream_with_bound_agent_history(
    raw_stream: AsyncIterator[object],
    *,
    agents: list[Agent],
) -> AsyncGenerator[object, None]:
    """Yield one team stream while guaranteeing replay cleanup afterwards."""
    try:
        async for event in raw_stream:
            yield event
    finally:
        clear_bound_agent_history_state(agents)


def clear_bound_agent_history_state(agents: list[Agent]) -> None:
    """Clear prepared replay state from a list of bound agents."""
    for agent in agents:
        clear_prepared_history(agent)


def clear_prepared_history(agent: Agent) -> None:
    """Restore one agent's original additional-input/runtime methods."""
    binding = agent.__dict__.pop(_ACTIVE_HISTORY_STATE_ATTR, None)
    if not isinstance(binding, _ActiveHistoryState):
        return
    agent.additional_input = binding.original_additional_input
    agent.__dict__["_get_run_messages"] = binding.original_get_run_messages
    agent.__dict__["_aget_run_messages"] = binding.original_aget_run_messages
    agent.__dict__["_start_learning_future"] = binding.original_start_learning_future
    agent.__dict__["_astart_learning_task"] = binding.original_astart_learning_task
    agent.__dict__["_cleanup_and_store"] = binding.original_cleanup_and_store
    agent.__dict__["_acleanup_and_store"] = binding.original_acleanup_and_store


def compose_prompt_with_persisted_history(
    *,
    base_prompt: str,
    prepared_history: PreparedHistory,
    fallback_prompt: str | None = None,
) -> str:
    """Compose the final prompt from persisted replay state and an optional fallback."""
    prompt_with_summary = f"{prepared_history.summary_prompt_prefix}{base_prompt}"
    if prepared_history.has_stored_replay_state or fallback_prompt is None:
        return prompt_with_summary
    return fallback_prompt


def _activate_prepared_history(agent: Agent, history_messages: list[object]) -> None:
    if not history_messages:
        return

    original_additional_input = None
    if isinstance(agent.additional_input, list):
        original_additional_input = list(agent.additional_input)

    binding = _ActiveHistoryState(
        original_additional_input=original_additional_input,
        original_get_run_messages=agent._get_run_messages,
        original_aget_run_messages=agent._aget_run_messages,
        original_start_learning_future=agent._start_learning_future,
        original_astart_learning_task=agent._astart_learning_task,
        original_cleanup_and_store=agent._cleanup_and_store,
        original_acleanup_and_store=agent._acleanup_and_store,
    )
    agent.__dict__[_ACTIVE_HISTORY_STATE_ATTR] = binding
    combined_input = [*(original_additional_input or []), *history_messages]
    agent.additional_input = combined_input

    def _patched_get_run_messages(self_agent: Agent, *args: object, **kwargs: object) -> RunMessages:
        run_messages = cast("RunMessages", binding.original_get_run_messages(*args, **kwargs))
        run_messages.extra_messages = strip_replay_messages(run_messages.extra_messages)
        return run_messages

    async def _patched_aget_run_messages(self_agent: Agent, *args: object, **kwargs: object) -> RunMessages:
        run_messages = cast("RunMessages", await binding.original_aget_run_messages(*args, **kwargs))
        run_messages.extra_messages = strip_replay_messages(run_messages.extra_messages)
        return run_messages

    def _patched_start_learning_future(self_agent: Agent, *args: object, **kwargs: object) -> object:
        sanitized_args, sanitized_kwargs = _sanitize_learning_call(args, kwargs)
        return binding.original_start_learning_future(*sanitized_args, **sanitized_kwargs)

    async def _patched_astart_learning_task(self_agent: Agent, *args: object, **kwargs: object) -> object:
        sanitized_args, sanitized_kwargs = _sanitize_learning_call(args, kwargs)
        return await binding.original_astart_learning_task(*sanitized_args, **sanitized_kwargs)

    def _patched_cleanup_and_store(self_agent: Agent, *args: object, **kwargs: object) -> object:
        run_response = _resolve_run_output_arg(args, kwargs)
        if run_response is not None:
            _scrub_replay_messages_from_run_output(run_response)
        return binding.original_cleanup_and_store(*args, **kwargs)

    async def _patched_acleanup_and_store(self_agent: Agent, *args: object, **kwargs: object) -> object:
        run_response = _resolve_run_output_arg(args, kwargs)
        if run_response is not None:
            _scrub_replay_messages_from_run_output(run_response)
        return await binding.original_acleanup_and_store(*args, **kwargs)

    agent.__dict__["_get_run_messages"] = MethodType(_patched_get_run_messages, agent)
    agent.__dict__["_aget_run_messages"] = MethodType(_patched_aget_run_messages, agent)
    agent.__dict__["_start_learning_future"] = MethodType(_patched_start_learning_future, agent)
    agent.__dict__["_astart_learning_task"] = MethodType(_patched_astart_learning_task, agent)
    agent.__dict__["_cleanup_and_store"] = MethodType(_patched_cleanup_and_store, agent)
    agent.__dict__["_acleanup_and_store"] = MethodType(_patched_acleanup_and_store, agent)


def _merge_bound_prepared_history(prepared_histories: list[PreparedHistory]) -> PreparedHistory:
    if not prepared_histories:
        return PreparedHistory()

    summary_prompt_prefix = ""
    seen_summary_prompt_prefixes: set[str] = set()
    for prepared in prepared_histories:
        if not prepared.summary_prompt_prefix:
            continue
        seen_summary_prompt_prefixes.add(prepared.summary_prompt_prefix)
        if not summary_prompt_prefix:
            summary_prompt_prefix = prepared.summary_prompt_prefix

    if len(seen_summary_prompt_prefixes) > 1:
        logger.warning(
            "Bound agents produced mismatched summary prefixes; using the first non-empty prefix",
            summary_prefix_count=len(seen_summary_prompt_prefixes),
        )

    return PreparedHistory(
        summary_prompt_prefix=summary_prompt_prefix,
        has_stored_replay_state=any(prepared.has_stored_replay_state for prepared in prepared_histories),
    )


def _sanitized_run_messages_for_learning(run_messages: RunMessages) -> RunMessages:
    return replace(
        run_messages,
        messages=[message for message in run_messages.messages if not is_replay_message(message)],
    )


def _sanitize_learning_call(
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> tuple[tuple[object, ...], dict[str, object]]:
    sanitized_kwargs = dict(kwargs)
    run_messages = sanitized_kwargs.get("run_messages")
    if isinstance(run_messages, RunMessages):
        sanitized_kwargs["run_messages"] = _sanitized_run_messages_for_learning(run_messages)
        return args, sanitized_kwargs

    if not args or not isinstance(args[0], RunMessages):
        return args, sanitized_kwargs

    sanitized_args = list(args)
    sanitized_args[0] = _sanitized_run_messages_for_learning(cast("RunMessages", sanitized_args[0]))
    return tuple(sanitized_args), sanitized_kwargs


def _resolve_run_output_arg(args: tuple[object, ...], kwargs: dict[str, object]) -> RunOutput | None:
    run_response = kwargs.get("run_response")
    if isinstance(run_response, RunOutput):
        return run_response
    if args and isinstance(args[0], RunOutput):
        return cast("RunOutput", args[0])
    return None


def _scrub_replay_messages_from_run_output(run_response: RunOutput) -> None:
    additional_input = run_response.additional_input
    if isinstance(additional_input, list):
        filtered_additional_input = [message for message in additional_input if not is_replay_message(message)]
        run_response.additional_input = filtered_additional_input or None
    messages = run_response.messages
    if isinstance(messages, list):
        filtered_messages = [message for message in messages if not is_replay_message(message)]
        run_response.messages = filtered_messages or None


def _materialize_session(
    *,
    agent_name: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    storage: SqliteDb | None,
    session: AgentSession | None,
) -> tuple[SqliteDb | None, AgentSession | None]:
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


def _resolve_available_history_budget(
    *,
    agent: Agent,
    agent_name: str,
    full_prompt: str,
    config: Config,
) -> int | None:
    model_name = config.get_entity_model_name(agent_name)
    model_config = config.models.get(model_name)
    context_window = model_config.context_window if model_config is not None else None
    compaction_config = config.get_agent_compaction_config(agent_name)

    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is None:
        if context_window is None:
            return None
        threshold_tokens = resolve_effective_compaction_threshold(compaction_config, context_window)

    ceiling = threshold_tokens
    if context_window is not None:
        reserve_tokens = normalize_compaction_budget_tokens(compaction_config.reserve_tokens, context_window)
        ceiling = min(ceiling, max(0, context_window - reserve_tokens))

    return max(0, ceiling - estimate_static_tokens(agent, full_prompt))
