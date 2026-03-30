"""Runtime integration for scoped replay and compaction."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MethodType
from typing import TYPE_CHECKING, cast

from agno.run.agent import RunOutput
from agno.run.messages import RunMessages
from agno.session.agent import AgentSession
from pydantic import BaseModel

from mindroom.agents import create_session_storage, create_state_storage_db, get_agent_session
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
from mindroom.history.types import HistoryPolicy, HistoryScope, PreparedHistory, ResolvedHistorySettings
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence

    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb

    from mindroom.config.main import Config
    from mindroom.config.models import CompactionConfig
    from mindroom.constants import RuntimePaths
    from mindroom.history.types import CompactionOutcome, CompactionState, ReplayPlan
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_ACTIVE_HISTORY_STATE_ATTR = "_mindroom_active_history_state"
_TEAM_SCOPE_OWNER_AGENT_ATTR = "_mindroom_team_scope_owner_agent_name"
_AdditionalInputItem = str | dict[object, object] | BaseModel
_TEAM_STATE_ROOT_DIRNAME = "teams"
_TEAM_STORAGE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass
class _ActiveHistoryState:
    original_additional_input: list[_AdditionalInputItem] | None
    original_get_run_messages: Callable[..., RunMessages]
    original_aget_run_messages: Callable[..., Awaitable[RunMessages]]
    original_start_learning_future: Callable[..., object]
    original_astart_learning_task: Callable[..., Awaitable[object]]
    original_cleanup_and_store: Callable[..., object]
    original_acleanup_and_store: Callable[..., Awaitable[object]]


@dataclass(frozen=True)
class ScopeSessionContext:
    """Resolved storage/session context for one logical history scope."""

    scope: HistoryScope
    storage: SqliteDb
    session: AgentSession | None


@dataclass(frozen=True)
class _ResolvedPreparationInputs:
    history_settings: ResolvedHistorySettings
    compaction_config: CompactionConfig
    has_authored_compaction_config: bool
    active_model_name: str
    active_context_window: int | None
    static_prompt_tokens: int


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
    history_settings: ResolvedHistorySettings | None = None,
    compaction_config: CompactionConfig | None = None,
    has_authored_compaction_config: bool | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
    static_prompt_tokens: int | None = None,
) -> PreparedHistory:
    """Prepare persisted replay state for one run and activate Agno guards."""
    clear_prepared_history(agent)

    scope = resolve_history_scope(agent)
    scope_context = load_scope_session_context(
        agent=agent,
        agent_name=agent_name,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        storage=storage,
        session=session,
        scope=scope,
    )
    if scope_context is None or scope_context.session is None:
        return PreparedHistory()
    scope = scope_context.scope
    storage = scope_context.storage
    resolved_session = scope_context.session

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
    available_history_budget = _resolve_available_history_budget(
        compaction_config=resolved_inputs.compaction_config,
        active_context_window=resolved_inputs.active_context_window,
        static_prompt_tokens=resolved_inputs.static_prompt_tokens,
    )
    state = read_scope_state(resolved_session, scope)
    replay_plan = _build_scope_replay_plan(
        session=resolved_session,
        scope=scope,
        state=state,
        history_settings=resolved_inputs.history_settings,
    )
    state, replay_plan, compaction_outcomes = await _apply_scope_compaction_if_needed(
        storage=storage,
        session=resolved_session,
        scope=scope,
        state=state,
        replay_plan=replay_plan,
        config=config,
        runtime_paths=runtime_paths,
        available_history_budget=available_history_budget,
        resolved_inputs=resolved_inputs,
    )

    replay_plan = apply_oldest_first_drop_policy(
        replay_plan,
        budget_tokens=available_history_budget,
        max_tool_calls_from_history=resolved_inputs.history_settings.max_tool_calls_from_history,
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
    team_name: str | None = None,
    active_model_name: str | None = None,
    active_context_window: int | None = None,
) -> PreparedHistory:
    """Prepare persisted history for a team's member agents."""
    clear_bound_agent_history_state(agents)
    owner_agent, owner_agent_name = resolve_bound_history_owner(agents)
    if owner_agent is None or owner_agent_name is None:
        return PreparedHistory()

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

    prepared = await prepare_history_for_run(
        agent=owner_agent,
        agent_name=owner_agent_name,
        full_prompt=full_prompt,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        compaction_outcomes_collector=compaction_outcomes_collector,
        history_settings=history_settings,
        compaction_config=compaction_config,
        has_authored_compaction_config=has_authored_compaction_config,
        active_model_name=resolved_active_model_name,
        active_context_window=resolved_active_context_window,
    )
    for agent in agents:
        if agent is owner_agent:
            continue
        _activate_prepared_history(agent, prepared.history_messages)
    return prepared


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
        agent.__dict__.pop(_TEAM_SCOPE_OWNER_AGENT_ATTR, None)


