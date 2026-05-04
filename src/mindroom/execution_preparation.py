"""Request-scoped execution preparation for prompts and persisted replay."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom import ai_runtime
from mindroom import prepared_conversation_chain as conversation_chain
from mindroom.history import (
    PreparedHistoryState,
    PreparedScopeHistory,
    ResolvedReplayPlan,
    ScopeSessionContext,
    apply_replay_plan,
    estimate_preparation_static_tokens,
    estimate_preparation_static_tokens_for_team,
    finalize_history_preparation,
    normalize_compaction_budget_tokens,
    prepare_bound_scope_history,
    prepare_scope_history,
    read_scope_seen_event_ids,
)
from mindroom.logging_config import get_logger
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Sequence

    from agno.agent import Agent
    from agno.models.message import Message
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history import (
        CompactionDecision,
        CompactionLifecycle,
        CompactionOutcome,
        CompactionReplyOutcome,
    )
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.timing import DispatchPipelineTiming

logger = get_logger(__name__)

_PartialReplyKind = conversation_chain.PartialReplyKind


@dataclass(frozen=True)
class PreparedExecutionContext:
    """Final request-scoped input planning result."""

    messages: tuple[Message, ...]
    replay_plan: ResolvedReplayPlan | None
    unseen_event_ids: list[str]
    replays_persisted_history: bool
    compaction_outcomes: list[CompactionOutcome]
    compaction_decision: CompactionDecision | None = None
    compaction_reply_outcome: CompactionReplyOutcome = "none"
    prepared_context_tokens: int | None = None
    estimated_context_tokens: int | None = None

    @property
    def final_prompt(self) -> str:
        """Return the prompt-visible text derived from the canonical message input."""
        return render_prepared_messages_text(self.messages)

    @property
    def context_messages(self) -> tuple[Message, ...]:
        """Return replayed context messages without the current user turn."""
        return self.messages[:-1]

    @property
    def prepared_history(self) -> PreparedHistoryState:
        """Return the history diagnostics prepared for this execution."""
        default_decision = PreparedHistoryState().compaction_decision
        return PreparedHistoryState(
            compaction_outcomes=self.compaction_outcomes,
            replay_plan=self.replay_plan,
            replays_persisted_history=self.replays_persisted_history,
            compaction_decision=(
                self.compaction_decision if self.compaction_decision is not None else default_decision
            ),
            compaction_reply_outcome=self.compaction_reply_outcome,
            prepared_context_tokens=self.prepared_context_tokens,
            estimated_context_tokens=self.estimated_context_tokens,
        )


@dataclass(frozen=True)
class ThreadHistoryRenderLimits:
    """Optional limits for rendering visible thread history back into prompt messages."""

    max_messages: int | None = None
    max_message_length: int | None = None
    missing_sender_label: str | None = None


def _collect_history_messages(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    max_messages: int | None,
    max_message_length: int | None,
    missing_sender_label: str | None,
) -> list[tuple[str, str]]:
    return conversation_chain.collect_history_messages(
        thread_history,
        max_messages=max_messages,
        max_message_length=max_message_length,
        missing_sender_label=missing_sender_label,
    )


def build_prompt_with_thread_history(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    *,
    header: str = "Previous conversation in this thread:",
    prompt_intro: str = "Current message:\n",
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
) -> str:
    """Build a plain-text prompt with ``sender: body`` history lines."""
    return conversation_chain.build_prompt_with_thread_history(
        prompt,
        thread_history,
        header=header,
        prompt_intro=prompt_intro,
        max_messages=max_messages,
        max_message_length=max_message_length,
        missing_sender_label=missing_sender_label,
    )


def build_matrix_prompt_with_thread_history(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    *,
    header: str = "Previous conversation in this thread:",
    prompt_intro: str = "Current message:\n",
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
    current_sender: str | None = None,
) -> str:
    """Build a Matrix prompt with structured XML-like message wrappers."""
    return conversation_chain.build_matrix_prompt_with_thread_history(
        prompt,
        thread_history,
        header=header,
        prompt_intro=prompt_intro,
        max_messages=max_messages,
        max_message_length=max_message_length,
        missing_sender_label=missing_sender_label,
        current_sender=current_sender,
    )


def _classify_partial_reply(
    msg: ResolvedVisibleMessage,
    *,
    active_event_ids: Collection[str],
) -> _PartialReplyKind | None:
    """Classify a self-authored partial reply from persisted stream metadata first."""
    return conversation_chain.classify_partial_reply(msg, active_event_ids=active_event_ids)


def _clean_partial_reply_body(body: str) -> str:
    """Strip live status notes before the canonical interrupted replay marker is added."""
    return conversation_chain.clean_partial_reply_text(body)


def _messages_with_current_prompt(
    prompt: str,
    *,
    context_messages: Sequence[Message] = (),
    current_sender_id: str | None = None,
) -> tuple[Message, ...]:
    """Return canonical live request messages with the current user turn last."""
    return conversation_chain.messages_with_current_prompt(
        prompt,
        context_messages=context_messages,
        current_sender_id=current_sender_id,
    )


def render_prepared_messages_text(messages: Sequence[Message]) -> str:
    """Render canonical request messages to text for logs and rough token estimates."""
    return conversation_chain.render_prepared_messages_text(messages)


def render_prepared_team_messages_text(messages: Sequence[Message]) -> str:
    """Render prepared team messages into the exact string form passed to Agno teams."""
    return conversation_chain.render_prepared_team_messages_text(messages)


def _build_unseen_context_messages(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    seen_event_ids: set[str],
    current_event_id: str,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
    current_sender_id: str | None = None,
) -> tuple[tuple[Message, ...], list[str]]:
    """Return canonical request messages for unseen thread context plus the current turn."""
    chain, unseen_event_ids = conversation_chain.build_unseen_context_chain(
        prompt,
        thread_history,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
    )
    return chain.messages, unseen_event_ids


def _build_thread_history_messages(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    *,
    response_sender_id: str | None,
    current_sender_id: str | None = None,
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
    static_token_budget: int | None = None,
    estimate_static_tokens_fn: Callable[[str], int] | None = None,
    render_messages_text_fn: Callable[[Sequence[Message]], str] | None = None,
) -> tuple[Message, ...]:
    """Return canonical request messages for fallback full-thread replay."""
    chain = conversation_chain.build_thread_history_chain(
        prompt,
        thread_history,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        max_messages=max_messages,
        max_message_length=max_message_length,
        missing_sender_label=missing_sender_label,
        static_token_budget=static_token_budget,
        estimate_static_tokens_fn=estimate_static_tokens_fn,
        render_messages_text_fn=render_messages_text_fn or render_prepared_messages_text,
    )
    return chain.messages


def _fallback_static_token_budget(*, context_window: int | None, reserve_tokens: int) -> int | None:
    """Return the total static-token budget available to Matrix-thread fallback prompts."""
    if context_window is None or context_window <= 0:
        return None
    return max(0, context_window - normalize_compaction_budget_tokens(reserve_tokens, context_window))


def _thread_history_before_current_event(
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    current_event_id: str | None,
) -> Sequence[ResolvedVisibleMessage] | None:
    """Return full-context fallback history up to, but not including, the current event."""
    return conversation_chain.thread_history_before_current_event(thread_history, current_event_id)


def _sanitize_thread_history_for_replay(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    response_sender_id: str | None,
    active_event_ids: Collection[str],
) -> tuple[ResolvedVisibleMessage, ...]:
    """Apply unseen-context sanitization before fallback full-thread replay."""
    return conversation_chain.sanitize_thread_history_for_replay(
        thread_history,
        response_sender_id=response_sender_id,
        active_event_ids=active_event_ids,
    )


def _get_unseen_event_ids_for_metadata(
    unseen_messages: list[ResolvedVisibleMessage],
    *,
    in_progress_event_ids: set[str],
) -> list[str]:
    """Return unseen event IDs that should be persisted as consumed by this run."""
    return conversation_chain.get_unseen_event_ids_for_metadata(
        unseen_messages,
        in_progress_event_ids=in_progress_event_ids,
    )


def _get_unseen_messages_for_sender(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    sender_id: str | None,
    seen_event_ids: set[str],
    current_event_id: str | None,
    active_event_ids: Collection[str],
) -> tuple[list[ResolvedVisibleMessage], set[_PartialReplyKind], set[str]]:
    """Filter thread_history to unseen messages for one Matrix sender."""
    return conversation_chain.get_unseen_messages_for_sender(
        thread_history,
        sender_id=sender_id,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
    )


def _scope_seen_event_ids(scope_context: ScopeSessionContext | None) -> set[str]:
    """Return currently persisted seen IDs for one open prepared scope."""
    if scope_context is None or scope_context.session is None:
        return set()
    return read_scope_seen_event_ids(scope_context.session, scope_context.scope)


@timed("system_prompt_assembly.history_prepare.finalize")
def _finalize_prepared_history(
    *,
    prepared_scope_history: PreparedScopeHistory,
    config: Config,
    static_prompt_tokens: int,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedHistoryState:
    return finalize_history_preparation(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=static_prompt_tokens,
        pipeline_timing=pipeline_timing,
    )


async def _prepare_execution_context_common(
    *,
    scope_context: ScopeSessionContext | None,
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    reply_to_event_id: str | None,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
    current_sender_id: str | None,
    config: Config,
    prepare_scope_history_fn: Callable[[str], Awaitable[PreparedScopeHistory]],
    estimate_static_tokens_fn: Callable[[str], int],
    render_messages_text_fn: Callable[[Sequence[Message]], str],
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    fallback_static_token_budget: int | None = None,
    timing_scope: str | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedExecutionContext:
    """Prepare one request-scoped prompt/replay plan after unseen-thread handling."""
    del timing_scope
    seen_event_ids = _scope_seen_event_ids(scope_context)
    fallback_thread_history = _thread_history_before_current_event(thread_history, reply_to_event_id)
    if fallback_thread_history is not None:
        fallback_thread_history = _sanitize_thread_history_for_replay(
            fallback_thread_history,
            response_sender_id=response_sender_id,
            active_event_ids=active_event_ids,
        )
    replay_fallback_chain = conversation_chain.build_thread_history_chain(
        prompt,
        fallback_thread_history,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        max_messages=thread_history_render_limits.max_messages if thread_history_render_limits else None,
        max_message_length=(thread_history_render_limits.max_message_length if thread_history_render_limits else None),
        missing_sender_label=(
            thread_history_render_limits.missing_sender_label if thread_history_render_limits else None
        ),
        static_token_budget=fallback_static_token_budget,
        estimate_static_tokens_fn=estimate_static_tokens_fn,
        render_messages_text_fn=render_messages_text_fn,
    )

    provisional_chain = conversation_chain.build_current_prompt_chain(
        prompt,
        current_sender_id=current_sender_id,
        render_messages_text_fn=render_messages_text_fn,
    )
    if reply_to_event_id and thread_history:
        provisional_chain, _ = conversation_chain.build_unseen_context_chain(
            prompt,
            thread_history,
            seen_event_ids=seen_event_ids,
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
            render_messages_text_fn=render_messages_text_fn,
        )

    prepared_scope_history = await prepare_scope_history_fn(provisional_chain.rendered_text)

    final_chain = conversation_chain.build_current_prompt_chain(
        prompt,
        current_sender_id=current_sender_id,
        render_messages_text_fn=render_messages_text_fn,
    )
    if reply_to_event_id and thread_history:
        final_chain, unseen_event_ids = conversation_chain.build_unseen_context_chain(
            prompt,
            thread_history,
            seen_event_ids=_scope_seen_event_ids(scope_context),
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
            render_messages_text_fn=render_messages_text_fn,
        )
    else:
        unseen_event_ids = []

    final_static_tokens = estimate_static_tokens_fn(final_chain.rendered_text)
    prepared_history = _finalize_prepared_history(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=final_static_tokens,
        pipeline_timing=pipeline_timing,
    )
    if pipeline_timing is not None:
        pipeline_timing.mark("prompt_assembly_start")
    if not prepared_history.replays_persisted_history and thread_history:
        final_chain = replay_fallback_chain
        fallback_context_tokens = estimate_static_tokens_fn(final_chain.rendered_text)
        if prepared_history.replay_plan is not None:
            fallback_context_tokens += prepared_history.replay_plan.estimated_tokens
        prepared_history = replace(
            prepared_history,
            prepared_context_tokens=fallback_context_tokens,
            estimated_context_tokens=fallback_context_tokens,
        )
    if pipeline_timing is not None:
        pipeline_timing.mark("prompt_assembly_ready")

    return PreparedExecutionContext(
        messages=final_chain.messages,
        replay_plan=prepared_history.replay_plan,
        estimated_context_tokens=prepared_history.estimated_context_tokens,
        unseen_event_ids=unseen_event_ids,
        replays_persisted_history=prepared_history.replays_persisted_history,
        compaction_outcomes=prepared_history.compaction_outcomes,
        compaction_decision=prepared_history.compaction_decision,
        compaction_reply_outcome=prepared_history.compaction_reply_outcome,
        prepared_context_tokens=prepared_history.prepared_context_tokens,
    )


@timed("system_prompt_assembly.history_prepare")
async def prepare_agent_execution_context(
    *,
    scope_context: ScopeSessionContext | None,
    agent: Agent,
    agent_name: str,
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    runtime_paths: RuntimePaths,
    config: Config,
    room_id: str | None,
    reply_to_event_id: str | None,
    active_event_ids: Collection[str],
    compaction_outcomes_collector: list[CompactionOutcome] | None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    current_sender_id: str | None = None,
    timing_scope: str | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedExecutionContext:
    """Prepare one agent's final prompt and replay plan for the current call."""
    response_sender_id = config.get_ids(runtime_paths).get(agent_name)
    response_sender = response_sender_id.full_id if response_sender_id is not None else None
    runtime_model = config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    )

    async def _prepare_agent_scope_history(
        prepared_prompt: str,
    ) -> PreparedScopeHistory:
        return await prepare_scope_history(
            agent=agent,
            agent_name=agent_name,
            full_prompt=prepared_prompt,
            runtime_paths=runtime_paths,
            config=config,
            compaction_outcomes_collector=compaction_outcomes_collector,
            scope_context=scope_context,
            active_model_name=runtime_model.model_name,
            active_context_window=runtime_model.context_window,
            static_prompt_tokens=estimate_preparation_static_tokens(
                agent,
                full_prompt=prepared_prompt,
            ),
            timing_scope=timing_scope,
            compaction_lifecycle=compaction_lifecycle,
            pipeline_timing=pipeline_timing,
        )

    def _estimate_agent_static_tokens(
        prepared_prompt: str,
    ) -> int:
        return estimate_preparation_static_tokens(
            agent,
            full_prompt=prepared_prompt,
        )

    return await _prepare_execution_context_common(
        scope_context=scope_context,
        prompt=prompt,
        thread_history=thread_history,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender,
        current_sender_id=current_sender_id,
        config=config,
        prepare_scope_history_fn=_prepare_agent_scope_history,
        estimate_static_tokens_fn=_estimate_agent_static_tokens,
        render_messages_text_fn=render_prepared_messages_text,
        thread_history_render_limits=None,
        fallback_static_token_budget=_fallback_static_token_budget(
            context_window=runtime_model.context_window,
            reserve_tokens=config.get_entity_compaction_config(agent_name).reserve_tokens,
        ),
        timing_scope=timing_scope,
        pipeline_timing=pipeline_timing,
    )


