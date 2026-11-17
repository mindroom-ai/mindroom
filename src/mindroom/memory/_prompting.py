"""Memory prompt and conversation shaping helpers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from mindroom.prompt_templates import render_prompt_template
from mindroom.redaction import REDACTION_FAILED, redact_private_keys, redact_sensitive_text
from mindroom.token_counting.counts import count
from mindroom.token_counting.families import o200k_counter
from mindroom.tool_system.events import body_without_tool_markers

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

    from ._shared import MemoryResult

_USER_TURN_TIME_PREFIX_RE = re.compile(r"^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2} [^\]]+\]\s")
# Absorbs float rounding, so a hit exactly ``score_gap`` below the top hit is kept.
_SCORE_GAP_TOLERANCE = 1e-9
# The o200k tokens one turn's recalled memories may take in total.
_RECALL_TOKEN_BUDGET = 800
_O200K_COUNTER = o200k_counter()


def redact_memory_text(text: str) -> str:
    """Remove credential-like values, on a best-effort basis, from conversation text kept in memory automatically.

    Private keys, which span several lines, are removed whole first. Then each line is redacted on its own and a line
    redaction cannot read, one over 64 KiB, is dropped, so it does not cost the rest of the message; a value on the line
    after its key is not recognized.
    """
    lines = (redact_sensitive_text(line) for line in redact_private_keys(text).splitlines())
    return "\n".join(line for line in lines if line != REDACTION_FAILED)


def format_memories_as_context(
    memories: list[MemoryResult],
    context_type: str = "agent",
    *,
    prompt_template: str,
) -> str:
    """Format memories into a context string."""
    if not memories:
        return ""

    memory_lines = "\n".join(f"- {memory.get('memory', '')}" for memory in memories)
    return render_prompt_template(prompt_template, context_type=context_type, memory_lines=memory_lines)


def select_recall_hits(
    results: Sequence[MemoryResult],
    *,
    max_hits: int = 5,
    score_gap: float = 0.05,
    token_budget: int = _RECALL_TOKEN_BUDGET,
) -> list[MemoryResult]:
    """Return the leading recall hits worth injecting, in rank order.

    Hits are kept while their score is within ``score_gap`` (an absolute difference on the score scale) of the top
    hit's, up to ``max_hits``, and while their memory text fits ``token_budget`` o200k tokens in total.
    The first hit that fails any rule ends the selection, so a top hit larger than the budget selects nothing.
    A result without a score counts as score 0.
    Selection never waits for the tokenizer: until it has loaded, or when it cannot load, the budget estimates a
    quarter token per character.
    """
    if not results:
        return []
    top_score = results[0].get("score", 0.0)
    selected: list[MemoryResult] = []
    tokens = 0
    for result in results[:max_hits]:
        if top_score - result.get("score", 0.0) > score_gap + _SCORE_GAP_TOLERANCE:
            break
        tokens += _recall_tokens(result)
        if tokens > token_budget:
            break
        selected.append(result)
    return selected


def add_within_recall_budget(
    selected: Sequence[MemoryResult],
    extra: Sequence[MemoryResult],
    *,
    token_budget: int = _RECALL_TOKEN_BUDGET,
) -> list[MemoryResult]:
    """Return ``selected`` followed by the hits of ``extra``, in order, that fit the token budget ``selected`` left.

    A hit of ``extra`` that does not fit is dropped, and a later, smaller one may still fit.
    """
    remaining = token_budget - sum(_recall_tokens(result) for result in selected)
    added: list[MemoryResult] = []
    for result in extra:
        tokens = _recall_tokens(result)
        if tokens <= remaining:
            added.append(result)
            remaining -= tokens
    return [*selected, *added]


def _recall_tokens(result: MemoryResult) -> int:
    """Return the o200k tokens of a hit's memory text: exact once the encoder has loaded, estimated before."""
    return count(_O200K_COUNTER, result.get("memory", ""))


def strip_user_turn_time_prefix(text: str) -> str:
    """Remove bot-injected timestamp metadata from a user turn."""
    return _USER_TURN_TIME_PREFIX_RE.sub("", text, count=1)


def _build_conversation_messages(
    thread_history: Sequence[ResolvedVisibleMessage],
    current_prompt: str,
    user_id: str,
) -> list[dict]:
    messages: list[dict] = []
    for message in thread_history:
        role = "user" if message.sender == user_id else "assistant"
        body = redact_memory_text(body_without_tool_markers(message.body, message.content)).strip()
        if not body:
            continue
        messages.append({"role": role, "content": body})
    messages.append({"role": "user", "content": redact_memory_text(current_prompt)})
    return messages


def build_memory_messages(
    prompt: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    user_id: str | None,
) -> list[dict]:
    """Convert prompt and optional thread history into memory-save messages."""
    # Mem0 extracts and stores memories from these messages, so credential-like values are removed first.
    if thread_history and user_id:
        return _build_conversation_messages(thread_history, prompt, user_id)
    return [{"role": "user", "content": redact_memory_text(prompt)}]
