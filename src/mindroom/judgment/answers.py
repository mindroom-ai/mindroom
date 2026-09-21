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
]


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    """A strictly validated probability that participation would be useful."""

    probability: float


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """The provider's nonnegative token counters."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class JudgmentResponse:
    """One complete response from the pinned evaluated model."""

    model: str
    answer: NoulAnswer
    usage: TokenUsage


@dataclass(frozen=True, slots=True)
class JudgmentResult:
    """A successful answer or one closed failure category."""

    answer: NoulAnswer | None
    failure: JudgmentFailure | None
    model_id: str | None
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    state_bytes: int
