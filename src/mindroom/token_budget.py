"""Shared token estimation and compaction budget helpers.

Kept deliberately narrow: only generic token math lives here.
Agno replay helpers and compaction serialization stay in their own modules.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Literal, Protocol, cast, runtime_checkable

import tiktoken

if TYPE_CHECKING:
    from agno.models.base import Model


_TokenEstimateConfidence = Literal["high", "low"]


@dataclass(frozen=True)
class _CompactionTokenEstimator:
    """Local token estimator selected for one compaction model.

    Model IDs recognized by tiktoken use their model-specific encoding. All
    other IDs, including Claude IDs, use one token per UTF-8 byte as a
    conservative local upper bound. The latter intentionally trades input
    capacity for avoiding tokenizer undercounts and never adds a network request
    to compaction.
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
class _ModelWithRequestParams(Protocol):
    """Typed surface for models that expose their normalized request."""

    def get_request_params(self) -> Mapping[str, object]:
        """Return provider-shaped request parameters."""


@runtime_checkable
class _ConfigWithMaxOutputTokens(Protocol):
    """Typed surface for Gemini SDK request config objects."""

    max_output_tokens: int | None


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
    model_id: str | None = None,
) -> _CompactionTokenEstimator:
    """Use a known tiktoken encoding or a conservative local fallback."""
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

    return _CompactionTokenEstimator(method="utf8_bytes_conservative", confidence="low")


def estimate_compaction_input_tokens(
    value: str,
    *,
    model_id: str | None = None,
) -> int:
    """Estimate serialized compaction history with a local model-aware estimator."""
    return compaction_token_estimator(model_id=model_id).estimate(value)


def configured_model_max_output_tokens(model: Model) -> int | None:
    """Return the largest positive output cap in a loaded model's effective request."""
    if not isinstance(model, _ModelWithRequestParams):
        return None
    request_params = model.get_request_params()
    cap_parameter_names = ("max_tokens", "max_output_tokens", "max_completion_tokens")
    candidates = {parameter_name: request_params.get(parameter_name) for parameter_name in cap_parameter_names}
    extra_body = request_params.get("extra_body")
    if isinstance(extra_body, Mapping):
        extra_body_mapping = cast("Mapping[str, object]", extra_body)
        for parameter_name in cap_parameter_names:
            if parameter_name in extra_body_mapping:
                candidates[parameter_name] = extra_body_mapping[parameter_name]
    request_options = request_params.get("options")
    if isinstance(request_options, Mapping):
        candidates["num_predict"] = cast("Mapping[str, object]", request_options).get("num_predict")
    request_config = request_params.get("config")
    if isinstance(request_config, Mapping):
        candidates["config.max_output_tokens"] = cast("Mapping[str, object]", request_config).get(
            "max_output_tokens",
        )
    elif isinstance(request_config, _ConfigWithMaxOutputTokens):
        candidates["config.max_output_tokens"] = request_config.max_output_tokens
    positive_caps = [
        cap for cap in candidates.values() if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0
    ]
    return max(positive_caps, default=None)


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
