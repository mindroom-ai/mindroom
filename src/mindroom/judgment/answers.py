"""Typed values returned by the bounded judgment adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type QueuedChoice = Literal["finish", "wrap_up"]
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
class ChoiceQuestion:
    """One fixed Choice question and its ordered candidate rubric."""

    question_id: str
    instructions: str
    criteria: tuple[tuple[QueuedChoice, str], ...]


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    """A strictly validated finish-versus-wrap-up answer."""

    choice: QueuedChoice
    probabilities: tuple[tuple[QueuedChoice, float], ...]
    confidence: float


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
class JudgmentResponse[AnswerT]:
    """One complete response from the pinned evaluated model."""

    model: str
    answer: AnswerT
    usage: TokenUsage


@dataclass(frozen=True, slots=True)
class JudgmentResult[AnswerT]:
    """A successful answer or one closed failure category."""

    answer: AnswerT | None
    failure: JudgmentFailure | None
    model_id: str | None
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    state_bytes: int
