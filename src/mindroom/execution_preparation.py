"""Request-scoped execution preparation for prompts and persisted replay."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from xml.sax.saxutils import quoteattr as xml_quoteattr

from agno.models.message import Message

from mindroom.constants import (
    COMPACTION_NOTICE_CONTENT_KEY,
    ORIGINAL_SENDER_KEY,
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
from mindroom.matrix.client_visible_messages import replace_visible_message
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
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

logger = get_logger(__name__)

_DEFAULT_UNSEEN_MESSAGES_HEADER = "Messages since your last response:"
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
    """Final request-scoped input planning result."""

    messages: tuple[Message, ...]
    replay_plan: ResolvedReplayPlan | None
    unseen_event_ids: list[str]
    replays_persisted_history: bool
    compaction_outcomes: list[CompactionOutcome]

    @property
    def final_prompt(self) -> str:
        """Return the prompt-visible text derived from the canonical message input."""
        return render_prepared_messages_text(self.messages)

    @property
    def context_messages(self) -> tuple[Message, ...]:
        """Return replayed context messages without the current user turn."""
        return self.messages[:-1]


@dataclass(frozen=True)
class ThreadHistoryRenderLimits:
    """Optional limits for rendering visible thread history back into prompt messages."""

    max_messages: int | None = None
    max_message_length: int | None = None
    missing_sender_label: str | None = None


def _wrap_msg_body(sender: str, body: str) -> str:
    """Render one Matrix message as a <msg from="..."><![CDATA[...]]></msg> tag."""
    safe_body = body.replace("]]>", "]]]]><![CDATA[>")
    return f"<msg from={xml_quoteattr(sender)}><![CDATA[{safe_body}]]></msg>"


def _collect_history_messages(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    max_messages: int | None,
    max_message_length: int | None,
    missing_sender_label: str | None,
) -> list[tuple[str, str]]:
    messages = thread_history[-max_messages:] if max_messages is not None else thread_history
    collected: list[tuple[str, str]] = []
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
        collected.append((sender, body))
    return collected


def _build_plain_prompt_with_history(
    prompt: str,
    history_messages: list[tuple[str, str]],
    *,
    header: str,
    prompt_intro: str,
) -> str:
    if not history_messages:
        return prompt
    context = "\n".join(f"{sender}: {body}" for sender, body in history_messages)
    return f"{header}\n{context}\n\n{prompt_intro}{prompt}"


def _build_matrix_prompt_with_history(
    prompt: str,
    history_messages: list[tuple[str, str]],
    *,
    header: str,
    prompt_intro: str,
    current_sender: str | None,
) -> str:
    current_block = _wrap_msg_body(current_sender, prompt) if current_sender is not None else prompt
    standalone_prompt = f"{prompt_intro}{current_block}" if current_sender is not None else prompt
    if not history_messages:
        return standalone_prompt
    rendered_history = "\n".join(_wrap_msg_body(sender, body) for sender, body in history_messages)
    return f"{header}\n<conversation>\n{rendered_history}\n</conversation>\n\n{prompt_intro}{current_block}"


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
    if not thread_history:
        return prompt
    history_messages = _collect_history_messages(
        thread_history,
        max_messages=max_messages,
        max_message_length=max_message_length,
        missing_sender_label=missing_sender_label,
    )
    return _build_plain_prompt_with_history(
        prompt,
        history_messages,
        header=header,
        prompt_intro=prompt_intro,
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
    history_messages = (
        _collect_history_messages(
            thread_history,
            max_messages=max_messages,
            max_message_length=max_message_length,
            missing_sender_label=missing_sender_label,
        )
        if thread_history
        else []
    )
    return _build_matrix_prompt_with_history(
        prompt,
        history_messages,
        header=header,
        prompt_intro=prompt_intro,
        current_sender=current_sender,
    )


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


def _message_speaker_label(message: ResolvedVisibleMessage) -> str:
    """Return the speaker label that should be shown for one visible Matrix message."""
    original_sender = message.content.get(ORIGINAL_SENDER_KEY)
    if isinstance(original_sender, str) and original_sender:
        return original_sender
    return message.sender


def _build_unseen_messages_header(partial_reply_kinds: set[_PartialReplyKind]) -> str:
    """Choose the unseen-context guidance for the partial-reply mix present."""
    if not partial_reply_kinds:
        return _DEFAULT_UNSEEN_MESSAGES_HEADER
    if partial_reply_kinds == {_PartialReplyKind.INTERRUPTED}:
        return _INTERRUPTED_PARTIAL_REPLY_HEADER
    if partial_reply_kinds == {_PartialReplyKind.IN_PROGRESS}:
        return _IN_PROGRESS_PARTIAL_REPLY_HEADER
    return _MIXED_PARTIAL_REPLY_HEADER


def _context_message_from_visible_message(
    message: ResolvedVisibleMessage,
    *,
    response_sender_id: str | None,
    missing_sender_label: str | None = None,
) -> Message:
    """Convert one visible Matrix message into a structured Agno message."""
    if response_sender_id is not None and message.sender == response_sender_id:
        return Message(role="assistant", content=message.body)
    speaker_label = _message_speaker_label(message)
    if not speaker_label:
        speaker_label = missing_sender_label
    if speaker_label:
        return Message(role="user", content=f"{speaker_label}: {message.body}")
    return Message(role="user", content=message.body)


def _context_messages_from_visible_messages(
    messages: Sequence[ResolvedVisibleMessage],
    *,
    response_sender_id: str | None,
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
) -> tuple[Message, ...]:
    """Convert visible Matrix context into provider-native message objects."""
    visible_messages = messages[-max_messages:] if max_messages is not None else messages
    return tuple(
        _context_message_from_visible_message(
            message,
            response_sender_id=response_sender_id,
            missing_sender_label=missing_sender_label,
        )
        for message in visible_messages
        if message.body and (max_message_length is None or len(message.body) < max_message_length)
    )


def _messages_with_current_prompt(
    prompt: str,
    *,
    context_messages: Sequence[Message] = (),
    current_sender_id: str | None = None,
) -> tuple[Message, ...]:
    """Return canonical live request messages with the current user turn last."""
    messages = [message.model_copy(deep=True) for message in context_messages]
    current_prompt = (
        _build_matrix_prompt_with_history(
            prompt,
            [],
            header="Previous conversation in this thread:",
            prompt_intro="Current message:\n",
            current_sender=current_sender_id,
        )
        if current_sender_id is not None
        else prompt
    )
    messages.append(Message(role="user", content=current_prompt))
    return tuple(messages)


def render_prepared_messages_text(messages: Sequence[Message]) -> str:
    """Render canonical request messages to text for logs and rough token estimates."""
    return "\n\n".join(str(message.content) for message in messages if message.content)


def render_prepared_team_messages_text(messages: Sequence[Message]) -> str:
    """Render prepared team messages into the exact string form passed to Agno teams."""
    rendered_chunks: list[str] = []
    for message in messages:
        if not message.content:
            continue
        content = str(message.content)
        rendered_chunks.append(f"assistant: {content}" if message.role == "assistant" else content)
    return "\n\n".join(rendered_chunks)


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
    unseen_messages, partial_reply_kinds, in_progress_event_ids = _get_unseen_messages_for_sender(
        thread_history,
        sender_id=response_sender_id,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
    )
    context_messages = _context_messages_from_visible_messages(
        unseen_messages,
        response_sender_id=response_sender_id,
    )
    if partial_reply_kinds:
        context_messages = (
            Message(role="user", content=_build_unseen_messages_header(partial_reply_kinds)),
            *context_messages,
        )
    return (
        _messages_with_current_prompt(
            prompt,
            context_messages=context_messages,
            current_sender_id=current_sender_id,
        ),
        _get_unseen_event_ids_for_metadata(
            unseen_messages,
            in_progress_event_ids=in_progress_event_ids,
        ),
    )


def _build_thread_history_messages(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    *,
    response_sender_id: str | None,
    current_sender_id: str | None = None,
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
) -> tuple[Message, ...]:
    """Return canonical request messages for fallback full-thread replay."""
    if not thread_history:
        return _messages_with_current_prompt(prompt, current_sender_id=current_sender_id)
    return _messages_with_current_prompt(
        prompt,
        context_messages=_context_messages_from_visible_messages(
            thread_history,
            response_sender_id=response_sender_id,
            max_messages=max_messages,
            max_message_length=max_message_length,
            missing_sender_label=missing_sender_label,
        ),
        current_sender_id=current_sender_id,
    )


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
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    reply_to_event_id: str | None,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
    current_sender_id: str | None,
    config: Config,
    prepare_scope_history_fn: Callable[[str, str | None], Awaitable[PreparedScopeHistory]],
    estimate_static_tokens_fn: Callable[[str, str | None], int],
    render_messages_text_fn: Callable[[Sequence[Message]], str],
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    timing_scope: str | None = None,
) -> PreparedExecutionContext:
    """Prepare one request-scoped prompt/replay plan after unseen-thread handling."""
    del timing_scope
    seen_event_ids = _scope_seen_event_ids(scope_context)
    replay_fallback_messages = (
        None
        if reply_to_event_id and thread_history
        else _build_thread_history_messages(
            prompt,
            thread_history,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
            max_messages=thread_history_render_limits.max_messages if thread_history_render_limits else None,
            max_message_length=(
                thread_history_render_limits.max_message_length if thread_history_render_limits else None
            ),
            missing_sender_label=(
                thread_history_render_limits.missing_sender_label if thread_history_render_limits else None
            ),
        )
    )

    provisional_messages = _messages_with_current_prompt(prompt, current_sender_id=current_sender_id)
    if reply_to_event_id and thread_history:
        provisional_messages, _ = _build_unseen_context_messages(
            prompt,
            thread_history,
            seen_event_ids=seen_event_ids,
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
        )

    prepared_scope_history = await prepare_scope_history_fn(
        render_messages_text_fn(provisional_messages),
        render_messages_text_fn(replay_fallback_messages) if replay_fallback_messages is not None else None,
    )

    final_messages = _messages_with_current_prompt(prompt, current_sender_id=current_sender_id)
    if reply_to_event_id and thread_history:
        final_messages, unseen_event_ids = _build_unseen_context_messages(
            prompt,
            thread_history,
            seen_event_ids=_scope_seen_event_ids(scope_context),
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
        )
    else:
        unseen_event_ids = []

    prepared_history = _finalize_prepared_history(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=estimate_static_tokens_fn(
            render_messages_text_fn(final_messages),
            render_messages_text_fn(replay_fallback_messages) if replay_fallback_messages is not None else None,
        ),
    )
    if replay_fallback_messages is not None and not prepared_history.replays_persisted_history and thread_history:
        final_messages = replay_fallback_messages

    return PreparedExecutionContext(
        messages=final_messages,
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
    current_sender_id: str | None = None,
    timing_scope: str | None = None,
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
        thread_history=thread_history,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender,
        current_sender_id=current_sender_id,
        config=config,
        prepare_scope_history_fn=_prepare_agent_scope_history,
        estimate_static_tokens_fn=lambda prepared_prompt, replay_fallback_prompt: estimate_preparation_static_tokens(
            agent,
            full_prompt=prepared_prompt,
            fallback_full_prompt=replay_fallback_prompt,
        ),
        render_messages_text_fn=render_prepared_messages_text,
        thread_history_render_limits=None,
        timing_scope=timing_scope,
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
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
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
        thread_history=thread_history,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        config=config,
        prepare_scope_history_fn=_prepare_team_scope_history,
        estimate_static_tokens_fn=lambda prepared_prompt,
        replay_fallback_prompt: estimate_preparation_static_tokens_for_team(
            team,
            full_prompt=prepared_prompt,
            fallback_full_prompt=replay_fallback_prompt,
        ),
        render_messages_text_fn=render_prepared_team_messages_text,
        thread_history_render_limits=thread_history_render_limits,
    )
