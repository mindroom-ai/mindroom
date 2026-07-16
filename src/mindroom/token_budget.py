"""Shared token estimation and compaction budget helpers.

Kept deliberately narrow: only generic token math lives here.
Agno replay helpers and compaction serialization stay in their own modules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

import tiktoken

if TYPE_CHECKING:
    from agno.models.base import Model


_TokenEstimateConfidence = Literal["high", "low"]


@dataclass(frozen=True)
class _CompactionTokenEstimator:
    """Local token estimator selected for one compaction model.

    Known tiktoken model IDs use their model-specific encoding. Unknown models,
    including Claude-family models whose exact counter is a provider API call,
    use one token per UTF-8 byte as a conservative local upper bound. The latter
    intentionally trades input capacity for avoiding tokenizer undercounts and
    never adds a network request to compaction.
    """

    method: str
    confidence: _TokenEstimateConfidence
    _encoding: tiktoken.Encoding | None = field(default=None, repr=False)

    def estimate(self, value: str) -> int:
        """Estimate tokens without logging or retaining input contents."""
        if self._encoding is not None:
            return len(self._encoding.encode(value, disallowed_special=()))
        return len(value.encode("utf-8"))


@runtime_checkable
class _ModelWithMaxTokens(Protocol):
    """Typed surface shared by loaded models with an authored output cap."""

    max_tokens: int | None


def estimate_text_tokens(value: str | list[str] | None) -> int:
    """Estimate token count using chars / 4."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value) // 4
    if isinstance(value, list):
        return sum(len(stable_serialize(part)) for part in value) // 4
    return len(stable_serialize(value)) // 4


@lru_cache(maxsize=32)
def compaction_token_estimator(
    *,
    provider: str | None = None,
    model_id: str | None = None,
) -> _CompactionTokenEstimator:
    """Select a provider/model-aware local estimator for compaction input."""
    if model_id:
        try:
            encoding = tiktoken.encoding_for_model(model_id)
        except KeyError:
            pass
        else:
            return _CompactionTokenEstimator(
                method=f"tiktoken:{encoding.name}",
                confidence="high",
                _encoding=encoding,
            )

    normalized_provider = (provider or "").strip().lower().replace("-", "_")
    is_claude_family = "claude" in normalized_provider or "claude" in (model_id or "").lower()
    family = "claude" if is_claude_family else "unknown"
    return _CompactionTokenEstimator(
        method=f"utf8_bytes_{family}_conservative",
        confidence="low",
    )


def estimate_compaction_input_tokens(
    value: str,
    *,
    provider: str | None = None,
    model_id: str | None = None,
) -> int:
    """Estimate serialized compaction history with a local model-aware estimator."""
    return compaction_token_estimator(provider=provider, model_id=model_id).estimate(value)


def configured_model_max_output_tokens(model: Model) -> int | None:
    """Return a loaded model's positive ``max_tokens`` cap when its type exposes one."""
    if not isinstance(model, _ModelWithMaxTokens):
        return None
    max_tokens = model.max_tokens
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        return None
    return max_tokens


def compute_compaction_input_budget(
    context_window: int,
    *,
    reserve_tokens: int,
    model_max_output_tokens: int | None = None,
    prompt_overhead_tokens: int = 2000,
    safety_margin_ratio: float = 0.10,
) -> int:
    """Compute the max input tokens available for a compaction summary request.

    Subtracts effective output reserve, prompt overhead (system prompt + response
    format), and a safety margin from the compaction model's context window. A
    loaded model's positive max output cap is never allowed to reserve less than
    the generic configured reserve.
    """
    safety = int(context_window * safety_margin_ratio)
    effective_reserve = max(reserve_tokens, model_max_output_tokens or 0)
    budget = context_window - effective_reserve - prompt_overhead_tokens - safety
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
