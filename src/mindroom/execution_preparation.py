"""Request-scoped execution preparation for prompts and persisted replay."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from xml.sax.saxutils import quoteattr as xml_quoteattr

from mindroom.constants import (
    COMPACTION_NOTICE_CONTENT_KEY,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    RuntimePaths,
)
from mindroom.history.runtime import (
    ScopeSessionContext,
    estimate_preparation_static_tokens,
    estimate_preparation_static_tokens_for_team,
    finalize_history_preparation,
    prepare_bound_scope_history,
    prepare_scope_history,
)
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.logging_config import get_logger
from mindroom.matrix.client import (
    ResolvedVisibleMessage,
    replace_visible_message,
)
from mindroom.streaming import clean_partial_reply_text, is_interrupted_partial_reply
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Sequence

    from agno.agent import Agent
    from agno.team import Team

    from mindroom.config.main import Config
    from mindroom.history import CompactionOutcome
    from mindroom.history.runtime import PreparedScopeHistory
    from mindroom.history.types import PreparedHistoryState, ResolvedReplayPlan

logger = get_logger(__name__)

_DEFAULT_UNSEEN_MESSAGES_HEADER = "Messages from other participants since your last response:"
_INTERRUPTED_PARTIAL_REPLY_HEADER = (
    "Messages since your last response:\n"
    "Your previous response was interrupted before completion. "
    "The partial content below may be incomplete. Continue from where you left off if appropriate."
)
_IN_PROGRESS_PARTIAL_REPLY_HEADER = (
    "Messages since your last response:\n"
    "Your previous response is still being delivered. Do NOT repeat or redo that work. "
    "The partial content is shown below for context only."
)
_MIXED_PARTIAL_REPLY_HEADER = (
    "Messages since your last response:\n"
    "Some partial content from your previous response is still being delivered, so do NOT repeat or redo that work. "
    "Other partial content was interrupted before completion and may be incomplete. "
    "Continue from where you left off if appropriate."
)
_PARTIAL_REPLY_SENDER_LABELS = {
    "interrupted": "You (interrupted reply draft)",
    "in_progress": "You (reply still streaming)",
}


class _PartialReplyKind(str, Enum):
    """Classification for a self-authored partial reply preserved in prompt context."""

    IN_PROGRESS = "in_progress"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class PreparedExecutionContext:
    """Final request-scoped prompt and replay planning result."""

    final_prompt: str
    replay_plan: ResolvedReplayPlan | None
    unseen_event_ids: list[str]
    replays_persisted_history: bool
    compaction_outcomes: list[CompactionOutcome]


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
    """Build a prompt with thread history context when available.

    History is rendered inside a <conversation> block as <msg from="..."> tags
    so the model can unambiguously attribute each message even when bodies
    contain colons, newlines, or quoted log lines. Bodies are passed through
    verbatim (preserving code, markdown, and special characters); the only
    transformation is neutralizing a literal "</msg>" sequence so it cannot
    prematurely close the wrapper.
    """
    if not thread_history:
        return prompt
    messages = thread_history[-max_messages:] if max_messages is not None else thread_history
    context_lines: list[str] = []
    for msg in messages:
        body = msg.body
        if not body:
            continue
        if max_message_length is not None and len(body) >= max_message_length:
            continue
        sender = msg.sender
        if not sender:
            if missing_sender_label is None:
                continue
            sender = missing_sender_label
        safe_body = body.replace("</msg>", "<\\/msg>")
        context_lines.append(f"<msg from={xml_quoteattr(sender)}>{safe_body}</msg>")
    if not context_lines:
        return prompt
    context = "<conversation>\n" + "\n".join(context_lines) + "\n</conversation>"
    return f"{header}\n{context}\n\n{prompt_intro}{prompt}"


def _classify_partial_reply(
    msg: ResolvedVisibleMessage,
    *,
    active_event_ids: Collection[str],
) -> _PartialReplyKind | None:
    """Classify a self-authored partial reply from persisted stream metadata first."""
    status = msg.stream_status
    if status == STREAM_STATUS_COMPLETED:
        return None

    partial_kind: _PartialReplyKind | None = None
    if status in {STREAM_STATUS_CANCELLED, STREAM_STATUS_ERROR}:
        partial_kind = _PartialReplyKind.INTERRUPTED
    elif status in {STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING}:
        event_id = msg.event_id
        if isinstance(event_id, str):
            return _PartialReplyKind.IN_PROGRESS if event_id in active_event_ids else _PartialReplyKind.INTERRUPTED
        partial_kind = _PartialReplyKind.IN_PROGRESS
    else:
        body = msg.body
        if is_interrupted_partial_reply(body):
            partial_kind = _PartialReplyKind.INTERRUPTED

    return partial_kind


def _clean_partial_reply_body(body: str) -> str:
    """Strip streaming markers and status notes from partial reply text."""
    return clean_partial_reply_text(body)


def _build_unseen_messages_header(partial_reply_kinds: set[_PartialReplyKind]) -> str:
    """Choose the unseen-context header for the partial-reply mix present."""
    if not partial_reply_kinds:
        return _DEFAULT_UNSEEN_MESSAGES_HEADER
    if partial_reply_kinds == {_PartialReplyKind.INTERRUPTED}:
        return _INTERRUPTED_PARTIAL_REPLY_HEADER
    if partial_reply_kinds == {_PartialReplyKind.IN_PROGRESS}:
        return _IN_PROGRESS_PARTIAL_REPLY_HEADER
    return _MIXED_PARTIAL_REPLY_HEADER


def _get_unseen_event_ids_for_metadata(
    unseen_messages: list[ResolvedVisibleMessage],
    *,
    in_progress_event_ids: set[str],
) -> list[str]:
    """Return unseen event IDs that should be persisted as consumed by this run."""
    event_ids: list[str] = []
    for msg in unseen_messages:
        event_id = msg.event_id
        if event_id in in_progress_event_ids:
            continue
        event_ids.append(event_id)
    return event_ids


def _get_unseen_messages_for_sender(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    sender_id: str | None,
    seen_event_ids: set[str],
    current_event_id: str | None,
    active_event_ids: Collection[str],
) -> tuple[list[ResolvedVisibleMessage], set[_PartialReplyKind], set[str]]:
    """Filter thread_history to unseen messages for one Matrix sender."""
    unseen: list[ResolvedVisibleMessage] = []
    partial_reply_kinds: set[_PartialReplyKind] = set()
    in_progress_event_ids: set[str] = set()
    for msg in thread_history:
        event_id = msg.event_id
        sender = msg.sender
        content = msg.content
        if event_id and event_id in seen_event_ids:
            continue
        if current_event_id and event_id == current_event_id:
            continue
        if isinstance(content, dict) and COMPACTION_NOTICE_CONTENT_KEY in content:
            continue
        if sender_id and sender == sender_id:
            partial_kind = _classify_partial_reply(
                msg,
                active_event_ids=active_event_ids,
            )
            if partial_kind is None:
                continue
            cleaned_body = _clean_partial_reply_body(msg.body)
            if not cleaned_body:
                continue
            partial_reply_kinds.add(partial_kind)
            if partial_kind is _PartialReplyKind.IN_PROGRESS and event_id is not None:
                in_progress_event_ids.add(event_id)
            unseen.append(
                replace_visible_message(
                    msg,
                    sender=_PARTIAL_REPLY_SENDER_LABELS.get(partial_kind.value, "You (partial reply)"),
                    body=cleaned_body,
                ),
            )
            continue
        unseen.append(msg)
    return unseen, partial_reply_kinds, in_progress_event_ids


def build_prompt_with_unseen_thread_context(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    *,
    seen_event_ids: set[str],
    current_event_id: str | None,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
) -> tuple[str, list[str]]:
    """Prepend unseen thread messages and return their persisted event ids."""
    if not current_event_id or not thread_history:
        return prompt, []

    unseen_messages, partial_reply_kinds, in_progress_event_ids = _get_unseen_messages_for_sender(
        thread_history,
        sender_id=response_sender_id,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
    )
    prompt_with_unseen = _build_prompt_with_unseen(
        prompt,
        unseen_messages,
        partial_reply_kinds=partial_reply_kinds,
    )
    return prompt_with_unseen, _get_unseen_event_ids_for_metadata(
        unseen_messages,
        in_progress_event_ids=in_progress_event_ids,
    )


def _build_prompt_with_unseen(
    prompt: str,
    unseen_messages: list[ResolvedVisibleMessage],
    *,
    partial_reply_kinds: set[_PartialReplyKind] | None,
) -> str:
    """Prepend unseen messages from other participants to the prompt."""
    if not unseen_messages:
        return prompt
    return build_prompt_with_thread_history(
        prompt,
        unseen_messages,
        header=_build_unseen_messages_header(partial_reply_kinds or set()),
    )


def _scope_seen_event_ids(scope_context: ScopeSessionContext | None) -> set[str]:
    """Return currently persisted seen IDs for one open prepared scope."""
    if scope_context is None or scope_context.session is None:
        return set()
    return read_scope_seen_event_ids(scope_context.session, scope_context.scope)


@timed("system_prompt_assembly.history_prepare.unseen_context_initial")
def _build_initial_unseen_context(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    seen_event_ids: set[str],
    current_event_id: str,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
) -> tuple[str, list[str]]:
    return build_prompt_with_unseen_thread_context(
        prompt,
        thread_history,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
    )


@timed("system_prompt_assembly.history_prepare.unseen_context_final")
def _build_final_unseen_context(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    seen_event_ids: set[str],
    current_event_id: str,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
) -> tuple[str, list[str]]:
    return build_prompt_with_unseen_thread_context(
        prompt,
        thread_history,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
    )


@timed("system_prompt_assembly.history_prepare.finalize")
def _finalize_prepared_history(
    *,
    prepared_scope_history: PreparedScopeHistory,
    config: Config,
    static_prompt_tokens: int,
) -> PreparedHistoryState:
    return finalize_history_preparation(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=static_prompt_tokens,
    )


async def _prepare_execution_context_common(
    *,
    scope_context: ScopeSessionContext | None,
    prompt: str,
    fallback_prompt: str | None,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    reply_to_event_id: str | None,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
    config: Config,
    prepare_scope_history_fn: Callable[[str, str | None], Awaitable[PreparedScopeHistory]],
    estimate_static_tokens_fn: Callable[[str, str | None], int],
    timing_scope: str | None = None,
) -> PreparedExecutionContext:
    """Prepare one request-scoped prompt/replay plan after unseen-thread handling."""
    del timing_scope
    seen_event_ids = _scope_seen_event_ids(scope_context)
    replay_fallback_prompt = None if reply_to_event_id and thread_history else fallback_prompt

    provisional_prompt = prompt
    if reply_to_event_id and thread_history:
        provisional_prompt, _ = _build_initial_unseen_context(
            prompt,
            thread_history,
            seen_event_ids=seen_event_ids,
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
        )

    prepared_scope_history = await prepare_scope_history_fn(
        provisional_prompt,
        replay_fallback_prompt,
    )

    if reply_to_event_id and thread_history:
        final_prompt, unseen_event_ids = _build_final_unseen_context(
            prompt,
            thread_history,
            seen_event_ids=_scope_seen_event_ids(scope_context),
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
        )
    else:
        final_prompt = prompt
        unseen_event_ids = []

    prepared_history = _finalize_prepared_history(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=estimate_static_tokens_fn(
            final_prompt,
            replay_fallback_prompt,
        ),
    )
    if replay_fallback_prompt is not None:
        final_prompt = prompt if prepared_history.replays_persisted_history else replay_fallback_prompt

    return PreparedExecutionContext(
        final_prompt=final_prompt,
        replay_plan=prepared_history.replay_plan,
        unseen_event_ids=unseen_event_ids,
        replays_persisted_history=prepared_history.replays_persisted_history,
        compaction_outcomes=prepared_history.compaction_outcomes,
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
    timing_scope: str | None = None,
) -> PreparedExecutionContext:
    """Prepare one agent's final prompt and replay plan for the current call."""
    response_sender_id = config.get_ids(runtime_paths).get(agent_name)
    response_sender = response_sender_id.full_id if response_sender_id is not None else None
    fallback_prompt = (
        None
        if reply_to_event_id and thread_history
        else build_prompt_with_thread_history(
            prompt,
            thread_history,
        )
    )
    runtime_model = config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    )

    async def _prepare_agent_scope_history(
        prepared_prompt: str,
        replay_fallback_prompt: str | None,
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
                fallback_full_prompt=replay_fallback_prompt,
            ),
            timing_scope=timing_scope,
        )

    return await _prepare_execution_context_common(
        scope_context=scope_context,
        prompt=prompt,
        fallback_prompt=fallback_prompt,
        thread_history=thread_history,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender,
        config=config,
        prepare_scope_history_fn=_prepare_agent_scope_history,
        estimate_static_tokens_fn=lambda prepared_prompt, replay_fallback_prompt: estimate_preparation_static_tokens(
            agent,
            full_prompt=prepared_prompt,
            fallback_full_prompt=replay_fallback_prompt,
        ),
        timing_scope=timing_scope,
    )