def resolve_bound_history_owner(agents: list[Agent]) -> tuple[Agent | None, str | None]:
    """Return the canonical storage owner for one bound team run."""
    candidates = [(agent_id, agent) for agent in agents if isinstance((agent_id := agent.id), str) and agent_id]
    if not candidates:
        return None, None

    owner_agent_name = min(agent_id for agent_id, _agent in candidates)
    _bind_team_scope_owner_agent_name(agents, owner_agent_name)
    for agent_id, agent in candidates:
        if agent_id == owner_agent_name:
            return agent, owner_agent_name
    return None, None


def load_scope_session_context(
    *,
    agent: Agent,
    agent_name: str,
    session_id: str | None,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    storage: SqliteDb | None = None,
    session: AgentSession | None = None,
    scope: HistoryScope | None = None,
    create_session_if_missing: bool = False,
) -> ScopeSessionContext | None:
    """Load the canonical storage/session backing one scope for one live agent."""
    resolved_scope = scope or resolve_history_scope(agent)
    if session_id is None or resolved_scope is None:
        return None

    if resolved_scope.kind == "team":
        storage = None
        session = None

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
        session = AgentSession(
            session_id=session_id,
            agent_id=_scope_session_agent_id(resolved_scope),
            team_id=resolved_scope.scope_id if resolved_scope.kind == "team" else None,
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


def _activate_prepared_history(agent: Agent, history_messages: Sequence[_AdditionalInputItem]) -> None:  # noqa: C901
    if not history_messages:
        return

    original_additional_input: list[_AdditionalInputItem] | None = None
    if isinstance(agent.additional_input, list):
        original_additional_input = cast("list[_AdditionalInputItem]", list(agent.additional_input))

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
    combined_input: list[_AdditionalInputItem] = [*(original_additional_input or []), *history_messages]
    agent.additional_input = combined_input

    def _patched_get_run_messages(_self_agent: Agent, *args: object, **kwargs: object) -> RunMessages:
        run_messages = binding.original_get_run_messages(*args, **kwargs)
        run_messages.extra_messages = strip_replay_messages(run_messages.extra_messages)
        return run_messages

    async def _patched_aget_run_messages(_self_agent: Agent, *args: object, **kwargs: object) -> RunMessages:
        run_messages = await binding.original_aget_run_messages(*args, **kwargs)
        run_messages.extra_messages = strip_replay_messages(run_messages.extra_messages)
        return run_messages

    def _patched_start_learning_future(_self_agent: Agent, *args: object, **kwargs: object) -> object:
        sanitized_args, sanitized_kwargs = _sanitize_learning_call(args, kwargs)
        return binding.original_start_learning_future(*sanitized_args, **sanitized_kwargs)

    async def _patched_astart_learning_task(_self_agent: Agent, *args: object, **kwargs: object) -> object:
        sanitized_args, sanitized_kwargs = _sanitize_learning_call(args, kwargs)
        return await binding.original_astart_learning_task(*sanitized_args, **sanitized_kwargs)

    def _patched_cleanup_and_store(_self_agent: Agent, *args: object, **kwargs: object) -> object:
        run_response = _resolve_run_output_arg(args, kwargs)
        if run_response is not None:
            _scrub_replay_messages_from_run_output(run_response)
        return binding.original_cleanup_and_store(*args, **kwargs)

    async def _patched_acleanup_and_store(_self_agent: Agent, *args: object, **kwargs: object) -> object:
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
        return args[0]
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
    scope: HistoryScope,
    storage: SqliteDb | None,
    session: AgentSession | None,
) -> tuple[SqliteDb | None, AgentSession | None]:
    if storage is None:
        storage = _create_scope_session_storage(
            agent_name=agent_name,
            scope=scope,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
    if session is None:
        session = get_agent_session(storage, session_id)
    return storage, session


def _bind_team_scope_owner_agent_name(agents: list[Agent], owner_agent_name: str) -> None:
    for agent in agents:
        agent.__dict__[_TEAM_SCOPE_OWNER_AGENT_ATTR] = owner_agent_name


def _create_scope_session_storage(
    *,
    agent_name: str,
    scope: HistoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> SqliteDb:
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

    resolved_static_prompt_tokens = static_prompt_tokens
    if resolved_static_prompt_tokens is None:
        resolved_static_prompt_tokens = estimate_static_tokens(agent, full_prompt)

    return _ResolvedPreparationInputs(
        history_settings=resolved_history_settings,
        compaction_config=resolved_compaction_config,
        has_authored_compaction_config=resolved_has_authored_compaction_config,
        active_model_name=resolved_active_model_name,
        active_context_window=resolved_active_context_window,
        static_prompt_tokens=resolved_static_prompt_tokens,
    )


def _build_scope_replay_plan(
    *,
    session: AgentSession,
    scope: HistoryScope,
    state: CompactionState,
    history_settings: ResolvedHistorySettings,
) -> ReplayPlan:
    return build_replay_plan(
        session=session,
        scope=scope,
        state=state,
        policy=history_settings.policy,
        max_tool_calls_from_history=history_settings.max_tool_calls_from_history,
    )


async def _apply_scope_compaction_if_needed(
    *,
    storage: SqliteDb,
    session: AgentSession,
    scope: HistoryScope,
    state: CompactionState,
    replay_plan: ReplayPlan,
    config: Config,
    runtime_paths: RuntimePaths,
    available_history_budget: int | None,
    resolved_inputs: _ResolvedPreparationInputs,
) -> tuple[CompactionState, ReplayPlan, list[CompactionOutcome]]:
    compaction_outcomes: list[CompactionOutcome] = []
    auto_compaction_enabled = (
        resolved_inputs.has_authored_compaction_config and resolved_inputs.compaction_config.enabled
    )
    should_attempt_compaction = state.force_compact_before_next_run or (
        auto_compaction_enabled
        and available_history_budget is not None
        and replay_plan.replay_tokens > available_history_budget
    )
    if not should_attempt_compaction:
        return state, replay_plan, compaction_outcomes

    if len(replay_plan.visible_runs) > 2:
        next_state, outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=state,
            visible_runs=replay_plan.visible_runs,
            config=config,
            runtime_paths=runtime_paths,
            compaction_config=resolved_inputs.compaction_config,
            active_model_name=resolved_inputs.active_model_name,
            active_context_window=resolved_inputs.active_context_window,
        )
        if next_state != state and outcome is None:
            write_scope_state(session, scope, next_state)
            storage.upsert_session(session)
        if outcome is not None:
            compaction_outcomes.append(outcome)
        replay_plan = _build_scope_replay_plan(
            session=session,
            scope=scope,
            state=next_state,
            history_settings=resolved_inputs.history_settings,
        )
        return next_state, replay_plan, compaction_outcomes

    if state.force_compact_before_next_run:
        cleared_state = replace(state, force_compact_before_next_run=False)
        if cleared_state != state:
            write_scope_state(session, scope, cleared_state)
            storage.upsert_session(session)
        replay_plan = _build_scope_replay_plan(
            session=session,
            scope=scope,
            state=cleared_state,
            history_settings=resolved_inputs.history_settings,
        )
        return cleared_state, replay_plan, compaction_outcomes

    return state, replay_plan, compaction_outcomes


def _resolve_available_history_budget(
    *,
    compaction_config: CompactionConfig,
    active_context_window: int | None,
    static_prompt_tokens: int,
) -> int | None:
    threshold_tokens = compaction_config.threshold_tokens
    if threshold_tokens is None:
        if active_context_window is None:
            return None
        threshold_tokens = resolve_effective_compaction_threshold(compaction_config, active_context_window)

    ceiling = threshold_tokens
    if active_context_window is not None:
        reserve_tokens = normalize_compaction_budget_tokens(compaction_config.reserve_tokens, active_context_window)
        ceiling = min(ceiling, max(0, active_context_window - reserve_tokens))

    return max(0, ceiling - static_prompt_tokens)
