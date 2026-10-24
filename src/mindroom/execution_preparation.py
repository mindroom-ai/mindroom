"""Request-scoped execution preparation for prompts and persisted replay."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom import ai_runtime
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.attachments.storage import attachment_records_for_visible_message, format_attachment_annotation
from mindroom.background_tasks import run_blocking_until_complete, run_coroutine_until_complete
from mindroom.constants import (
    COMPACTION_NOTICE_CONTENT_KEY,
    ORIGINAL_SENDER_KEY,
    SKILL_REVIEW_NOTICE_CONTENT_KEY,
    SOURCE_KIND_KEY,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    THREAD_SUMMARY_CONTENT_KEY,
    UI_ACTION_CONTENT_KEY,
    RuntimePaths,
)
from mindroom.dispatch_source import hook_source_from_content
from mindroom.entity_resolution import current_internal_sender_ids, entity_identity_registry
from mindroom.history.policy import context_budget_after_reserve, resolve_replay_window
from mindroom.history.prompt_tokens import build_static_token_estimator
from mindroom.history.request_messages import TurnMessages
from mindroom.history.runtime import (
    PreparedScopeHistory,
    finalize_history_preparation,
    prepare_bound_scope_history,
    prepare_scope_history,
    resolve_agent_preparation_inputs,
)
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.history.types import HistoryScope, ResolvedReplayPlan
from mindroom.llm.messages import Message
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import replace_visible_message
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.streaming import (
    PROGRESS_PLACEHOLDER,
    TEAM_PROGRESS_PLACEHOLDER,
    clean_partial_reply_text,
    is_interrupted_partial_reply,
)
from mindroom.timestamp_formatting import format_timestamp_ms
from mindroom.timing import timed
from mindroom.tool_system.events import has_tool_trace, strip_visible_tool_markers

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.attachments.storage import AttachmentRecord
    from mindroom.config.main import Config, ResolvedRuntimeModel
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.types import CompactionLifecycle, PreparedHistoryState
    from mindroom.llm.provider import ModelProvider, ToolSpec
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.timing import DispatchPipelineTiming

logger = get_logger(__name__)

_NO_MEMBER_DISPLAY_NAMES: Mapping[str, str] = MappingProxyType({})

_PARTIAL_REPLY_SENDER_LABELS = {
    "interrupted": "You (interrupted reply draft)",
    "in_progress": "You (reply still streaming)",
}
_PARTIAL_REPLY_GUIDANCE_LABELS = frozenset({*_PARTIAL_REPLY_SENDER_LABELS.values(), "You (partial reply)"})
_HOOK_OUTPUT_LABEL = "[posted automatically by hook {hook_source}, not written by you or any agent]"
# Claims about who wrote a message; only MindRoom's own accounts may make them.
_AUTHORSHIP_CLAIM_KEYS = (ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY)
# Notices that are not turns: lifecycle notices describe the runtime, a Chat UI request is a tool
# call's fallback text for other clients (answers and error reports name the canvas themselves),
# and a thread summary is the thread's title.
_NON_TURN_NOTICE_CONTENT_KEYS = (
    COMPACTION_NOTICE_CONTENT_KEY,
    SKILL_REVIEW_NOTICE_CONTENT_KEY,
    UI_ACTION_CONTENT_KEY,
    THREAD_SUMMARY_CONTENT_KEY,
)


class _PartialReplyKind(str, Enum):
    """Classification for a self-authored partial reply preserved in prompt context."""

    IN_PROGRESS = "in_progress"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class _PreparedExecutionContext:
    """Final request-scoped input planning result."""

    turn: TurnMessages
    unseen_event_ids: list[str]
    prepared_history: PreparedHistoryState

    @property
    def replay_plan(self) -> ResolvedReplayPlan | None:
        """Return the resolved persisted-replay plan for this execution."""
        return self.prepared_history.replay_plan


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


def _classify_partial_reply(
    msg: ResolvedVisibleMessage,
    *,
    active_event_ids: Collection[str],
) -> _PartialReplyKind | None:
    """Classify a self-authored partial reply from persisted stream metadata first."""
    # LEGACY_COMPAT: Body-only terminal suffixes when structured stream status is absent.
    # Legacy format: mindroom.legacy_streaming owns body-only terminal suffix parsing without stream_status.
    # Last legacy release: v2026.3.121; replacement: v2026.3.122 wrote structured stream status.
    # Handling: Structured status wins; consult body markers only when status is absent or unrecognized.
    # Coverage: tests/test_partial_reply_context.py::TestClassifyPartialReply::test_legacy_interrupted_markers_without_metadata_are_interrupted.
    status = msg.stream_status
    if status == STREAM_STATUS_COMPLETED:
        return None
    if status in {STREAM_STATUS_CANCELLED, STREAM_STATUS_ERROR, STREAM_STATUS_INTERRUPTED}:
        return _PartialReplyKind.INTERRUPTED
    if status in {STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING}:
        if isinstance(msg.event_id, str) and msg.event_id not in active_event_ids:
            return _PartialReplyKind.INTERRUPTED
        return _PartialReplyKind.IN_PROGRESS
    return _PartialReplyKind.INTERRUPTED if is_interrupted_partial_reply(msg.body) else None


def _has_authorship_claim(message: ResolvedVisibleMessage) -> bool:
    return any(key in message.content for key in _AUTHORSHIP_CLAIM_KEYS)


def _without_untrusted_authorship_claims(
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Sequence[ResolvedVisibleMessage] | None:
    """Drop relay or hook claims that a sender outside MindRoom wrote, so the prompt names each message's real author."""
    if not thread_history or not any(_has_authorship_claim(message) for message in thread_history):
        return thread_history
    internal_sender_ids = current_internal_sender_ids(config, runtime_paths)
    return [
        message
        if message.sender in internal_sender_ids or not _has_authorship_claim(message)
        else replace(message, content={k: v for k, v in message.content.items() if k not in _AUTHORSHIP_CLAIM_KEYS})
        for message in thread_history
    ]


