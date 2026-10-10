"""Bounded direct HTTP client for probability judgments from a fixed provider endpoint."""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx

from mindroom.json_utils import DuplicateJSONKeyError, object_with_unique_keys
from mindroom.judgment.answers import (
    ChoiceDecision,
    JudgmentError,
    JudgmentResponse,
    JudgmentResult,
    TokenUsage,
)
from mindroom.judgment.execution import SHARED_CAPACITY, JudgmentCapacity, run_judgment

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.judgment.state import JudgmentRequest

_MAX_RESPONSE_BYTES = 64 * 1024


class InvalidJudgmentResponseError(ValueError):
    """The response did not match the requested judgment contract."""


class JudgmentModelDriftError(InvalidJudgmentResponseError):
    """The provider returned a model other than the configured pin."""


class _ResponseTooLargeError(ValueError):
    """The decoded response crossed the configured byte ceiling."""


@dataclass(frozen=True, slots=True)
class WireChoice:
    """A provider's choice with its range-checked confidence and distribution."""

    choice: str
    confidence: float
    probabilities: dict[str, float]


@dataclass(frozen=True, slots=True)
class WireAnswer[T]:
    """One provider answer, or None when the provider declined to answer."""

    model: str
    usage: TokenUsage
    answer: T | None


@dataclass(frozen=True, slots=True)
class JudgmentWire:
    """One provider's endpoint and its request and answer shapes."""

    endpoint: str
    encode: Callable[[dict[str, Any]], dict[str, object]]
    decode_probability: Callable[[object, str], WireAnswer[float]]
    decode_choice: Callable[[object, str], WireAnswer[WireChoice]]


def _reject_constant(_value: str) -> object:
    msg = "response contains a non-finite number"
    raise InvalidJudgmentResponseError(msg)


def _parse_json(body: bytes) -> object:
    if len(body) > _MAX_RESPONSE_BYTES:
        msg = "response exceeds the byte limit"
        raise InvalidJudgmentResponseError(msg)
    try:
        return json.loads(body, object_pairs_hook=object_with_unique_keys, parse_constant=_reject_constant)
    except InvalidJudgmentResponseError:
        raise
    except DuplicateJSONKeyError as exc:
        msg = "response contains a duplicate JSON key"
        raise InvalidJudgmentResponseError(msg) from exc
    except (ValueError, RecursionError) as exc:
        msg = "response is not valid JSON"
        raise InvalidJudgmentResponseError(msg) from exc


def exact_keys(value: object, expected: set[str], label: str) -> dict[str, object]:
    """Return a JSON object with exactly the expected keys."""
    if not isinstance(value, dict) or set(value) != expected:
        msg = f"response {label} has unexpected keys"
        raise InvalidJudgmentResponseError(msg)
    return cast("dict[str, object]", value)


def probability(value: object, label: str) -> float:
    """Return a finite JSON number between zero and one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"response {label} must be a number"
        raise InvalidJudgmentResponseError(msg)
    if not 0 <= value <= 1:
        msg = f"response {label} is outside the allowed range"
        raise InvalidJudgmentResponseError(msg)
    result = float(value)
    if not math.isfinite(result):
        msg = f"response {label} must be a finite number"
        raise InvalidJudgmentResponseError(msg)
    return result


def token_usage(usage: dict[str, object]) -> TokenUsage:
    """Read the provider's nonnegative input and output token counters."""
    counts = [usage.get(key) for key in ("input_tokens", "output_tokens")]
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts):
        msg = "response token usage must be nonnegative integers"
        raise InvalidJudgmentResponseError(msg)
    return TokenUsage(input_tokens=cast("int", counts[0]), output_tokens=cast("int", counts[1]))


def _accept_probability(wire_answer: WireAnswer[float], threshold: float) -> JudgmentResponse[bool]:
    """Accept only the requested judgment probability, never a generated decision."""
    value = wire_answer.answer
    return JudgmentResponse(
        model=wire_answer.model,
        decision=None if value is None else value >= threshold,
        probability=value,
        usage=wire_answer.usage,
    )


def _accept_choice(
    wire_answer: WireAnswer[WireChoice],
    options: set[str],
    threshold: float,
) -> JudgmentResponse[ChoiceDecision]:
    """Validate the full distribution before accepting a unique, confident winner."""
    answer = wire_answer.answer
    if answer is None:
        return JudgmentResponse(model=wire_answer.model, decision=None, probability=None, usage=wire_answer.usage)
    probabilities = answer.probabilities
    if answer.choice not in options or set(probabilities) != options:
        msg = "response choice is not a requested option"
        raise InvalidJudgmentResponseError(msg)
    value = probabilities[answer.choice]
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=0.01) or value != max(probabilities.values()):
        msg = "response choice distribution is inconsistent"
        raise InvalidJudgmentResponseError(msg)
    unique = sum(other == value for other in probabilities.values()) == 1
    decision = (
        ChoiceDecision(answer.choice, answer.confidence, tuple(probabilities.items()))
        if unique and min(answer.confidence, value) >= threshold
        else None
    )
    return JudgmentResponse(model=wire_answer.model, decision=decision, probability=value, usage=wire_answer.usage)


