"""Cache-warm fork compaction eligibility, prompt construction, basis verification, and frame memoization."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.history.policy import context_budget_after_reserve
from mindroom.history.request_messages import request_layout, scope_history_messages
from mindroom.history.summary_message import compaction_summary_text, is_scope_summary
from mindroom.history.types import replayed_summary_text
from mindroom.hooks.enrichment import render_transient_context
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.llm.request_identity import message_digest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from mindroom.history.request_size import RequestSizer
    from mindroom.history.types import HistoryScope, ScopeSummary
    from mindroom.llm.provider import ModelIdentity
    from mindroom.llm.runs import AnyRunRecord

type WarmFrameKey = tuple[Path, str, str]

_STANDARD_CACHE_TTL_SECONDS = 300.0
_EXTENDED_CACHE_TTL_SECONDS = 3300.0
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


@dataclass(frozen=True, slots=True)
class ScopeForkSource:
    """Request frame metadata for building a turn-less whole-history scope compaction fork."""

    sizer: RequestSizer
    template: ModelRequest
    system_messages: tuple[Message, ...]
    context_window: int
    reserve_tokens: int


def warm_cache_ttl_seconds(model_params: Mapping[str, Any]) -> float:
    """Return the warm prompt-cache lifetime in seconds for ``model_params``."""
    if model_params.get("extended_cache_time") is True and model_params.get("cache_system_prompt") is True:
        return _EXTENDED_CACHE_TTL_SECONDS
    return _STANDARD_CACHE_TTL_SECONDS


def remember_fork_frame(key: WarmFrameKey, template: ModelRequest, system_messages: Sequence[Message]) -> None:
    """Store a message-free request template and leading system messages in a bounded LRU for live ``/compact``."""
    clean = replace(template, messages=(), retry_budget=None, on_send=_DEFAULT_ON_SEND, prompt_hold_pin=None)
    entry = (clean, tuple(system_messages))
    with _WARM_FRAMES_LOCK:
        _WARM_FRAMES[key] = entry
        _WARM_FRAMES.move_to_end(key)
        while len(_WARM_FRAMES) > _MAX_WARM_FRAMES:
            _WARM_FRAMES.popitem(last=False)


def lookup_fork_frame(key: WarmFrameKey) -> tuple[ModelRequest, tuple[Message, ...]] | None:
    """Return the remembered ``(template, system_messages)`` for ``key``, or ``None`` if absent."""
    with _WARM_FRAMES_LOCK:
        entry = _WARM_FRAMES.get(key)
        if entry is not None:
            _WARM_FRAMES.move_to_end(key)
        return entry


def clear_fork_frames() -> None:
    """Drop all remembered fork frames."""
    with _WARM_FRAMES_LOCK:
        _WARM_FRAMES.clear()


def build_scope_fork(
    source: ScopeForkSource,
    *,
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
    messages = (*source.system_messages, *request_layout(summary=summary, history=history))
    return SummaryFork(
        sizer=source.sizer,
        request=replace(source.template, messages=messages),
        context_window=source.context_window,
        reserve_tokens=source.reserve_tokens,
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
        "Stop normal task execution for this single turn. Do NOT invoke any tools, do NOT continue the task, "
        "and do NOT address the user. Return ONLY the plain-text handoff summary.\n\n"
        "Mapping to the instructions below:\n"
        "- Any earlier `<compacted_history>...</compacted_history>` message above is `<previous_summary>`.\n"
        "- The conversation messages and tool activity after it (excluding this compaction directive) are `<new_conversation>`.\n"
        f"- {scope_note}\n\n"
        f"{summary_prompt}"
    )


def _static_skip_reason(fork: SummaryFork, summary_model_identity: ModelIdentity) -> str | None:
    identity = fork.sizer.model.identity
    if identity != summary_model_identity:
        return "different_model"
    if identity.provider == "claude_code":
        return "unsupported_provider"
    base = fork.request
    if base.native_compaction is not None:
        return "native_compaction_active"
    if base.output_schema is not None:
        return "output_schema_set"
    if base.tool_choice is not None:
        return "tool_choice_set"
    return None


def _fits_context_window(tokens: int, *, context_window: int, reserve_tokens: int) -> bool:
    if context_window <= 0:
        return False
    effective_reserve = max(reserve_tokens, min(4_096, context_window // 2))
    fit_limit = context_budget_after_reserve(context_window, effective_reserve)
    safety_margin = min(4_096, context_window // 50)
    return tokens <= fit_limit - safety_margin


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
    fork_request = replace(
        base,
        messages=(*base.messages, prompt_message),
        overrides=replace(base.overrides, caller_retries=True),
        prompt_hold_pin=[] if base.prompt_hold is not None else None,
        retry_budget=None,
    )

    size = fork.sizer.size(fork_request)
    if not size.reported or size.anchor_index != newest_assistant_index:
        return None, "prefix_not_anchored"

    current_time = time.time() if now is None else now
    ttl = warm_cache_ttl_seconds(fork.sizer.model.model_params)
    if size.answered_at is None or not (0 <= current_time - size.answered_at < ttl):
        return None, "cache_cold"

    if not _fits_context_window(
        size.tokens,
        context_window=fork.context_window,
        reserve_tokens=fork.reserve_tokens,
    ):
        return None, "exceeds_context_window"

    return fork_request, None


__all__ = [
    "ScopeForkSource",
    "SummaryFork",
    "WarmFrameKey",
    "build_fork_compaction_prompt",
    "build_scope_fork",
    "clear_fork_frames",
    "holds_fold_basis",
    "lookup_fork_frame",
    "prepare_fork_request",
    "remember_fork_frame",
    "warm_cache_ttl_seconds",
]