def _relayed_sender(message: ResolvedVisibleMessage) -> str | None:
    """Return the user whose message an internal Matrix sender relayed, or None for a message its sender wrote."""
    original_sender = message.content.get(ORIGINAL_SENDER_KEY)
    return original_sender if isinstance(original_sender, str) and original_sender else None


def _is_own_reply(message: ResolvedVisibleMessage, response_sender_id: str | None) -> bool:
    """Return whether this agent wrote one visible message.

    Relayed user messages and hook output also arrive from the agent's account, but the agent wrote neither.
    Replaying hook progress as assistant turns taught the model to write fake progress lines itself.
    """
    return (
        bool(response_sender_id)
        and message.sender == response_sender_id
        and _relayed_sender(message) is None
        and hook_source_from_content(message.content) is None
    )


def _context_body_from_visible_message(
    message: ResolvedVisibleMessage,
    *,
    response_sender_id: str | None,
) -> str:
    """Return the model-facing body for one visible Matrix message, without marker lines known to be display chrome."""
    if has_tool_trace(message.content) or _is_own_reply(message, response_sender_id):
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
    member_display_names: Mapping[str, str] = _NO_MEMBER_DISPLAY_NAMES,
) -> Message:
    """Convert one visible Matrix message into a structured prompt message."""
    # Matrix bodies include human-facing tool markers like "🔧 `tool` [1]".
    # Those markers are display chrome, not conversation content; if we replay
    # them to the model it can continue the pattern as plain text with no trace.
    body = _context_body_from_visible_message(message, response_sender_id=response_sender_id) if body is None else body
    annotation = format_attachment_annotation(list(attachment_records))
    if _is_own_reply(message, response_sender_id):
        if message.content.get("msgtype") == "m.audio":
            body = f"[audio message: {body}]"
        if annotation:
            body = f"{body}\n{annotation}" if body else annotation
        # Provider APIs reject media on assistant turns, so agent-sent
        # attachments surface through text annotations only. The leading
        # separator prevents same-role provider merges from gluing messages.
        return Message(role="assistant", content=f"\n\n{body}")
    if annotation:
        body = f"{body}\n{annotation}" if body else annotation
    hook_source = hook_source_from_content(message.content)
    if hook_source is not None:
        body = f"{_HOOK_OUTPUT_LABEL.format(hook_source=hook_source)}\n{body}"
    event_id = message.event_id or None
    speaker_label = _relayed_sender(message) or message.sender or missing_sender_label
    if speaker_label in _PARTIAL_REPLY_GUIDANCE_LABELS and response_sender_id is not None:
        content = render_msg_tag(
            sender=response_sender_id,
            body=f"{speaker_label}: {body}",
            event_id=event_id,
        )
    else:
        sender = speaker_label or ""
        content = render_msg_tag(
            sender=sender,
            body=body,
            event_id=event_id,
            display_name=member_display_names.get(sender),
        )
    return Message(role="user", content=content)