async def prepare_bound_team_execution_context(
    *,
    scope_context: ScopeSessionContext | None,
    agents: list[Agent],
    team: Team,
    prompt: str,
    fallback_prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    runtime_paths: RuntimePaths,
    config: Config,
    team_name: str | None,
    active_model_name: str | None,
    active_context_window: int | None,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    response_sender_id: str | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
) -> PreparedExecutionContext:
    """Prepare one bound team scope for the current call."""

    async def _prepare_team_scope_history(
        prepared_prompt: str,
        replay_fallback_prompt: str | None,
    ) -> PreparedScopeHistory:
        return await prepare_bound_scope_history(
            agents=agents,
            team=team,
            full_prompt=prepared_prompt,
            fallback_full_prompt=replay_fallback_prompt,
            runtime_paths=runtime_paths,
            config=config,
            compaction_outcomes_collector=compaction_outcomes_collector,
            scope_context=scope_context,
            team_name=team_name,
            active_model_name=active_model_name,
            active_context_window=active_context_window,
        )

    return await _prepare_execution_context_common(
        scope_context=scope_context,
        prompt=prompt,
        fallback_prompt=fallback_prompt,
        thread_history=thread_history,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
        config=config,
        prepare_scope_history_fn=_prepare_team_scope_history,
        estimate_static_tokens_fn=lambda prepared_prompt,
        replay_fallback_prompt: estimate_preparation_static_tokens_for_team(
            team,
            full_prompt=prepared_prompt,
            fallback_full_prompt=replay_fallback_prompt,
        ),
    )
