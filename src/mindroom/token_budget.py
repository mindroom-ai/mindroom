"""Shared compaction budget math and the stable serialization counted text is built from."""

from __future__ import annotations

import json

import tiktoken


def compute_compaction_input_budget(
    context_window: int,
    *,
    reserve_tokens: int,
    prompt_overhead_tokens: int = 2000,
    safety_margin_ratio: float = 0.10,
) -> int:
    """Compute the max input tokens available for a compaction summary request.

    Subtracts output reserve, prompt overhead (system prompt + response format),
    and a safety margin from the compaction model's context window.
    """
    safety = int(context_window * safety_margin_ratio)
    budget = context_window - reserve_tokens - prompt_overhead_tokens - safety
    return max(0, budget)


def stable_serialize(value: object) -> str:
    """Serialize arbitrary values into a stable JSON-ish string."""
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def o200k_encoding() -> tiktoken.Encoding:
    """Return the shared o200k_base encoding; building it on first use takes about 300 ms."""
    return tiktoken.get_encoding("o200k_base")


def approximate_o200k_tokens(value: str) -> int:
    """Approximate token count with the o200k_base encoding."""
    return len(o200k_encoding().encode(value, disallowed_special=()))