def _context_messages_from_visible_messages(
    messages: Sequence[ResolvedVisibleMessage],
    *,
    response_sender_id: str | None,
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
    attachment_context: _ThreadAttachmentContext | None = None,
    member_display_names: Mapping[str, str] = _NO_MEMBER_DISPLAY_NAMES,
) -> tuple[Message, ...]:
    """Convert visible Matrix context into provider-native message objects."""
    visible_messages = messages[-max_messages:] if max_messages is not None else messages
    context_messages: list[Message] = []
    for message in visible_messages:
        # Strip before length capping so display-only markers do not consume the
        # model-context budget or leave marker-only turns behind.
        body = _context_body_from_visible_message(message, response_sender_id=response_sender_id)
        attachment_records = attachment_context.records_for(message) if attachment_context is not None else []
        capped_body = _cap_visible_message_body(body, max_message_length)
        if not capped_body and not attachment_records:
            continue
        context_messages.append(
            _context_message_from_visible_message(
                replace_visible_message(message, body=capped_body),
                response_sender_id=response_sender_id,
                missing_sender_label=missing_sender_label,
                body=capped_body,
                attachment_records=attachment_records,
                member_display_names=member_display_names,
            ),
        )
    return tuple(context_messages)


def _messages_with_capped_context(
    with_context: Callable[[Sequence[Message]], TurnMessages],
    context_messages: Sequence[Message],
    *,
    config: Config,
    static_token_budget: int,
    estimate_static_tokens_fn: Callable[[str], int],
    render_messages_text_fn: Callable[[Sequence[Message]], str],
) -> TurnMessages:
    """Return the turn ``with_context`` builds around the newest context-message suffix that fits the static budget."""

    def fits(turn: TurnMessages) -> bool:
        return estimate_static_tokens_fn(render_messages_text_fn(turn.in_request_order())) <= static_token_budget

    full_messages = with_context(context_messages)
    if fits(full_messages):
        return full_messages
    selected_messages = with_context(())
    for start in range(len(context_messages), 0, -1):
        # Tell the agent older messages were dropped; the marker counts against the same budget.
        marker = config.render_prompt("THREAD_HISTORY_OMITTED_MARKER_TEMPLATE", omitted_count=start)
        messages = with_context([Message(role="user", content=marker), *context_messages[start:]])
        if not fits(messages):
            break
        selected_messages = messages
    return selected_messages


def _messages_with_current_prompt(
    prompt: str,
    *,
    context_messages: Sequence[Message] = (),
    transient_context_messages: Sequence[Message] = (),
    current_sender_id: str | None = None,
    current_timestamp_ms: float | None = None,
    current_event_id: str | None = None,
    current_prompt_is_structured: bool = False,
    config: Config,
    member_display_names: Mapping[str, str] = _NO_MEMBER_DISPLAY_NAMES,
) -> TurnMessages:
    """Return the canonical live turn: its context messages, the current user prompt, and its transient context."""
    current_ts = format_timestamp_ms(current_timestamp_ms, timezone=config.timezone)
    current_prompt = prompt
    if current_sender_id is not None:
        current_block = (
            prompt
            if current_prompt_is_structured
            else render_msg_tag(
                sender=current_sender_id,
                body=prompt,
                event_id=current_event_id,
                ts=current_ts,
                display_name=member_display_names.get(current_sender_id),
            )
        )
        current_prompt = f"{config.get_prompt('CURRENT_MESSAGE_PROMPT_INTRO')}{current_block}"
    return TurnMessages(
        prompt=Message(role="user", content=current_prompt),
        context=tuple(deepcopy(message) for message in context_messages),
        transient=tuple(deepcopy(message) for message in transient_context_messages),
    )


def render_prepared_messages_text(messages: Sequence[Message]) -> str:
    """Render canonical request messages to text for logs and rough token estimates."""
    return "\n\n".join(str(message.content) for message in messages if message.content)


