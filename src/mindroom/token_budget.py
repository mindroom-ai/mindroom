"""Shared token estimation and compaction budget helpers.

Kept deliberately narrow: only generic token math lives here.
Agno replay helpers and compaction serialization stay in their own modules.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

import tiktoken

CompactionEstimateKind = Literal[
    "model_tiktoken_tokens",
    "o200k_base_tokens",
    "utf8_bytes_token_upper_bound",
]


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
def _compaction_encoding(model_id: str | None) -> tiktoken.Encoding | None:
    if model_id:
        try:
            return tiktoken.encoding_for_model(model_id)
        except KeyError:
            pass
    return None


def compaction_estimate_kind(
    model_id: str | None,
    *,
    conservative_fallback: bool = False,
) -> CompactionEstimateKind:
    """Describe the unchanged sizing strategy used for one compaction model."""
    if _compaction_encoding(model_id) is not None:
        return "model_tiktoken_tokens"
    if conservative_fallback:
        return "utf8_bytes_token_upper_bound"
    return "o200k_base_tokens"


def estimate_compaction_input_tokens(
    value: str,
    *,
    model_id: str | None = None,
    conservative_fallback: bool = False,
) -> int:
    """Estimate serialized compaction history using the selected sizing strategy."""
    kind = compaction_estimate_kind(model_id, conservative_fallback=conservative_fallback)
    if kind == "model_tiktoken_tokens":
        encoding = _compaction_encoding(model_id)
        assert encoding is not None
        return len(encoding.encode(value, disallowed_special=()))
    if kind == "utf8_bytes_token_upper_bound":
        return len(value.encode("utf-8", errors="surrogatepass"))
    return approximate_o200k_tokens(value)


def approximate_o200k_tokens(value: str) -> int:
    """Approximate token count with the o200k_base encoding."""
    return len(tiktoken.get_encoding("o200k_base").encode(value, disallowed_special=()))


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
