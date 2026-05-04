"""Prepared conversation-chain construction and compaction transforms."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Literal, cast
from xml.sax.saxutils import quoteattr as xml_quoteattr

from agno.models.message import Message
from agno.utils.message import filter_tool_calls
from pydantic import BaseModel

from mindroom.constants import (
    COMPACTION_NOTICE_CONTENT_KEY,
    ORIGINAL_SENDER_KEY,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.matrix.client_visible_messages import replace_visible_message
from mindroom.partial_reply_text import clean_partial_reply_text, is_interrupted_partial_reply
from mindroom.token_budget import stable_serialize

if TYPE_CHECKING:
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession

    from mindroom.history.types import HistoryScope, ResolvedHistorySettings
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

type PreparedConversationChainSource = Literal[
    "current_prompt",
    "unseen_context",
    "matrix_thread_fallback",
    "persisted_runs",
    "warm_cache_compaction",
]

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
_STANDARD_HISTORY_ROLES = frozenset({"user", "assistant", "tool"})
_OVERSIZED_RUN_NOTE = "Run truncated to fit compaction budget."
_COMPACTION_SUMMARY_INSTRUCTION = """\
You are updating a durable conversation handoff summary for a future model call.

The conversation to summarize is the message chain above.
It may include user, assistant, and tool messages from previous turns.

Previous durable summary:
{previous_summary}

Your job is to produce one merged handoff summary as plain text.
Return only the summary text.

Rules:
- Preserve all still-relevant information from the previous durable summary.
- Add only the new information from the conversation messages above.
- Do not summarize static instructions or tool definitions.
- Keep unchanged wording verbatim when it is still correct so future prompt prefixes remain stable.
- Never paraphrase away exact technical details such as file paths, function names, class names, commands, Matrix IDs, model names, config keys, numeric thresholds, ports, URLs, or error text.
- Preserve tool activity when it matters to current state, especially file edits, commands, and tool results.
- Do not invent facts.
- If a section has no content, write `None.`

