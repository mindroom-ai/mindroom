"""Typed values returned by the bounded judgment adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type JudgmentFailure = Literal[
    "capacity_exhausted",
    "http_error",
    "incomplete_state",
    "invalid_request",
    "invalid_response",
    "model_drift",
    "network_disabled",
    "rate_limited",
    "response_too_large",
    "timeout",
    "transport_error",
    "provider_error",
]


class JudgmentError(ValueError):
    """A backend failure safe to expose without provider text or credentials."""

    def __init__(self, failure: JudgmentFailure) -> None:
        super().__init__(failure)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """The provider's nonnegative token counters."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ChoiceDecision:
    """One allowlisted option, with provider probabilities when available."""

    option: str
    confidence: float | None = None
    probabilities: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True, slots=True)
class JudgmentResponse[T]:
    """One validated backend response, including an explicit abstention."""

    model: str
    decision: T | None
    probability: float | None
    usage: TokenUsage | None


@dataclass(frozen=True, slots=True)
class JudgmentResult[T]:
    """A successful answer or one closed failure category."""

    decision: T | None
    probability: float | None
    failure: JudgmentFailure | None
    model_id: str | None
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    state_bytes: int
