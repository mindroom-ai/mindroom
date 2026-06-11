"""Request-scoped execution preparation for prompts and persisted replay."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING
from xml.sax.saxutils import quoteattr as xml_quoteattr

from agno.models.message import Message

from mindroom import ai_runtime
from mindroom.attachment_media import attachment_records_to_media
from mindroom.attachments import attachment_records_for_visible_message, format_attachment_annotation
from mindroom.constants import (
    COMPACTION_NOTICE_CONTENT_KEY,
    ORIGINAL_SENDER_KEY,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    TOOL_TRACE_CONTENT_KEY,
    RuntimePaths,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.history import (
    PreparedHistoryState,
    PreparedScopeHistory,
    ResolvedReplayPlan,
    ScopeSessionContext,
    apply_replay_plan,
    context_budget_after_reserve,
    estimate_preparation_static_tokens,
    estimate_preparation_static_tokens_for_team,
    finalize_history_preparation,
    prepare_bound_scope_history,
    prepare_scope_history,
    read_scope_seen_event_ids,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import replace_visible_message
from mindroom.streaming import clean_partial_reply_text, is_interrupted_partial_reply, strip_visible_tool_markers
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Sequence
    from pathlib import Path

    from agno.agent import Agent
    from agno.team import Team

    from mindroom.attachments import AttachmentRecord
    from mindroom.config.main import Config
    from mindroom.history import CompactionDecision, CompactionLifecycle, CompactionOutcome, CompactionReplyOutcome
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.timing import DispatchPipelineTiming

logger = get_logger(__name__)

_PARTIAL_REPLY_SENDER_LABELS = {
    "interrupted": "You (interrupted reply draft)",
    "in_progress": "You (reply still streaming)",
}


class _PartialReplyKind(str, Enum):
    """Classification for a self-authored partial reply preserved in prompt context."""

    IN_PROGRESS = "in_progress"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class _PreparedExecutionContext:
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


@dataclass(frozen=True)
class _ThreadAttachmentContext:
    """Bind attachment-record resolution to one room for thread-history rendering."""

    storage_path: Path
    room_id: str | None

    def records_for(self, message: ResolvedVisibleMessage) -> list[AttachmentRecord]:
        return attachment_records_for_visible_message(self.storage_path, message, room_id=self.room_id)


def _wrap_msg_body(sender: str, body: str) -> str:
    """Render one Matrix message as a <msg from="..."><![CDATA[...]]></msg> tag."""
    safe_body = body.replace("]]>", "]]]]><![CDATA[>")
    return f"<msg from={xml_quoteattr(sender)}><![CDATA[{safe_body}]]></msg>"


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
    if status in {STREAM_STATUS_CANCELLED, STREAM_STATUS_ERROR, STREAM_STATUS_INTERRUPTED}:
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
    """Strip live status notes before the canonical interrupted replay marker is added."""
    return clean_partial_reply_text(body)


def _message_speaker_label(message: ResolvedVisibleMessage) -> str:
    """Return the speaker label that should be shown for one visible Matrix message."""
    original_sender = message.content.get(ORIGINAL_SENDER_KEY)
    if isinstance(original_sender, str) and original_sender:
        return original_sender
    return message.sender


def _is_relayed_user_message(message: ResolvedVisibleMessage) -> bool:
    """Return whether an internal Matrix sender is relaying a user-authored message."""
    original_sender = message.content.get(ORIGINAL_SENDER_KEY)
    return isinstance(original_sender, str) and bool(original_sender)


def _should_strip_visible_tool_markers(
    message: ResolvedVisibleMessage,
    *,
    response_sender_id: str | None,
) -> bool:
    """Return whether visible marker lines are known MindRoom display chrome."""
    if isinstance(message.content.get(TOOL_TRACE_CONTENT_KEY), dict):
        return True
    return (
        response_sender_id is not None
        and message.sender == response_sender_id
        and not _is_relayed_user_message(message)
    )


def _context_body_from_visible_message(
    message: ResolvedVisibleMessage,
    *,
    response_sender_id: str | None,
) -> str:
    """Return the model-facing body for one visible Matrix message."""
    if _should_strip_visible_tool_markers(message, response_sender_id=response_sender_id):
        return strip_visible_tool_markers(message.body)
    return message.body


def _cap_visible_message_body(body: str, max_length: int | None) -> str:
    """Return a body capped for fallback context while marking truncated text."""
    if max_length is None or len(body) <= max_length:
        return body
    if max_length <= 0:
        return ""
    return f"{body[: max_length - 1]}…"


def _build_unseen_messages_header(
    partial_reply_kinds: set[_PartialReplyKind],
    *,
    config: Config,
) -> str:
    """Choose the unseen-context guidance for the partial-reply mix present."""
    if not partial_reply_kinds:
        return config.get_prompt("DEFAULT_UNSEEN_MESSAGES_HEADER")
    if partial_reply_kinds == {_PartialReplyKind.INTERRUPTED}:
        return config.get_prompt("INTERRUPTED_PARTIAL_REPLY_HEADER")
    if partial_reply_kinds == {_PartialReplyKind.IN_PROGRESS}:
        return config.get_prompt("IN_PROGRESS_PARTIAL_REPLY_HEADER")
    return config.get_prompt("MIXED_PARTIAL_REPLY_HEADER")


def _context_message_from_visible_message(
    message: ResolvedVisibleMessage,
    *,
    response_sender_id: str | None,
    missing_sender_label: str | None = None,
    body: str | None = None,
    attachment_records: Sequence[AttachmentRecord] = (),
) -> Message:
    """Convert one visible Matrix message into a structured Agno message."""
    # Matrix bodies include human-facing tool markers like "🔧 `tool` [1]".
    # Those markers are display chrome, not conversation content; if we replay
    # them to the model it can continue the pattern as plain text with no trace.
    body = _context_body_from_visible_message(message, response_sender_id=response_sender_id) if body is None else body
    annotation = format_attachment_annotation(list(attachment_records))
    if annotation:
        body = f"{body}\n{annotation}" if body else annotation
    if (
        response_sender_id is not None
        and message.sender == response_sender_id
        and not _is_relayed_user_message(message)
    ):
        # Provider APIs reject media on assistant turns, so agent-sent
        # attachments surface through the annotation text only.
        return Message(role="assistant", content=body)
    speaker_label = _message_speaker_label(message)
    if not speaker_label:
        speaker_label = missing_sender_label
    content = f"{speaker_label}: {body}" if speaker_label else body
    if not attachment_records:
        return Message(role="user", content=content)
    audio, images, files, videos = attachment_records_to_media(list(attachment_records))
    return Message(
        role="user",
        content=content,
        audio=audio or None,
        images=images or None,
        files=files or None,
        videos=videos or None,
    )


def _context_messages_from_visible_messages(
    messages: Sequence[ResolvedVisibleMessage],
    *,
    response_sender_id: str | None,
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
    attachment_context: _ThreadAttachmentContext | None = None,
) -> tuple[Message, ...]:
    """Convert visible Matrix context into provider-native message objects."""
    visible_messages = messages[-max_messages:] if max_messages is not None else messages
    context_messages: list[Message] = []
    for message in visible_messages:
        # Strip before length capping so display-only markers do not consume the
        # model-context budget or leave marker-only turns behind.
        body = _context_body_from_visible_message(message, response_sender_id=response_sender_id)
        attachment_records = attachment_context.records_for(message) if attachment_context is not None else []
        if not body and not attachment_records:
            continue
        capped_body = body
        capped_message = replace_visible_message(message, body=capped_body)
        if max_message_length is not None:
            capped_body = _cap_visible_message_body(body, max_message_length)
            if not capped_body and not attachment_records:
                continue
            capped_message = replace_visible_message(message, body=capped_body)
        context_messages.append(
            _context_message_from_visible_message(
                capped_message,
                response_sender_id=response_sender_id,
                missing_sender_label=missing_sender_label,
                body=capped_body,
                attachment_records=attachment_records,
            ),
        )
    return tuple(context_messages)


def _messages_with_capped_context(
    prompt: str,
    *,
    context_messages: Sequence[Message],
    current_sender_id: str | None,
    config: Config,
    static_token_budget: int,
    estimate_static_tokens_fn: Callable[[str], int],
    render_messages_text_fn: Callable[[Sequence[Message]], str],
) -> tuple[Message, ...]:
    """Return the newest context-message suffix that fits the total static token budget."""
    selected_context: list[Message] = []
    current_only_messages = _messages_with_current_prompt(prompt, current_sender_id=current_sender_id, config=config)
    current_only_tokens = estimate_static_tokens_fn(render_messages_text_fn(current_only_messages))
    if current_only_tokens > static_token_budget:
        return current_only_messages

    for context_message in reversed(context_messages):
        candidate_context = [context_message, *selected_context]
        candidate_messages = _messages_with_current_prompt(
            prompt,
            context_messages=candidate_context,
            current_sender_id=current_sender_id,
            config=config,
        )
        if estimate_static_tokens_fn(render_messages_text_fn(candidate_messages)) > static_token_budget:
            break
        selected_context = candidate_context
    return _messages_with_current_prompt(
        prompt,
        context_messages=selected_context,
        current_sender_id=current_sender_id,
        config=config,
    )


def _messages_with_current_prompt(
    prompt: str,
    *,
    context_messages: Sequence[Message] = (),
    current_sender_id: str | None = None,
    config: Config,
) -> tuple[Message, ...]:
    """Return canonical live request messages with the current user turn last."""
    messages = [message.model_copy(deep=True) for message in context_messages]
    current_prompt = (
        _build_matrix_prompt_with_history(
            prompt,
            [],
            header=config.get_prompt("PREVIOUS_CONVERSATION_THREAD_HEADER"),
            prompt_intro=config.get_prompt("CURRENT_MESSAGE_PROMPT_INTRO"),
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
    config: Config,
    attachment_context: _ThreadAttachmentContext | None = None,
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
        attachment_context=attachment_context,
    )
    if partial_reply_kinds:
        context_messages = (
            Message(role="user", content=_build_unseen_messages_header(partial_reply_kinds, config=config)),
            *context_messages,
        )
    return (
        _messages_with_current_prompt(
            prompt,
            context_messages=context_messages,
            current_sender_id=current_sender_id,
            config=config,
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
    config: Config,
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
    static_token_budget: int | None = None,
    estimate_static_tokens_fn: Callable[[str], int] | None = None,
    render_messages_text_fn: Callable[[Sequence[Message]], str] | None = None,
    attachment_context: _ThreadAttachmentContext | None = None,
) -> tuple[Message, ...]:
    """Return canonical request messages for fallback full-thread replay."""
    if not thread_history:
        return _messages_with_current_prompt(prompt, current_sender_id=current_sender_id, config=config)
    context_messages = _context_messages_from_visible_messages(
        thread_history,
        response_sender_id=response_sender_id,
        max_messages=max_messages,
        max_message_length=max_message_length,
        missing_sender_label=missing_sender_label,
        attachment_context=attachment_context,
    )
    if (
        static_token_budget is not None
        and estimate_static_tokens_fn is not None
        and render_messages_text_fn is not None
    ):
        return _messages_with_capped_context(
            prompt,
            context_messages=context_messages,
            current_sender_id=current_sender_id,
            config=config,
            static_token_budget=static_token_budget,
            estimate_static_tokens_fn=estimate_static_tokens_fn,
            render_messages_text_fn=render_messages_text_fn,
        )
    return _messages_with_current_prompt(
        prompt,
        context_messages=context_messages,
        current_sender_id=current_sender_id,
        config=config,
    )


def _fallback_static_token_budget(*, context_window: int | None, reserve_tokens: int) -> int | None:
    """Return the total static-token budget available to Matrix-thread fallback prompts."""
    if context_window is None or context_window <= 0:
        return None
    return context_budget_after_reserve(context_window, reserve_tokens)


def _thread_history_before_current_event(
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    current_event_id: str | None,
) -> Sequence[ResolvedVisibleMessage] | None:
    """Return full-context fallback history up to, but not including, the current event."""
    if not thread_history or current_event_id is None:
        return thread_history
    preceding_messages: list[ResolvedVisibleMessage] = []
    for msg in thread_history:
        if msg.event_id == current_event_id:
            return tuple(preceding_messages)
        preceding_messages.append(msg)
    return tuple(preceding_messages)


def _sanitize_thread_history_for_replay(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    response_sender_id: str | None,
    active_event_ids: Collection[str],
) -> tuple[ResolvedVisibleMessage, ...]:
    """Apply unseen-context sanitization before fallback full-thread replay."""
    sanitized, _, _ = _get_unseen_messages_for_sender(
        thread_history,
        sender_id=response_sender_id,
        seen_event_ids=set(),
        current_event_id=None,
        active_event_ids=active_event_ids,
    )
    return tuple(sanitized)


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
        if sender_id and sender == sender_id and not _is_relayed_user_message(msg):
            partial_kind = _classify_partial_reply(
                msg,
                active_event_ids=active_event_ids,
            )
            if partial_kind is _PartialReplyKind.INTERRUPTED:
                continue
            if partial_kind is not None:
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
    attachment_context: _ThreadAttachmentContext | None = None,
    timing_scope: str | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> _PreparedExecutionContext:
    """Prepare one request-scoped prompt/replay plan after unseen-thread handling."""
    del timing_scope
    seen_event_ids = _scope_seen_event_ids(scope_context)

    provisional_messages = _messages_with_current_prompt(prompt, current_sender_id=current_sender_id, config=config)
    if reply_to_event_id and thread_history:
        provisional_messages, _ = _build_unseen_context_messages(
            prompt,
            thread_history,
            seen_event_ids=seen_event_ids,
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
            config=config,
            attachment_context=attachment_context,
        )

    prepared_scope_history = await prepare_scope_history_fn(render_messages_text_fn(provisional_messages))

    final_messages = _messages_with_current_prompt(prompt, current_sender_id=current_sender_id, config=config)
    if reply_to_event_id and thread_history:
        final_messages, unseen_event_ids = _build_unseen_context_messages(
            prompt,
            thread_history,
            seen_event_ids=_scope_seen_event_ids(scope_context),
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
            config=config,
            attachment_context=attachment_context,
        )
    else:
        unseen_event_ids = []

    final_static_tokens = estimate_static_tokens_fn(render_messages_text_fn(final_messages))
    prepared_history = _finalize_prepared_history(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=final_static_tokens,
        pipeline_timing=pipeline_timing,
    )
    if pipeline_timing is not None:
        pipeline_timing.mark("prompt_assembly_start")
    if not prepared_history.replays_persisted_history and thread_history:
        fallback_thread_history = _thread_history_before_current_event(thread_history, reply_to_event_id)
        if fallback_thread_history is not None:
            fallback_thread_history = _sanitize_thread_history_for_replay(
                fallback_thread_history,
                response_sender_id=response_sender_id,
                active_event_ids=active_event_ids,
            )
        replay_fallback_messages = _build_thread_history_messages(
            prompt,
            fallback_thread_history,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
            config=config,
            max_messages=thread_history_render_limits.max_messages if thread_history_render_limits else None,
            max_message_length=(
                thread_history_render_limits.max_message_length if thread_history_render_limits else None
            ),
            missing_sender_label=(
                thread_history_render_limits.missing_sender_label if thread_history_render_limits else None
            ),
            static_token_budget=fallback_static_token_budget,
            estimate_static_tokens_fn=estimate_static_tokens_fn,
            render_messages_text_fn=render_messages_text_fn,
            attachment_context=attachment_context,
        )
        final_messages = replay_fallback_messages
        fallback_context_tokens = estimate_static_tokens_fn(render_messages_text_fn(final_messages))
        if prepared_history.replay_plan is not None:
            fallback_context_tokens += prepared_history.replay_plan.estimated_tokens
        prepared_history = replace(
            prepared_history,
            prepared_context_tokens=fallback_context_tokens,
            estimated_context_tokens=fallback_context_tokens,
        )
    if pipeline_timing is not None:
        pipeline_timing.mark("prompt_assembly_ready")

    return _PreparedExecutionContext(
        messages=final_messages,
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
    thread_id: str | None,
    reply_to_event_id: str | None,
    active_event_ids: Collection[str],
    compaction_outcomes_collector: list[CompactionOutcome] | None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    current_sender_id: str | None = None,
    include_openai_compat_guidance: bool = False,
    timing_scope: str | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> _PreparedExecutionContext:
    """Prepare one agent's final prompt and replay plan for the current call."""
    response_sender = None
    if not include_openai_compat_guidance:
        response_sender_id = entity_identity_registry(config, runtime_paths).current_ids.get(agent_name)
        response_sender = response_sender_id.full_id if response_sender_id is not None else None
    runtime_model = config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=room_id,
        thread_id=thread_id,
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
        attachment_context=_ThreadAttachmentContext(
            storage_path=runtime_paths.storage_root,
            room_id=room_id,
        ),
        timing_scope=timing_scope,
        pipeline_timing=pipeline_timing,
    )


async def _prepare_bound_team_execution_context(
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
    room_id: str | None = None,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    response_sender_id: str | None = None,
    current_sender_id: str | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> _PreparedExecutionContext:
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
        attachment_context=_ThreadAttachmentContext(
            storage_path=runtime_paths.storage_root,
            room_id=room_id,
        ),
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
    room_id: str | None = None,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    response_sender_id: str | None = None,
    current_sender_id: str | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> _PreparedExecutionContext:
    """Prepare a team run with queued-notice scrubbing and replay application."""
    _scrub_bound_team_scope_context(
        scope_context=scope_context,
        team=team,
        entity_name=entity_name,
    )
    prepared_execution = await _prepare_bound_team_execution_context(
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
        room_id=room_id,
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