def render_prepared_team_messages_text(messages: Sequence[Message]) -> str:
    """Render prepared team messages to text, labeling assistant turns, for logs and rough token estimates."""
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
    transient_context_messages: Sequence[Message] = (),
    seen_event_ids: set[str],
    current_event_id: str,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
    current_sender_id: str | None = None,
    member_display_names: Mapping[str, str] = _NO_MEMBER_DISPLAY_NAMES,
    current_timestamp_ms: float | None = None,
    prompt_event_id: str | None = None,
    current_prompt_is_structured: bool = False,
    config: Config,
    attachment_context: _ThreadAttachmentContext | None = None,
) -> tuple[TurnMessages, list[str]]:
    """Return the canonical turn for unseen thread context plus the current prompt, and the unseen event ids."""
    history_before_current = _thread_history_before_current_event(thread_history, current_event_id)
    unseen_messages, partial_reply_kinds, in_progress_event_ids = _get_unseen_messages_for_sender(
        history_before_current or (),
        sender_id=response_sender_id,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
    )
    context_messages = _context_messages_from_visible_messages(
        unseen_messages,
        response_sender_id=response_sender_id,
        attachment_context=attachment_context,
        member_display_names=member_display_names,
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
            transient_context_messages=transient_context_messages,
            current_sender_id=current_sender_id,
            current_timestamp_ms=current_timestamp_ms,
            current_event_id=prompt_event_id,
            current_prompt_is_structured=current_prompt_is_structured,
            config=config,
            member_display_names=member_display_names,
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
    transient_context_messages: Sequence[Message] = (),
    response_sender_id: str | None,
    current_sender_id: str | None = None,
    member_display_names: Mapping[str, str] = _NO_MEMBER_DISPLAY_NAMES,
    current_timestamp_ms: float | None = None,
    current_event_id: str | None = None,
    current_prompt_is_structured: bool = False,
    config: Config,
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
    static_token_budget: int | None = None,
    estimate_static_tokens_fn: Callable[[str], int] | None = None,
    render_messages_text_fn: Callable[[Sequence[Message]], str] | None = None,
    attachment_context: _ThreadAttachmentContext | None = None,
) -> TurnMessages:
    """Return the canonical turn for fallback full-thread replay."""

    def with_context(context_messages: Sequence[Message] = ()) -> TurnMessages:
        return _messages_with_current_prompt(
            prompt,
            context_messages=context_messages,
            transient_context_messages=transient_context_messages,
            current_sender_id=current_sender_id,
            current_timestamp_ms=current_timestamp_ms,
            current_event_id=current_event_id,
            current_prompt_is_structured=current_prompt_is_structured,
            config=config,
            member_display_names=member_display_names,
        )

    if not thread_history:
        return with_context()
    context_messages = _context_messages_from_visible_messages(
        thread_history,
        response_sender_id=response_sender_id,
        max_messages=max_messages,
        max_message_length=max_message_length,
        missing_sender_label=missing_sender_label,
        attachment_context=attachment_context,
        member_display_names=member_display_names,
    )
    if (
        static_token_budget is not None
        and estimate_static_tokens_fn is not None
        and render_messages_text_fn is not None
    ):
        return _messages_with_capped_context(
            with_context,
            context_messages,
            config=config,
            static_token_budget=static_token_budget,
            estimate_static_tokens_fn=estimate_static_tokens_fn,
            render_messages_text_fn=render_messages_text_fn,
        )
    return with_context(context_messages)


def _fallback_static_token_budget(
    *,
    context_window: int | None,
    replay_window_tokens: int | None,
    reserve_tokens: int,
) -> int | None:
    """Return the total static-token budget available to Matrix-thread fallback prompts.

    Fallback thread replay stands in for persisted replay, so it uses the same replay window.
    """
    window = resolve_replay_window(
        active_context_window=context_window,
        configured_replay_window=replay_window_tokens,
    )
    if window is None or window <= 0:
        return None
    return context_budget_after_reserve(window, reserve_tokens)


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