async def prepare_bound_team_execution_context(
    *,
    scope_context: ScopeSessionContext | None,
    agents: list[Agent],
    team: Team,
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    runtime_paths: RuntimePaths,
    config: Config,
    team_name: str | None,
    active_model_name: str | None,
    active_context_window: int | None,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    response_sender_id: str | None = None,
    current_sender_id: str | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedExecutionContext:
    """Prepare one bound team scope for the current call."""

    async def _prepare_team_scope_history(
        prepared_prompt: str,
    ) -> PreparedScopeHistory:
        return await prepare_bound_scope_history(
            agents=agents,
            team=team,
            full_prompt=prepared_prompt,
            runtime_paths=runtime_paths,
            config=config,
            compaction_outcomes_collector=compaction_outcomes_collector,
            scope_context=scope_context,
            team_name=team_name,
            active_model_name=active_model_name,
            active_context_window=active_context_window,
            compaction_lifecycle=compaction_lifecycle,
            pipeline_timing=pipeline_timing,
        )

    def _estimate_team_static_tokens(
        prepared_prompt: str,
    ) -> int:
        return estimate_preparation_static_tokens_for_team(
            team,
            full_prompt=prepared_prompt,
        )

    return await _prepare_execution_context_common(
        scope_context=scope_context,
        prompt=prompt,
        thread_history=thread_history,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        config=config,
        prepare_scope_history_fn=_prepare_team_scope_history,
        estimate_static_tokens_fn=_estimate_team_static_tokens,
        render_messages_text_fn=render_prepared_team_messages_text,
        thread_history_render_limits=thread_history_render_limits,
        fallback_static_token_budget=_fallback_static_token_budget(
            context_window=active_context_window,
            reserve_tokens=(
                config.get_entity_compaction_config(team_name).reserve_tokens
                if team_name is not None and team_name in config.teams
                else config.get_default_compaction_config().reserve_tokens
            ),
        ),
        pipeline_timing=pipeline_timing,
    )


def _scrub_bound_team_scope_context(
    *,
    scope_context: ScopeSessionContext | None,
    team: Team,
    entity_name: str | None,
) -> None:
    """Strip stale queued-message notices before preparing a bound team run."""
    ai_runtime.scrub_queued_notice_session_context(
        scope_context=scope_context,
        entity_name=entity_name or str(team.name or "Team"),
    )


async def prepare_bound_team_run_context(
    *,
    scope_context: ScopeSessionContext | None,
    agents: list[Agent],
    team: Team,
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    runtime_paths: RuntimePaths,
    config: Config,
    entity_name: str | None,
    active_model_name: str | None,
    active_context_window: int | None,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    response_sender_id: str | None = None,
    current_sender_id: str | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedExecutionContext:
    """Prepare a team run with queued-notice scrubbing and replay application."""
    _scrub_bound_team_scope_context(
        scope_context=scope_context,
        team=team,
        entity_name=entity_name,
    )
    prepared_execution = await prepare_bound_team_execution_context(
        scope_context=scope_context,
        agents=agents,
        team=team,
        prompt=prompt,
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        team_name=entity_name,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        compaction_outcomes_collector=compaction_outcomes_collector,
        compaction_lifecycle=compaction_lifecycle,
        thread_history_render_limits=thread_history_render_limits,
        pipeline_timing=pipeline_timing,
    )
    if prepared_execution.replay_plan is not None:
        apply_replay_plan(target=team, replay_plan=prepared_execution.replay_plan)
    return prepared_execution
