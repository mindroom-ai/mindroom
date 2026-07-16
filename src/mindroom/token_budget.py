"""Shared token estimation and compaction budget helpers.

Kept deliberately narrow: only generic token math lives here.
Agno replay helpers and compaction serialization stay in their own modules.
"""

from __future__ import annotations

import json
from functools import lru_cache

import tiktoken


def effective_input_budget(
    context_window_tokens: int,
    *,
    configured_reserve_tokens: int,
    model_max_tokens: int | None,
) -> int:
    """Return input capacity after the larger configured or model output reserve."""
    configured_reserve = max(0, configured_reserve_tokens)
    model_reserve = max(0, model_max_tokens or 0)
    return max(0, context_window_tokens - max(configured_reserve, model_reserve))


def estimate_text_tokens(value: str | list[str] | None) -> int:
    """Estimate token count using chars / 4."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value) // 4
    if isinstance(value, list):
        return sum(len(stable_serialize(part)) for part in value) // 4
    return len(stable_serialize(value)) // 4


@lru_cache(maxsize=16)
def _compaction_encoding(model_id: str | None) -> tiktoken.Encoding:
    if model_id:
        try:
            return tiktoken.encoding_for_model(model_id)
        except KeyError:
            pass
    return tiktoken.get_encoding("o200k_base")


def estimate_compaction_input_tokens(value: str, *, model_id: str | None = None) -> int:
    """Estimate serialized compaction history with the closest tiktoken encoding."""
    encoding = _compaction_encoding(model_id)
    return len(encoding.encode(value, disallowed_special=()))


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