def _thread_history_with_scheduled_budget(
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    *,
    current_event_id: str | None,
    source_event_id: str,
    history_limit: int,
) -> Sequence[ResolvedVisibleMessage] | None:
    """Keep the current prompt event plus at most the newest prior history messages."""
    if not thread_history:
        return thread_history

    history_through_current: list[ResolvedVisibleMessage] = []
    for message in thread_history:
        history_through_current.append(message)
        if current_event_id is not None and message.event_id == current_event_id:
            break

    prompt_event_ids = {source_event_id}
    if current_event_id is not None:
        prompt_event_ids.add(current_event_id)
    history_indices = [
        index for index, message in enumerate(history_through_current) if message.event_id not in prompt_event_ids
    ]
    selected_indices = set(history_indices[-history_limit:]) if history_limit > 0 else set()
    return tuple(
        message
        for index, message in enumerate(history_through_current)
        if index in selected_indices or message.event_id == current_event_id
    )


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
        if msg.latest_event_id != event_id:
            event_ids.append(msg.latest_event_id)
    return event_ids


def _has_nothing_to_read(msg: ResolvedVisibleMessage) -> bool:
    """Return whether one message is a notice rather than a turn, or a streamed reply showing only its placeholder.

    MindRoom redacts a placeholder that ends empty, so recording one as consumed would let that tidy-up remove
    the history of whoever read it.
    """
    content = msg.content
    if isinstance(content, dict) and any(key in content for key in _NON_TURN_NOTICE_CONTENT_KEYS):
        return True
    return msg.stream_status is not None and msg.body.strip() in {PROGRESS_PLACEHOLDER, TEAM_PROGRESS_PLACEHOLDER}


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
        if event_id and event_id in seen_event_ids:
            continue
        if current_event_id and event_id == current_event_id:
            continue
        if _has_nothing_to_read(msg):
            continue
        if _is_own_reply(msg, sender_id):
            partial_kind = _classify_partial_reply(
                msg,
                active_event_ids=active_event_ids,
            )
            if partial_kind is _PartialReplyKind.INTERRUPTED:
                continue
            if partial_kind is not None:
                # Strip live status notes before the canonical interrupted replay marker is added.
                cleaned_body = clean_partial_reply_text(msg.body)
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
    return read_scope_seen_event_ids(scope_context.storage, scope_context.session, scope_context.scope)


def _prepared_history_with_scheduled_limit(
    prepared_history: PreparedHistoryState,
    max_persisted_messages: int,
) -> PreparedHistoryState:
    """Intersect persisted replay with the scheduled turn's remaining message budget."""
    if max_persisted_messages <= 0:
        return replace(
            prepared_history,
            replay_plan=ResolvedReplayPlan(mode="disabled", estimated_tokens=0, add_history_to_context=False),
            replays_persisted_history=False,
        )
    plan = prepared_history.replay_plan
    if plan is None or not plan.add_history_to_context or plan.mode == "disabled":
        return prepared_history
    if plan.num_history_messages is not None and plan.num_history_messages <= max_persisted_messages:
        return prepared_history
    return replace(
        prepared_history,
        replay_plan=replace(
            plan,
            mode="limited",
            num_history_messages=max_persisted_messages,
        ),
    )