@contextmanager
def _closed_failures() -> Iterator[None]:
    """Expose transport and contract failures only as closed categories."""
    try:
        yield
    except httpx.TimeoutException as error:
        raise JudgmentError(failure="timeout") from error
    except _ResponseTooLargeError as error:
        raise JudgmentError(failure="response_too_large") from error
    except httpx.HTTPStatusError as error:
        failure = "rate_limited" if error.response.status_code in {429, 529} else "http_error"
        raise JudgmentError(failure) from error
    except httpx.HTTPError as error:
        raise JudgmentError(failure="transport_error") from error
    except JudgmentModelDriftError as error:
        raise JudgmentError(failure="model_drift") from error
    except InvalidJudgmentResponseError as error:
        raise JudgmentError(failure="invalid_response") from error


class JudgmentClient:
    """Call one provider's fixed endpoint under strict local budgets."""

    def __init__(
        self,
        *,
        api_key: str,
        wire: JudgmentWire,
        timeout_seconds: float = 1.5,
        threshold: float = 0.8,
        transport: httpx.AsyncBaseTransport | None = None,
        capacity: JudgmentCapacity = SHARED_CAPACITY,
    ) -> None:
        if not api_key:
            msg = "a judgment API key is required"
            raise ValueError(msg)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            msg = "the judgment timeout must be finite and positive"
            raise ValueError(msg)
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            msg = "the judgment threshold must be between zero and one"
            raise ValueError(msg)
        self._threshold = threshold
        self._api_key = api_key
        self._wire = wire
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._capacity = capacity

    async def _post(self, body: bytes) -> bytes:
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout_seconds),
            trust_env=False,
        ) as client:
            outbound = client.build_request(
                "POST",
                self._wire.endpoint,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                content=body,
            )
            response = await client.send(outbound, stream=True)
            try:
                if response.status_code != httpx.codes.OK:
                    response.raise_for_status()
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > _MAX_RESPONSE_BYTES:
                        raise _ResponseTooLargeError
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                await response.aclose()

    def _wire_body(self, request: JudgmentRequest, *, choice: bool) -> tuple[dict[str, Any], bytes]:
        assert request.body is not None
        payload = json.loads(request.body)
        question = payload["question"]
        if (question.get("type") == "choice") != choice:
            raise JudgmentError(failure="invalid_request")
        # The validated request bounds the wire, which only reframes the same rubric and messages.
        body = json.dumps(self._wire.encode(payload), ensure_ascii=False, separators=(",", ":")).encode()
        return question, body

    async def _evaluate(self, request: JudgmentRequest) -> JudgmentResponse[bool]:
        question, body = self._wire_body(request, choice=False)
        with _closed_failures():
            root = _parse_json(await self._post(body))
            return _accept_probability(self._wire.decode_probability(root, question["id"]), self._threshold)

    async def _evaluate_choice(self, request: JudgmentRequest) -> JudgmentResponse[ChoiceDecision]:
        question, body = self._wire_body(request, choice=True)
        with _closed_failures():
            root = _parse_json(await self._post(body))
            return _accept_choice(
                self._wire.decode_choice(root, question["id"]),
                set(question["criteria"]),
                self._threshold,
            )

    async def judge(
        self,
        request: JudgmentRequest,
        *,
        owner: str,
        allow_network: bool = False,
    ) -> JudgmentResult[bool]:
        """Evaluate one question with the shared deadline and concurrency limits."""
        return await run_judgment(
            request,
            self._evaluate,
            owner=owner,
            timeout_seconds=self._timeout_seconds,
            allow_network=allow_network,
            capacity=self._capacity,
        )

    async def judge_choice(
        self,
        request: JudgmentRequest,
        *,
        owner: str,
        allow_network: bool = False,
    ) -> JudgmentResult[ChoiceDecision]:
        """Evaluate a choice under the same limits as boolean judgments."""
        return await run_judgment(
            request,
            self._evaluate_choice,
            owner=owner,
            timeout_seconds=self._timeout_seconds,
            allow_network=allow_network,
            capacity=self._capacity,
        )
