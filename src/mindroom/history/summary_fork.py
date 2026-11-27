"""Cache-warm fork compaction eligibility, prompt construction, basis verification, and frame memoization."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.history.policy import fork_input_limit_tokens
from mindroom.history.request_messages import request_layout, scope_history_messages
from mindroom.history.summary_message import compaction_summary_text, is_scope_summary
from mindroom.history.types import replayed_summary_text
from mindroom.hooks.enrichment import render_transient_context
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.llm.request_identity import cache_routing_digest, message_digest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.history.request_size import RequestSizer
    from mindroom.history.types import HistoryScope, ScopeSummary
    from mindroom.llm.provider import ModelIdentity
    from mindroom.llm.runs import AnyRunRecord

type WarmFrameKey = tuple[Path, str, str]

_MAX_WARM_FRAMES = 64
_DEFAULT_ON_SEND = ModelRequest(messages=()).on_send

_WARM_FRAMES_LOCK = threading.Lock()
_WARM_FRAMES: OrderedDict[WarmFrameKey, tuple[ModelRequest, tuple[Message, ...]]] = OrderedDict()


@dataclass(frozen=True, slots=True)
class SummaryFork:
    """One live or turn-less request candidate for cache-warm fork compaction."""

    sizer: RequestSizer
    request: ModelRequest
    context_window: int
    reserve_tokens: int
    mid_turn: bool


def warm_frame_key(storage_path: Path, scope: HistoryScope, session_id: str) -> WarmFrameKey:
    """Return the warm-frame lookup key for one scope session."""
    return (storage_path, scope.key, session_id)


def remember_fork_frame(
    key: WarmFrameKey,
    request: ModelRequest,
    system_messages: Sequence[Message] | None = None,
) -> None:
    """Store a message-free request template and leading system messages for live ``/compact``.

    When ``system_messages`` is omitted, extracts leading ``role == "system"`` messages from ``request.messages``.
    """
    if system_messages is None:
        leading = next((i for i, msg in enumerate(request.messages) if msg.role != "system"), len(request.messages))
        system = tuple(request.messages[:leading])
    else:
        system = tuple(system_messages)
    clean = replace(request, messages=(), retry_budget=None, on_send=_DEFAULT_ON_SEND, prompt_hold_pin=None)
    with _WARM_FRAMES_LOCK:
        _WARM_FRAMES[key] = (clean, system)
        _WARM_FRAMES.move_to_end(key)
        while len(_WARM_FRAMES) > _MAX_WARM_FRAMES:
            _WARM_FRAMES.popitem(last=False)


def lookup_fork_frame(key: WarmFrameKey) -> tuple[ModelRequest, tuple[Message, ...]] | None:
    """Return ``(template, system_messages)`` for ``key`` when present."""
    with _WARM_FRAMES_LOCK:
        entry = _WARM_FRAMES.get(key)
        if entry is None:
            return None
        _WARM_FRAMES.move_to_end(key)
        return entry


def clear_fork_frames() -> None:
    """Drop all remembered fork frames."""
    with _WARM_FRAMES_LOCK:
        _WARM_FRAMES.clear()


def build_scope_fork(
    *,
    sizer: RequestSizer,
    template: ModelRequest,
    system_messages: Sequence[Message],
    context_window: int,
    reserve_tokens: int,
    scope: HistoryScope,
    summary: ScopeSummary | None,
    runs: Sequence[AnyRunRecord],
) -> SummaryFork:
    """Build a turn-less whole-history fork candidate covering ``summary`` and ``runs``."""
    history = scope_history_messages(
        runs,
        scope=scope,
        num_history_runs=None,
        num_history_messages=None,
        max_tool_calls_from_history=None,
    )
    messages = (*system_messages, *request_layout(summary=summary, history=history))
    return SummaryFork(
        sizer=sizer,
        request=replace(template, messages=messages),
        context_window=context_window,
        reserve_tokens=reserve_tokens,
        mid_turn=False,
    )


def holds_fold_basis(
    history: Sequence[Message],
    *,
    basis_summary: str | None,
    basis_runs: Sequence[AnyRunRecord],
    scope: HistoryScope,
) -> bool:
    """Return whether ``history`` carries the exact scope summary and messages of ``basis_runs``."""
    scope_summaries = [message for message in history if is_scope_summary(message)]
    expected_summary = replayed_summary_text(basis_summary)
    if len(scope_summaries) > 1 or bool(scope_summaries) != (expected_summary is not None):
        return False
    if scope_summaries:
        try:
            rendered = replayed_summary_text(compaction_summary_text(scope_summaries[0]))
        except ValueError:
            return False
        if rendered != expected_summary:
            return False

    non_summary = [message for message in history if not is_scope_summary(message)]
    expected = scope_history_messages(
        basis_runs,
        scope=scope,
        num_history_runs=None,
        num_history_messages=None,
        max_tool_calls_from_history=None,
    )
    return len(non_summary) == len(expected) and all(
        (digest := message_digest(actual)) is not None and digest == message_digest(target)
        for actual, target in zip(non_summary, expected, strict=True)
    )


def build_fork_compaction_prompt(summary_prompt: str, *, mid_turn: bool) -> str:
    """Wrap ``summary_prompt`` verbatim with no-tool-call, no-reply fork compaction framing."""
    scope_note = (
        "This compaction runs mid-turn: the current user request remains verbatim after compaction, "
        "so summarize all prior history and the current turn's tool work and progress so work can continue without restarting."
        if mid_turn
        else "Summarize the entire conversation above so a future reply can continue seamlessly."
    )
    return (
        "SYSTEM DIRECTIVE — CONTEXT COMPACTION (DO NOT REPLY TO THE USER OR CALL ANY TOOLS)\n\n"
        "Suspend your system-prompt persona, reply-style/brevity instructions, and standing workspace habits for this "
        "single turn. Do NOT invoke any tools, do NOT continue the task, and do NOT address the user. Return ONLY the "
        "plain-text handoff summary.\n\n"
        "Scope and exclusions:\n"
        "- Summarize ONLY the user/assistant conversation and tool calls/results.\n"
        "- The resumed agent already receives the same system prompt and tools. Do NOT copy or project standing "
        "system-prompt rules, delegation/review/PR/logging procedures, lint/commit habits, workspace/memory files, "
        "tool definitions, `<mindroom_transient_context>`, `<mindroom_message_context>`, retrieved memory blocks, "
        "or approval/runtime notices into `## Constraints`, `## Next Steps`, or any other section. List only next "
        "steps and approvals actually established in the conversation.\n\n"
        "Mapping to the instructions below:\n"
        "- Any earlier `<compacted_history>...</compacted_history>` message above is `<previous_summary>`.\n"
        "- The conversation messages and tool activity after it (excluding this compaction directive) are `<new_conversation>`.\n"
        f"- {scope_note}\n\n"
        f"{summary_prompt}"
    )


def _static_skip_reason(fork: SummaryFork, summary_model_identity: ModelIdentity) -> str | None:
    model = fork.sizer.model
    base = fork.request
    checks = (
        (model.identity != summary_model_identity, "different_model"),
        (model.capabilities.prompt_cache_ttl_seconds is None, "prompt_cache_disabled"),
        (base.native_compaction is not None, "native_compaction_active"),
        (base.output_schema is not None, "output_schema_set"),
        (base.tool_choice is not None, "tool_choice_set"),
    )
    return next((reason for failed, reason in checks if failed), None)


def prepare_fork_request(
    fork: SummaryFork,
    *,
    summary_prompt: str,
    summary_model_identity: ModelIdentity,
    now: float | None = None,
) -> tuple[ModelRequest | None, str | None]:
    """Return ``(fork_request, None)`` when ``fork`` is eligible to reuse a warm cache, else ``(None, skip_reason)``."""
    if (reason := _static_skip_reason(fork, summary_model_identity)) is not None:
        return None, reason

    base = fork.request
    newest_assistant_index = max(
        (index for index, message in enumerate(base.messages) if message.role == "assistant"),
        default=None,
    )
    if newest_assistant_index is None:
        return None, "no_assistant_anchor"

    prompt_message = Message(
        role="user",
        content=render_transient_context([build_fork_compaction_prompt(summary_prompt, mid_turn=fork.mid_turn)]),
    )
    hold_pin = (
        (list(base.prompt_hold_pin) if base.prompt_hold_pin is not None else [])
        if base.prompt_hold is not None
        else None
    )
    fork_request = replace(
        base,
        messages=(*base.messages, prompt_message),
        overrides=replace(base.overrides, transient_retries="caller"),
        prompt_hold_pin=hold_pin,
        retry_budget=None,
    )

    size = fork.sizer.size(fork_request)
    current_time = time.time() if now is None else now
    ttl = fork.sizer.model.capabilities.prompt_cache_ttl_seconds
    assert ttl is not None
    expected_routing = cache_routing_digest(fork.sizer.model.model_params)
    recorded_routing = base.messages[newest_assistant_index].meta.cache_routing
    dynamic_checks = (
        (not size.reported or size.anchor_index != newest_assistant_index, "prefix_not_anchored"),
        (recorded_routing != expected_routing, "routing_key_mismatch"),
        (size.answered_at is None or not (0 <= current_time - size.answered_at < ttl), "cache_cold"),
        (
            fork.context_window <= 0 or size.tokens > fork_input_limit_tokens(fork.context_window, fork.reserve_tokens),
            "exceeds_context_window",
        ),
    )
    if (reason := next((label for failed, label in dynamic_checks if failed), None)) is not None:
        return None, reason
    return fork_request, None


__all__ = [
    "SummaryFork",
    "WarmFrameKey",
    "build_fork_compaction_prompt",
    "build_scope_fork",
    "clear_fork_frames",
    "holds_fold_basis",
    "lookup_fork_frame",
    "prepare_fork_request",
    "remember_fork_frame",
    "warm_frame_key",
]