@timed("system_prompt_assembly.history_prepare.finalize")
async def _finalize_prepared_history(
    *,
    prepared_scope_history: PreparedScopeHistory,
    config: Config,
    static_prompt_tokens: int,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedHistoryState:
    # Planning reads the caller's live session in a worker; drain it before cancellation lets the caller change it.
    return await run_coroutine_until_complete(
        asyncio.to_thread(
            finalize_history_preparation,
            prepared_scope_history=prepared_scope_history,
            config=config,
            static_prompt_tokens=static_prompt_tokens,
            pipeline_timing=pipeline_timing,
        ),
    )


async def _prepare_execution_context_common(
    ctx: ResponseTurnContext,
    *,
    scope_context: ScopeSessionContext | None,
    prompt: str,
    transient_context_messages: Sequence[Message] = (),
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    response_sender_id: str | None,
    current_sender_id: str | None,
    member_display_names: Mapping[str, str] = _NO_MEMBER_DISPLAY_NAMES,
    current_timestamp_ms: float | None = None,
    current_event_id: str | None = None,
    current_prompt_is_structured: bool = False,
    config: Config,
    prepare_scope_history_fn: Callable[[str], Awaitable[PreparedScopeHistory]],
    estimate_static_tokens_fn: Callable[[str], int],
    render_messages_text_fn: Callable[[Sequence[Message]], str],
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    fallback_static_token_budget: int | None = None,
    attachment_context: _ThreadAttachmentContext | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> _PreparedExecutionContext:
    """Prepare one request-scoped prompt/replay plan after unseen-thread handling."""
    history_boundary_event_id = ctx.history_boundary_event_id or ctx.reply_to_event_id
    active_event_ids = ctx.active_event_ids
    # Compacted history's seen ids are read from the summary rows; keep that SQLite read off the loop.
    seen_event_ids = await run_blocking_until_complete(_scope_seen_event_ids, scope_context)
    scheduled_history_budget = ctx.scheduled_history_budget
    if scheduled_history_budget is not None:
        thread_history = _thread_history_with_scheduled_budget(
            thread_history,
            current_event_id=history_boundary_event_id,
            source_event_id=scheduled_history_budget.source_event_id,
            history_limit=scheduled_history_budget.limit,
        )

    def turn_messages(seen_event_ids: set[str]) -> tuple[TurnMessages, list[str]]:
        """Return the turn, with the thread's unseen context when it has a history boundary, and the unseen ids."""
        if not (history_boundary_event_id and thread_history):
            return _messages_with_current_prompt(
                prompt,
                transient_context_messages=transient_context_messages,
                current_sender_id=current_sender_id,
                current_timestamp_ms=current_timestamp_ms,
                current_event_id=current_event_id,
                current_prompt_is_structured=current_prompt_is_structured,
                config=config,
                member_display_names=member_display_names,
            ), []
        return _build_unseen_context_messages(
            prompt,
            thread_history,
            transient_context_messages=transient_context_messages,
            seen_event_ids=seen_event_ids,
            current_event_id=history_boundary_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
            current_timestamp_ms=current_timestamp_ms,
            prompt_event_id=current_event_id,
            current_prompt_is_structured=current_prompt_is_structured,
            config=config,
            attachment_context=attachment_context,
            member_display_names=member_display_names,
        )

    provisional_messages, _ = turn_messages(seen_event_ids)
    prepared_scope_history = await prepare_scope_history_fn(
        render_messages_text_fn(provisional_messages.in_request_order()),
    )
    final_messages, unseen_event_ids = turn_messages(
        await run_blocking_until_complete(_scope_seen_event_ids, scope_context)
        if history_boundary_event_id and thread_history
        else set(),
    )

    final_static_tokens = estimate_static_tokens_fn(render_messages_text_fn(final_messages.in_request_order()))
    prepared_history = await _finalize_prepared_history(
        prepared_scope_history=prepared_scope_history,
        config=config,
        static_prompt_tokens=final_static_tokens,
        pipeline_timing=pipeline_timing,
    )
    if scheduled_history_budget is not None:
        prepared_history = _prepared_history_with_scheduled_limit(
            prepared_history,
            max(0, scheduled_history_budget.limit - len(final_messages.context)),
        )
    if pipeline_timing is not None:
        pipeline_timing.mark("prompt_assembly_start")
    if not prepared_history.replays_persisted_history and thread_history:
        fallback_thread_history = _thread_history_before_current_event(thread_history, history_boundary_event_id)
        if fallback_thread_history is not None:
            fallback_thread_history = _sanitize_thread_history_for_replay(
                fallback_thread_history,
                response_sender_id=response_sender_id,
                active_event_ids=active_event_ids,
            )
        render_limits = thread_history_render_limits or ThreadHistoryRenderLimits()
        final_messages = _build_thread_history_messages(
            prompt,
            fallback_thread_history,
            transient_context_messages=transient_context_messages,
            response_sender_id=response_sender_id,
            current_sender_id=current_sender_id,
            current_timestamp_ms=current_timestamp_ms,
            current_event_id=current_event_id,
            current_prompt_is_structured=current_prompt_is_structured,
            config=config,
            max_messages=render_limits.max_messages,
            max_message_length=render_limits.max_message_length,
            missing_sender_label=render_limits.missing_sender_label,
            static_token_budget=fallback_static_token_budget,
            estimate_static_tokens_fn=estimate_static_tokens_fn,
            render_messages_text_fn=render_messages_text_fn,
            attachment_context=attachment_context,
            member_display_names=member_display_names,
        )
        fallback_context_tokens = estimate_static_tokens_fn(render_messages_text_fn(final_messages.in_request_order()))
        if prepared_history.replay_plan is not None:
            fallback_context_tokens += prepared_history.replay_plan.estimated_tokens
        prepared_history = replace(prepared_history, prepared_context_tokens=fallback_context_tokens)
    if pipeline_timing is not None:
        pipeline_timing.mark("prompt_assembly_ready")

    return _PreparedExecutionContext(
        turn=final_messages,
        unseen_event_ids=unseen_event_ids,
        prepared_history=prepared_history,
    )


@timed("system_prompt_assembly.history_prepare")
async def prepare_agent_execution_context(
    ctx: ResponseTurnContext,
    *,
    scope_context: ScopeSessionContext | None,
    build: AgentBuild,
    system_message: Message,
    prompt: str,
    transient_context_messages: Sequence[Message] = (),
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    runtime_paths: RuntimePaths,
    config: Config,
    resolved_runtime_model: ResolvedRuntimeModel | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    current_sender_id: str | None = None,
    current_timestamp_ms: float | None = None,
    current_event_id: str | None = None,
    current_prompt_is_structured: bool = False,
    include_openai_compat_guidance: bool = False,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> _PreparedExecutionContext:
    """Prepare one agent's final prompt and replay plan for the current call.

    ``system_message`` is the turn's rendered system message, which sizes the static part of every request.
    """
    agent_name = ctx.entity_label
    response_sender = None
    if not include_openai_compat_guidance:
        response_sender_id = entity_identity_registry(config, runtime_paths).current_ids.get(agent_name)
        response_sender = response_sender_id.full_id if response_sender_id is not None else None
    runtime_model = resolved_runtime_model or config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        runtime_paths=runtime_paths,
    )
    static_token_estimator = build_static_token_estimator(
        system_message,
        prepare_run_tools(build.spec).specs(),
        build.request_template.deferred_tool_names,
    )
    compaction_config = config.resolve_entity(agent_name).compaction_config

    async def _prepare_agent_scope_history(
        prepared_prompt: str,
    ) -> PreparedScopeHistory:
        resolved_inputs = resolve_agent_preparation_inputs(
            agent_name=agent_name,
            history_settings=build.history_settings,
            config=config,
            active_model_name=runtime_model.model_name,
            active_context_window=runtime_model.context_window,
            static_prompt_tokens=static_token_estimator.estimate(prepared_prompt),
        )
        return await prepare_scope_history(
            agent_name=agent_name,
            scope=scope_context.scope if scope_context is not None else HistoryScope(kind="agent", scope_id=agent_name),
            model=build.spec.model,
            resolved_inputs=resolved_inputs,
            non_prompt_tokens=static_token_estimator.non_prompt_tokens(),
            runtime_paths=runtime_paths,
            config=config,
            scope_context=scope_context,
            compaction_lifecycle=compaction_lifecycle,
            pipeline_timing=pipeline_timing,
            allow_native_compaction=ctx.scheduled_history_budget is None,
        )

    return await _prepare_execution_context_common(
        ctx,
        scope_context=scope_context,
        prompt=prompt,
        transient_context_messages=transient_context_messages,
        thread_history=_without_untrusted_authorship_claims(thread_history, config, runtime_paths),
        response_sender_id=response_sender,
        current_sender_id=current_sender_id,
        member_display_names=ctx.member_display_names,
        current_timestamp_ms=current_timestamp_ms,
        current_event_id=current_event_id,
        current_prompt_is_structured=current_prompt_is_structured,
        config=config,
        prepare_scope_history_fn=_prepare_agent_scope_history,
        estimate_static_tokens_fn=static_token_estimator.estimate,
        render_messages_text_fn=render_prepared_messages_text,
        fallback_static_token_budget=_fallback_static_token_budget(
            context_window=runtime_model.context_window,
            replay_window_tokens=compaction_config.replay_window_tokens,
            reserve_tokens=compaction_config.reserve_tokens,
        ),
        attachment_context=_ThreadAttachmentContext(
            storage_path=runtime_paths.storage_root,
            room_id=ctx.room_id,
        ),
        pipeline_timing=pipeline_timing,
    )


async def _prepare_bound_team_execution_context(
    ctx: ResponseTurnContext,
    *,
    scope_context: ScopeSessionContext | None,
    member_names: Sequence[str],
    leader_system_message: Message,
    leader_tools: Sequence[ToolSpec],
    model: ModelProvider,
    prompt: str,
    transient_context_messages: Sequence[Message] = (),
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    runtime_paths: RuntimePaths,
    config: Config,
    team_name: str | None,
    active_model_name: str | None,
    active_context_window: int | None,
    response_sender_id: str | None = None,
    current_sender_id: str | None = None,
    member_display_names: Mapping[str, str] = _NO_MEMBER_DISPLAY_NAMES,
    current_timestamp_ms: float | None = None,
    current_event_id: str | None = None,
    current_prompt_is_structured: bool = False,
    compaction_lifecycle: CompactionLifecycle | None = None,
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> _PreparedExecutionContext:
    """Prepare one bound team scope for the current call.

    ``leader_system_message`` and ``leader_tools`` size the static part of every leader request; the delegation tool
    the run adds is among them.
    """
    static_token_estimator = build_static_token_estimator(leader_system_message, leader_tools, frozenset())
    team_compaction_config = config.resolve_entity(
        team_name if team_name is not None and team_name in config.teams else None,
    ).compaction_config

    async def _prepare_team_scope_history(
        prepared_prompt: str,
    ) -> PreparedScopeHistory:
        return await prepare_bound_scope_history(
            member_names=member_names,
            model=model,
            runtime_paths=runtime_paths,
            config=config,
            scope_context=scope_context,
            team_name=team_name,
            active_model_name=active_model_name,
            active_context_window=active_context_window,
            static_prompt_tokens=static_token_estimator.estimate(prepared_prompt),
            non_prompt_tokens=static_token_estimator.non_prompt_tokens(),
            compaction_lifecycle=compaction_lifecycle,
            pipeline_timing=pipeline_timing,
            allow_native_compaction=ctx.scheduled_history_budget is None,
        )

    return await _prepare_execution_context_common(
        ctx,
        scope_context=scope_context,
        prompt=prompt,
        transient_context_messages=transient_context_messages,
        thread_history=_without_untrusted_authorship_claims(thread_history, config, runtime_paths),
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        member_display_names=member_display_names,
        current_timestamp_ms=current_timestamp_ms,
        current_event_id=current_event_id,
        current_prompt_is_structured=current_prompt_is_structured,
        config=config,
        prepare_scope_history_fn=_prepare_team_scope_history,
        estimate_static_tokens_fn=static_token_estimator.estimate,
        render_messages_text_fn=render_prepared_team_messages_text,
        thread_history_render_limits=thread_history_render_limits,
        attachment_context=_ThreadAttachmentContext(
            storage_path=runtime_paths.storage_root,
            room_id=ctx.room_id,
        ),
        fallback_static_token_budget=_fallback_static_token_budget(
            context_window=active_context_window,
            replay_window_tokens=team_compaction_config.replay_window_tokens,
            reserve_tokens=team_compaction_config.reserve_tokens,
        ),
        pipeline_timing=pipeline_timing,
    )


async def prepare_bound_team_run_context(
    ctx: ResponseTurnContext,
    *,
    scope_context: ScopeSessionContext | None,
    member_names: Sequence[str],
    leader_system_message: Message,
    leader_tools: Sequence[ToolSpec],
    model: ModelProvider,
    prompt: str,
    transient_context_messages: Sequence[Message] = (),
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    runtime_paths: RuntimePaths,
    config: Config,
    entity_name: str | None,
    active_model_name: str | None,
    active_context_window: int | None,
    response_sender_id: str | None = None,
    current_sender_id: str | None = None,
    current_timestamp_ms: float | None = None,
    current_event_id: str | None = None,
    current_prompt_is_structured: bool = False,
    compaction_lifecycle: CompactionLifecycle | None = None,
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> _PreparedExecutionContext:
    """Prepare a team run: recover queued notices in its scope, then plan its prompt and history replay."""
    ai_runtime.scrub_queued_notice_session_context(
        scope_context=scope_context,
        entity_name=entity_name or ctx.entity_label,
    )
    return await _prepare_bound_team_execution_context(
        ctx,
        scope_context=scope_context,
        member_names=member_names,
        leader_system_message=leader_system_message,
        leader_tools=leader_tools,
        model=model,
        prompt=prompt,
        transient_context_messages=transient_context_messages,
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        team_name=entity_name,
        active_model_name=active_model_name,
        active_context_window=active_context_window,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        member_display_names=ctx.member_display_names,
        current_timestamp_ms=current_timestamp_ms,
        current_event_id=current_event_id,
        current_prompt_is_structured=current_prompt_is_structured,
        compaction_lifecycle=compaction_lifecycle,
        thread_history_render_limits=thread_history_render_limits,
        pipeline_timing=pipeline_timing,
    )