Write a plain-text summary in exactly this markdown structure:
## Goal
## Constraints
## Progress
## Decisions
## Next Steps
## Critical Context
"""


class PartialReplyKind(str, Enum):
    """Classification for a self-authored partial reply preserved in prompt context."""

    IN_PROGRESS = "in_progress"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class PreparedConversationChain:
    """Ordered model messages plus diagnostics for one prepared conversation chain."""

    messages: tuple[Message, ...]
    rendered_text: str
    source: PreparedConversationChainSource
    source_run_ids: tuple[str, ...] = ()
    seen_event_ids: tuple[str, ...] = ()
    estimated_tokens: int | None = None


@dataclass(frozen=True)
class CompactionSummaryRequest:
    """Prepared model request for one compaction summary chunk."""

    messages: tuple[Message, ...]
    chain: PreparedConversationChain
    included_run_ids: tuple[str, ...]
    rendered_text: str
    estimated_tokens: int


def _wrap_msg_body(sender: str, body: str) -> str:
    safe_body = body.replace("]]>", "]]]]><![CDATA[>")
    return f"<msg from={xml_quoteattr(sender)}><![CDATA[{safe_body}]]></msg>"


def _truncate_message_body(body: str, limit: int) -> str:
    if len(body) <= limit:
        return body
    if limit <= 1:
        return "…"
    return f"{body[: limit - 1]}…"


def collect_history_messages(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    max_messages: int | None,
    max_message_length: int | None,
    missing_sender_label: str | None,
) -> list[tuple[str, str]]:
    """Collect visible Matrix messages as sender/body history pairs."""
    messages = thread_history[-max_messages:] if max_messages is not None else thread_history
    collected: list[tuple[str, str]] = []
    for msg in messages:
        body = msg.body
        if not body:
            continue
        if max_message_length is not None:
            body = _truncate_message_body(body, max_message_length)
        sender = msg.sender
        if not sender:
            if missing_sender_label is None:
                continue
            sender = missing_sender_label
        collected.append((sender, body))
    return collected


def build_plain_prompt_with_history(
    prompt: str,
    history_messages: list[tuple[str, str]],
    *,
    header: str,
    prompt_intro: str,
) -> str:
    """Render sender/body history pairs ahead of the current plain-text prompt."""
    if not history_messages:
        return prompt
    context = "\n".join(f"{sender}: {body}" for sender, body in history_messages)
    return f"{header}\n{context}\n\n{prompt_intro}{prompt}"


def build_matrix_prompt_with_history(
    prompt: str,
    history_messages: list[tuple[str, str]],
    *,
    header: str,
    prompt_intro: str,
    current_sender: str | None,
) -> str:
    """Render sender/body history pairs as Matrix XML-like prompt context."""
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
    history_messages = collect_history_messages(
        thread_history,
        max_messages=max_messages,
        max_message_length=max_message_length,
        missing_sender_label=missing_sender_label,
    )
    return build_plain_prompt_with_history(
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
        collect_history_messages(
            thread_history,
            max_messages=max_messages,
            max_message_length=max_message_length,
            missing_sender_label=missing_sender_label,
        )
        if thread_history
        else []
    )
    return build_matrix_prompt_with_history(
        prompt,
        history_messages,
        header=header,
        prompt_intro=prompt_intro,
        current_sender=current_sender,
    )


def message_speaker_label(message: ResolvedVisibleMessage) -> str:
    """Return the speaker label that should be shown for one visible Matrix message."""
    original_sender = message.content.get(ORIGINAL_SENDER_KEY)
    if isinstance(original_sender, str) and original_sender:
        return original_sender
    return message.sender


def is_relayed_user_message(message: ResolvedVisibleMessage) -> bool:
    """Return whether an internal Matrix sender is relaying a user-authored message."""
    original_sender = message.content.get(ORIGINAL_SENDER_KEY)
    return isinstance(original_sender, str) and bool(original_sender)


def context_message_from_visible_message(
    message: ResolvedVisibleMessage,
    *,
    response_sender_id: str | None,
    missing_sender_label: str | None = None,
) -> Message:
    """Convert one visible Matrix message into a structured Agno message."""
    if response_sender_id is not None and message.sender == response_sender_id and not is_relayed_user_message(message):
        return Message(role="assistant", content=message.body)
    speaker_label = message_speaker_label(message)
    if not speaker_label:
        speaker_label = missing_sender_label
    if speaker_label:
        return Message(role="user", content=f"{speaker_label}: {message.body}")
    return Message(role="user", content=message.body)


def context_messages_from_visible_messages(
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
        context_message_from_visible_message(
            message,
            response_sender_id=response_sender_id,
            missing_sender_label=missing_sender_label,
        )
        for message in visible_messages
        if message.body and (max_message_length is None or len(message.body) < max_message_length)
    )


def messages_with_current_prompt(
    prompt: str,
    *,
    context_messages: Sequence[Message] = (),
    current_sender_id: str | None = None,
) -> tuple[Message, ...]:
    """Return canonical live request messages with the current user turn last."""
    messages = [message.model_copy(deep=True) for message in context_messages]
    current_prompt = (
        build_matrix_prompt_with_history(
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


def build_current_prompt_chain(
    prompt: str,
    *,
    context_messages: Sequence[Message] = (),
    current_sender_id: str | None = None,
    source: PreparedConversationChainSource = "current_prompt",
    render_messages_text_fn: Callable[[Sequence[Message]], str] = render_prepared_messages_text,
    estimated_tokens_fn: Callable[[str], int] | None = None,
    seen_event_ids: Sequence[str] = (),
) -> PreparedConversationChain:
    """Build a prepared chain from optional context messages plus the current prompt."""
    messages = messages_with_current_prompt(
        prompt,
        context_messages=context_messages,
        current_sender_id=current_sender_id,
    )
    rendered_text = render_messages_text_fn(messages)
    estimated_tokens = estimated_tokens_fn(rendered_text) if estimated_tokens_fn is not None else None
    return PreparedConversationChain(
        messages=messages,
        rendered_text=rendered_text,
        source=source,
        seen_event_ids=tuple(seen_event_ids),
        estimated_tokens=estimated_tokens,
    )


def messages_with_capped_context(
    prompt: str,
    *,
    context_messages: Sequence[Message],
    current_sender_id: str | None,
    static_token_budget: int,
    estimate_static_tokens_fn: Callable[[str], int],
    render_messages_text_fn: Callable[[Sequence[Message]], str],
) -> tuple[Message, ...]:
    """Return the newest context-message suffix that fits the total static token budget."""
    selected_context: list[Message] = []
    current_only_messages = messages_with_current_prompt(prompt, current_sender_id=current_sender_id)
    current_only_tokens = estimate_static_tokens_fn(render_messages_text_fn(current_only_messages))
    if current_only_tokens > static_token_budget:
        return current_only_messages

    for context_message in reversed(context_messages):
        candidate_context = [context_message, *selected_context]
        candidate_messages = messages_with_current_prompt(
            prompt,
            context_messages=candidate_context,
            current_sender_id=current_sender_id,
        )
        if estimate_static_tokens_fn(render_messages_text_fn(candidate_messages)) > static_token_budget:
            break
        selected_context = candidate_context
    return messages_with_current_prompt(
        prompt,
        context_messages=selected_context,
        current_sender_id=current_sender_id,
    )


def build_unseen_context_chain(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    seen_event_ids: set[str],
    current_event_id: str,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
    current_sender_id: str | None = None,
    render_messages_text_fn: Callable[[Sequence[Message]], str] = render_prepared_messages_text,
    estimated_tokens_fn: Callable[[str], int] | None = None,
) -> tuple[PreparedConversationChain, list[str]]:
    """Return canonical request chain for unseen thread context plus the current turn."""
    unseen_messages, partial_reply_kinds, in_progress_event_ids = get_unseen_messages_for_sender(
        thread_history,
        sender_id=response_sender_id,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
    )
    context_messages = context_messages_from_visible_messages(
        unseen_messages,
        response_sender_id=response_sender_id,
    )
    if partial_reply_kinds:
        context_messages = (
            Message(role="user", content=_build_unseen_messages_header(partial_reply_kinds)),
            *context_messages,
        )
    unseen_event_ids = get_unseen_event_ids_for_metadata(
        unseen_messages,
        in_progress_event_ids=in_progress_event_ids,
    )
    return (
        build_current_prompt_chain(
            prompt,
            context_messages=context_messages,
            current_sender_id=current_sender_id,
            source="unseen_context",
            render_messages_text_fn=render_messages_text_fn,
            estimated_tokens_fn=estimated_tokens_fn,
            seen_event_ids=unseen_event_ids,
        ),
        unseen_event_ids,
    )


def build_thread_history_chain(
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
    render_messages_text_fn: Callable[[Sequence[Message]], str] = render_prepared_messages_text,
) -> PreparedConversationChain:
    """Return canonical request chain for fallback full-thread replay."""
    if not thread_history:
        return build_current_prompt_chain(
            prompt,
            current_sender_id=current_sender_id,
            source="matrix_thread_fallback",
            render_messages_text_fn=render_messages_text_fn,
        )
    context_messages = context_messages_from_visible_messages(
        thread_history,
        response_sender_id=response_sender_id,
        max_messages=max_messages,
        max_message_length=max_message_length,
        missing_sender_label=missing_sender_label,
    )
    if (
        static_token_budget is not None
        and estimate_static_tokens_fn is not None
        and render_messages_text_fn is not None
    ):
        messages = messages_with_capped_context(
            prompt,
            context_messages=context_messages,
            current_sender_id=current_sender_id,
            static_token_budget=static_token_budget,
            estimate_static_tokens_fn=estimate_static_tokens_fn,
            render_messages_text_fn=render_messages_text_fn,
        )
    else:
        messages = messages_with_current_prompt(
            prompt,
            context_messages=context_messages,
            current_sender_id=current_sender_id,
        )
    rendered_text = render_messages_text_fn(messages)
    estimated_tokens = (
        estimate_static_tokens_fn(rendered_text)
        if static_token_budget is not None and estimate_static_tokens_fn is not None
        else None
    )
    return PreparedConversationChain(
        messages=tuple(messages),
        rendered_text=rendered_text,
        source="matrix_thread_fallback",
        estimated_tokens=estimated_tokens,
    )


def thread_history_before_current_event(
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


def sanitize_thread_history_for_replay(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    response_sender_id: str | None,
    active_event_ids: Collection[str],
) -> tuple[ResolvedVisibleMessage, ...]:
    """Apply unseen-context sanitization before fallback full-thread replay."""
    sanitized, _, _ = get_unseen_messages_for_sender(
        thread_history,
        sender_id=response_sender_id,
        seen_event_ids=set(),
        current_event_id=None,
        active_event_ids=active_event_ids,
    )
    return tuple(sanitized)


def get_unseen_event_ids_for_metadata(
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


def get_unseen_messages_for_sender(
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    sender_id: str | None,
    seen_event_ids: set[str],
    current_event_id: str | None,
    active_event_ids: Collection[str],
) -> tuple[list[ResolvedVisibleMessage], set[PartialReplyKind], set[str]]:
    """Filter thread_history to unseen messages for one Matrix sender."""
    unseen: list[ResolvedVisibleMessage] = []
    partial_reply_kinds: set[PartialReplyKind] = set()
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
        if sender_id and sender == sender_id and not is_relayed_user_message(msg):
            partial_kind = classify_partial_reply(
                msg,
                active_event_ids=active_event_ids,
            )
            if partial_kind is PartialReplyKind.INTERRUPTED:
                continue
            if partial_kind is not None:
                cleaned_body = clean_partial_reply_text(msg.body)
                if not cleaned_body:
                    continue
                partial_reply_kinds.add(partial_kind)
                if partial_kind is PartialReplyKind.IN_PROGRESS and event_id is not None:
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


def build_persisted_run_chain(
    runs: Sequence[RunOutput | TeamRunOutput],
    *,
    history_settings: ResolvedHistorySettings,
    source: PreparedConversationChainSource = "persisted_runs",
) -> PreparedConversationChain:
    """Materialize persisted Agno runs as the prepared conversation chain used by compaction."""
    messages: list[Message] = []
    source_run_ids: list[str] = []
    for run in runs:
        messages.extend(compaction_replay_messages(run, history_settings))
        if isinstance(run.run_id, str) and run.run_id:
            source_run_ids.append(run.run_id)
    rendered_text = render_prepared_messages_text(messages)
    return PreparedConversationChain(
        messages=tuple(messages),
        rendered_text=rendered_text,
        source=source,
        source_run_ids=tuple(source_run_ids),
        estimated_tokens=estimate_history_messages_tokens(messages),
    )


def build_warm_cache_compaction_summary_request(
    chain: PreparedConversationChain,
    *,
    previous_summary: str | None,
) -> CompactionSummaryRequest:
    """Append one summary instruction while preserving the prepared-chain prefix."""
    prefix_messages = tuple(message.model_copy(deep=True) for message in chain.messages)
    validate_tool_result_adjacency(prefix_messages)
    return _build_compaction_summary_request_from_prefix(
        chain=chain,
        prefix_messages=prefix_messages,
        previous_summary=previous_summary,
        source="warm_cache_compaction",
    )


def build_compaction_summary_request(
    *,
    previous_summary: str | None,
    compacted_runs: Sequence[RunOutput | TeamRunOutput],
    history_settings: ResolvedHistorySettings,
    max_input_tokens: int,
) -> tuple[CompactionSummaryRequest | None, list[RunOutput | TeamRunOutput]]:
    """Select a run prefix and build the chain-based summary request for one compaction chunk."""
    remaining = max_input_tokens - _summary_instruction_tokens(previous_summary)
    if remaining <= 0:
        return None, []

    included_runs: list[RunOutput | TeamRunOutput] = []
    for run in compacted_runs:
        run_tokens = _estimate_run_chain_tokens(run, history_settings)
        if run_tokens > remaining:
            if not included_runs:
                request = _build_oversized_summary_request(
                    run,
                    history_settings=history_settings,
                    max_prefix_tokens=remaining,
                    max_input_tokens=max_input_tokens,
                    previous_summary=previous_summary,
                )
                if request is None:
                    return None, []
                return request, [run]
            break
        included_runs.append(run)
        remaining -= run_tokens

    while included_runs:
        chain = build_persisted_run_chain(included_runs, history_settings=history_settings)
        request = _summary_request_from_chain(
            chain,
            previous_summary=previous_summary,
        )
        if request.estimated_tokens <= max_input_tokens:
            return request, included_runs
        if len(included_runs) == 1:
            request = _build_oversized_summary_request(
                included_runs[0],
                history_settings=history_settings,
                max_prefix_tokens=remaining + _estimate_run_chain_tokens(included_runs[0], history_settings),
                max_input_tokens=max_input_tokens,
                previous_summary=previous_summary,
            )
            if request is not None:
                return request, included_runs
        included_runs.pop()

    return None, []


def compaction_summary_instruction(previous_summary: str | None) -> str:
    """Return the final user instruction appended by compaction transforms."""
    summary = previous_summary.strip() if previous_summary is not None and previous_summary.strip() else "None."
    return _COMPACTION_SUMMARY_INSTRUCTION.format(previous_summary=summary)


def _summary_instruction_tokens(previous_summary: str | None) -> int:
    return estimate_history_messages_tokens(
        [Message(role="user", content=compaction_summary_instruction(previous_summary))],
    )


def validate_tool_result_adjacency(messages: Sequence[Message]) -> None:
    """Validate that a tool result still directly follows the assistant tool call it answers."""
    expected_tool_call_ids: list[str] = []
    for message in messages:
        if expected_tool_call_ids:
            if message.role != "tool" or message.tool_call_id not in expected_tool_call_ids:
                msg = "tool result adjacency would be broken by prepared-chain transform"
                raise ValueError(msg)
            expected_tool_call_ids.remove(message.tool_call_id)
            continue
        if message.role == "assistant":
            expected_tool_call_ids = _tool_call_ids(message)
            continue
        if message.role == "tool":
            msg = "tool result appears without an adjacent assistant tool call"
            raise ValueError(msg)
    if expected_tool_call_ids:
        msg = "tool result adjacency would be broken by prepared-chain transform"
        raise ValueError(msg)


def compaction_replay_messages(
    run: RunOutput | TeamRunOutput,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    """Return the replayable message chain for one persisted run."""
    skip_roles = set(history_skip_roles(history_settings) or [])
    messages = [deepcopy(message) for message in run.messages or [] if message.role not in skip_roles]
    if history_settings.max_tool_calls_from_history is not None and messages:
        filter_tool_calls(messages, history_settings.max_tool_calls_from_history)
    strip_stale_anthropic_replay_fields(messages)
    return messages


def history_messages_for_session(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    """Return materialized replay messages for one persisted session scope."""
    session_messages = session_history_messages(
        session=session,
        scope=scope,
        history_settings=history_settings,
    )
    history_messages = [deepcopy(message) for message in session_messages]
    if history_settings.max_tool_calls_from_history is not None and history_messages:
        filter_tool_calls(history_messages, history_settings.max_tool_calls_from_history)
    strip_stale_anthropic_replay_fields(history_messages)
    return history_messages


def session_history_messages(
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    history_settings: ResolvedHistorySettings,
) -> list[Message]:
    """Return Agno-selected persisted messages before MindRoom replay sanitization."""
    limit = history_settings.policy.limit
    if scope.kind == "team":
        return _team_session_history_messages(
            session=cast("TeamSession", session),
            scope_id=scope.scope_id,
            history_settings=history_settings,
            limit=limit,
        )
    return _agent_session_history_messages(
        session=cast("AgentSession", session),
        scope_id=scope.scope_id,
        history_settings=history_settings,
        limit=limit,
    )


def history_skip_roles(history_settings: ResolvedHistorySettings) -> list[str] | None:
    """Return the effective Agno skip_roles filter for persisted history replay."""
    if not history_settings.skip_history_system_role:
        return None
    if history_settings.system_message_role in _STANDARD_HISTORY_ROLES:
        return None
    return [history_settings.system_message_role]


def render_message_content(message: Message) -> str:
    """Render one replayable string form of a message body."""
    content = message.compressed_content if message.compressed_content is not None else message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(stable_serialize(part) for part in content)
    if content is None:
        return ""
    return stable_serialize(content)


def estimate_history_messages_tokens(messages: list[Message]) -> int:
    """Estimate the token count of materialized history messages."""
    if not messages:
        return 0
    return sum(_estimated_message_chars(message) for message in messages) // 4


def strip_stale_anthropic_replay_fields(messages: list[Message]) -> int:
    """Strip stale Anthropic thinking replay fields from completed turns."""
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            last_user_idx = i
            break
    if last_user_idx < 0:
        return 0
    modified = 0
    for msg in messages[:last_user_idx]:
        if msg.role != "assistant":
            continue
        pd = msg.provider_data
        if not isinstance(pd, dict) or "signature" not in pd:
            continue
        msg.reasoning_content = None
        msg.redacted_reasoning_content = None
        del pd["signature"]
        modified += 1
    return modified


def message_media_entries(message: Message) -> tuple[tuple[str, object | None], ...]:
    """Return all media-bearing message fields as stable tag/value pairs."""
    return (
        ("images", message.images),
        ("audio", message.audio),
        ("videos", message.videos),
        ("files", message.files),
        ("audio_output", message.audio_output),
        ("image_output", message.image_output),
        ("video_output", message.video_output),
        ("file_output", message.file_output),
    )


def media_payload_snapshot(media_value: object) -> object:
    """Return media metadata without inline bytes for token estimation and logs."""
    if isinstance(media_value, BaseModel):
        payload = cast("dict[str, object]", media_value.model_dump(exclude_none=True))
        payload.pop("content", None)
        return payload
    if isinstance(media_value, Sequence) and not isinstance(media_value, (str, bytes, bytearray)):
        return [media_payload_snapshot(item) for item in media_value]
    return media_value


def _summary_request_from_chain(
    chain: PreparedConversationChain,
    *,
    previous_summary: str | None,
) -> CompactionSummaryRequest:
    return build_warm_cache_compaction_summary_request(chain, previous_summary=previous_summary)


def _build_compaction_summary_request_from_prefix(
    *,
    chain: PreparedConversationChain,
    prefix_messages: tuple[Message, ...],
    previous_summary: str | None,
    source: PreparedConversationChainSource,
) -> CompactionSummaryRequest:
    messages = [
        *[message.model_copy(deep=True) for message in prefix_messages],
        Message(role="user", content=compaction_summary_instruction(previous_summary)),
    ]
    strip_stale_anthropic_replay_fields(messages)
    sanitized_prefix = tuple(messages[:-1])
    request_messages = tuple(messages)
    rendered_text = render_prepared_messages_text(messages)
    return CompactionSummaryRequest(
        messages=request_messages,
        chain=replace(
            chain,
            source=source,
            messages=sanitized_prefix,
            rendered_text=render_prepared_messages_text(sanitized_prefix),
        ),
        included_run_ids=chain.source_run_ids,
        rendered_text=rendered_text,
        estimated_tokens=estimate_history_messages_tokens(list(messages)),
    )


def _estimate_run_chain_tokens(run: RunOutput | TeamRunOutput, history_settings: ResolvedHistorySettings) -> int:
    return estimate_history_messages_tokens(compaction_replay_messages(run, history_settings))


def _build_oversized_summary_request(
    run: RunOutput | TeamRunOutput,
    *,
    history_settings: ResolvedHistorySettings,
    max_prefix_tokens: int,
    max_input_tokens: int,
    previous_summary: str | None,
) -> CompactionSummaryRequest | None:
    budget = max_prefix_tokens
    while budget > 0:
        chain = _build_oversized_run_chain(
            run,
            history_settings=history_settings,
            max_tokens=budget,
        )
        if not chain.messages:
            return None
        request = _summary_request_from_chain(
            chain,
            previous_summary=previous_summary,
        )
        if request.estimated_tokens <= max_input_tokens:
            return request
        budget -= max(1, request.estimated_tokens - max_input_tokens)
    return None


def _build_oversized_run_chain(
    run: RunOutput | TeamRunOutput,
    *,
    history_settings: ResolvedHistorySettings,
    max_tokens: int,
) -> PreparedConversationChain:
    note = Message(role="user", content=_OVERSIZED_RUN_NOTE)
    messages: list[Message] = [note]
    remaining_chars = max(0, max_tokens * 4 - _estimated_message_chars(note))
    included_content_messages = 0
    for message in compaction_replay_messages(run, history_settings):
        if remaining_chars <= 0:
            break
        content = _oversized_excerpt_content(message)
        if not content:
            continue
        if len(content) > remaining_chars:
            content = _truncate_excerpt(content, remaining_chars)
        if not content:
            continue
        copied = _plain_oversized_excerpt_message(message, content)
        messages.append(copied)
        included_content_messages += 1
        remaining_chars -= _estimated_message_chars(copied)
    if included_content_messages == 0:
        return PreparedConversationChain(
            messages=(),
            rendered_text="",
            source="persisted_runs",
            source_run_ids=(),
            estimated_tokens=0,
        )
    source_run_ids = (run.run_id,) if isinstance(run.run_id, str) and run.run_id else ()
    return PreparedConversationChain(
        messages=tuple(messages),
        rendered_text=render_prepared_messages_text(messages),
        source="persisted_runs",
        source_run_ids=source_run_ids,
        estimated_tokens=estimate_history_messages_tokens(messages),
    )


def _oversized_excerpt_content(message: Message) -> str:
    parts: list[str] = []
    content = render_message_content(message)
    if content:
        if message.role == "tool":
            tool_label = f"Tool result for {message.tool_call_id}" if message.tool_call_id else "Tool result"
            content = f"{tool_label}:\n{content}"
        parts.append(content)
    if message.tool_calls:
        parts.append(f"Tool calls: {stable_serialize(message.tool_calls)}")
    for tag, media_value in message_media_entries(message):
        if media_value is None:
            continue
        parts.append(f"{tag}: {stable_serialize(media_payload_snapshot(media_value))}")
    return "\n".join(parts)


def _plain_oversized_excerpt_message(message: Message, content: str) -> Message:
    role = message.role if message.role in {"assistant", "system", "user"} else "user"
    return Message(role=role, content=content)


def classify_partial_reply(
    msg: ResolvedVisibleMessage,
    *,
    active_event_ids: Collection[str],
) -> PartialReplyKind | None:
    """Classify a self-authored partial reply from persisted stream metadata first."""
    status = msg.stream_status
    if status == STREAM_STATUS_COMPLETED:
        return None

    partial_kind: PartialReplyKind | None = None
    if status in {STREAM_STATUS_CANCELLED, STREAM_STATUS_ERROR, STREAM_STATUS_INTERRUPTED}:
        partial_kind = PartialReplyKind.INTERRUPTED
    elif status in {STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING}:
        event_id = msg.event_id
        if isinstance(event_id, str):
            return PartialReplyKind.IN_PROGRESS if event_id in active_event_ids else PartialReplyKind.INTERRUPTED
        partial_kind = PartialReplyKind.IN_PROGRESS
    else:
        body = msg.body
        if is_interrupted_partial_reply(body):
            partial_kind = PartialReplyKind.INTERRUPTED

    return partial_kind


def _build_unseen_messages_header(partial_reply_kinds: set[PartialReplyKind]) -> str:
    """Choose the unseen-context guidance for the partial-reply mix present."""
    if not partial_reply_kinds:
        return _DEFAULT_UNSEEN_MESSAGES_HEADER
    if partial_reply_kinds == {PartialReplyKind.INTERRUPTED}:
        return _INTERRUPTED_PARTIAL_REPLY_HEADER
    if partial_reply_kinds == {PartialReplyKind.IN_PROGRESS}:
        return _IN_PROGRESS_PARTIAL_REPLY_HEADER
    return _MIXED_PARTIAL_REPLY_HEADER


def _agent_session_history_messages(
    *,
    session: AgentSession,
    scope_id: str,
    history_settings: ResolvedHistorySettings,
    limit: int | None,
) -> list[Message]:
    skip_roles = history_skip_roles(history_settings)
    if history_settings.policy.mode == "runs":
        return session.get_messages(agent_id=scope_id, last_n_runs=limit, skip_roles=skip_roles)
    if history_settings.policy.mode == "messages":
        return session.get_messages(agent_id=scope_id, limit=limit, skip_roles=skip_roles)
    return session.get_messages(agent_id=scope_id, skip_roles=skip_roles)


def _team_session_history_messages(
    *,
    session: TeamSession,
    scope_id: str,
    history_settings: ResolvedHistorySettings,
    limit: int | None,
) -> list[Message]:
    skip_roles = history_skip_roles(history_settings)
    if history_settings.policy.mode == "runs":
        return session.get_messages(team_id=scope_id, last_n_runs=limit, skip_roles=skip_roles)
    if history_settings.policy.mode == "messages":
        return session.get_messages(team_id=scope_id, limit=limit, skip_roles=skip_roles)
    return session.get_messages(team_id=scope_id, skip_roles=skip_roles)


def _tool_call_ids(message: Message) -> list[str]:
    ids: list[str] = []
    for tool_call in message.tool_calls or []:
        tool_call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
        if isinstance(tool_call_id, str) and tool_call_id:
            ids.append(tool_call_id)
    return ids


def _estimated_message_chars(message: Message) -> int:
    content_chars = len(render_message_content(message))
    tool_call_chars = len(stable_serialize(message.tool_calls)) if message.tool_calls else 0
    return content_chars + tool_call_chars + _estimate_message_media_chars(message)


def _estimate_message_media_chars(message: Message) -> int:
    media_chars = 0
    for _tag, media_value in message_media_entries(message):
        if media_value is None:
            continue
        media_chars += len(stable_serialize(media_payload_snapshot(media_value)))
    return media_chars


def _truncate_excerpt(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return "…"
    return f"{text[: max_chars - 1].rstrip()}…"
