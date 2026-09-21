"""Bounded direct HTTP adapter for TypeSafe System One Choice judgments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from functools import partial
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING, cast

import httpx

from mindroom.judgment.answers import (
    ChoiceAnswer,
    JudgmentFailure,
    JudgmentResponse,
    JudgmentResult,
    NoulAnswer,
    QueuedChoice,
    TokenUsage,
)
from mindroom.judgment.state import MAX_REQUEST_BYTES, PINNED_MODEL, QUEUED_MESSAGE_QUESTION, JudgmentRequest

if TYPE_CHECKING:
    from collections.abc import Callable

_TYPE_SAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_MAX_RESPONSE_BYTES = 64 * 1024
_PROBABILITY_SUM_TOLERANCE = 1e-6
_EXPECTED_OPTIONS = ("finish", "wrap_up")


class InvalidJudgmentResponseError(ValueError):
    """The response did not match the exact evaluated Choice contract."""


class _ResponseTooLargeError(ValueError):
    """The decoded response crossed the configured byte ceiling."""


class JudgmentModelDriftError(InvalidJudgmentResponseError):
    """The provider returned a model other than the evaluated pin."""


@dataclass(frozen=True, slots=True)
class _CapacityLease:
    capacity: JudgmentCapacity
    owner: str

    def release(self) -> None:
        self.capacity.release(self.owner)


class JudgmentCapacity:
    """One process-safe, non-waiting global and per-owner request budget."""

    def __init__(self, *, max_concurrent: int, max_per_owner: int) -> None:
        if max_concurrent < 1 or max_per_owner < 1 or max_per_owner > max_concurrent:
            msg = "judgment capacity limits must be positive and per-owner must not exceed global"
            raise ValueError(msg)
        self._max_concurrent = max_concurrent
        self._max_per_owner = max_per_owner
        self._active = 0
        self._active_by_owner: dict[str, int] = {}
        self._lock = Lock()

    def acquire_nowait(self, owner: str) -> _CapacityLease | None:
        """Acquire immediately or return none without creating a waiter."""
        with self._lock:
            owner_active = self._active_by_owner.get(owner, 0)
            if self._active >= self._max_concurrent or owner_active >= self._max_per_owner:
                return None
            self._active += 1
            self._active_by_owner[owner] = owner_active + 1
        return _CapacityLease(self, owner)

    def release(self, owner: str) -> None:
        """Release one acquired owner slot."""
        with self._lock:
            owner_active = self._active_by_owner[owner]
            self._active -= 1
            if owner_active == 1:
                del self._active_by_owner[owner]
            else:
                self._active_by_owner[owner] = owner_active - 1


_SHARED_CAPACITY = JudgmentCapacity(max_concurrent=8, max_per_owner=1)


def _reject_constant(_value: str) -> object:
    msg = "response contains a non-finite number"
    raise InvalidJudgmentResponseError(msg)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            msg = "response contains a duplicate JSON key"
            raise InvalidJudgmentResponseError(msg)
        result[key] = value
    return result


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        msg = f"response {label} has unexpected keys"
        raise InvalidJudgmentResponseError(msg)
    return cast("dict[str, object]", value)


def _number(value: object, label: str) -> float:
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


def _token_count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = f"response {label} must be a nonnegative integer"
        raise InvalidJudgmentResponseError(msg)
    return value


def _decode_envelope(body: bytes, *, expected_model: str) -> tuple[dict[str, object], TokenUsage]:
    """Validate shared JSON, model pin, and usage for either fixed question."""
    if len(body) > _MAX_RESPONSE_BYTES:
        msg = "response exceeds the byte limit"
        raise InvalidJudgmentResponseError(msg)
    try:
        parsed = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except InvalidJudgmentResponseError:
        raise
    except (ValueError, RecursionError) as exc:
        msg = "response is not valid JSON"
        raise InvalidJudgmentResponseError(msg) from exc

    root = _exact_keys(parsed, {"model", "answers", "usage"}, "body")
    model = root["model"]
    if not isinstance(model, str) or model != expected_model:
        msg = "response model does not match the pinned evaluated model"
        raise JudgmentModelDriftError(msg)

    usage = _exact_keys(root["usage"], {"input_tokens", "output_tokens"}, "usage")
    token_usage = TokenUsage(
        input_tokens=_token_count(usage["input_tokens"], "input_tokens"),
        output_tokens=_token_count(usage["output_tokens"], "output_tokens"),
    )
    return root, token_usage


def decode_response(body: bytes, *, expected_model: str) -> JudgmentResponse[ChoiceAnswer]:
    """Strictly decode one live or recorded queued-message Choice response."""
    root, token_usage = _decode_envelope(body, expected_model=expected_model)
    answers = _exact_keys(root["answers"], {QUEUED_MESSAGE_QUESTION.question_id}, "question map")
    answer = _exact_keys(
        answers[QUEUED_MESSAGE_QUESTION.question_id],
        {"type", "choice", "probabilities", "confidence"},
        "choice answer",
    )
    if answer["type"] != "choice":
        msg = "response answer type is not choice"
        raise InvalidJudgmentResponseError(msg)
    choice = answer["choice"]
    if not isinstance(choice, str) or choice not in _EXPECTED_OPTIONS:
        msg = "response choice is not a declared option"
        raise InvalidJudgmentResponseError(msg)

    probabilities = _exact_keys(answer["probabilities"], set(_EXPECTED_OPTIONS), "probability map")
    finish = _number(probabilities["finish"], "finish probability")
    wrap_up = _number(probabilities["wrap_up"], "wrap_up probability")
    if not math.isclose(finish + wrap_up, 1.0, rel_tol=0.0, abs_tol=_PROBABILITY_SUM_TOLERANCE):
        msg = "response probabilities do not sum to one"
        raise InvalidJudgmentResponseError(msg)
    if choice == "finish" and finish < wrap_up:
        msg = "response choice is not a highest-probability option"
        raise InvalidJudgmentResponseError(msg)
    if choice == "wrap_up" and wrap_up < finish:
        msg = "response choice is not a highest-probability option"
        raise InvalidJudgmentResponseError(msg)
    confidence = _number(answer["confidence"], "confidence")

    return JudgmentResponse(
        model=expected_model,
        answer=ChoiceAnswer(
            choice=cast("QueuedChoice", choice),
            probabilities=(("finish", finish), ("wrap_up", wrap_up)),
            confidence=confidence,
        ),
        usage=token_usage,
    )


def _decode_participation_response(body: bytes, *, expected_model: str) -> JudgmentResponse[NoulAnswer]:
    """Accept only the requested participation probability, never a generated decision."""
    root, usage = _decode_envelope(body, expected_model=expected_model)
    answers = _exact_keys(root["answers"], {"participation"}, "question map")
    answer = _exact_keys(answers["participation"], {"type", "noul"}, "participation answer")
    if answer["type"] != "noul":
        msg = "response answer type is not noul"
        raise InvalidJudgmentResponseError(msg)
    return JudgmentResponse(
        model=expected_model,
        answer=NoulAnswer(probability=_number(answer["noul"], "participation probability")),
        usage=usage,
    )


class SystemOneClient:
    """Call the one fixed TypeSafe endpoint under strict local budgets."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 1.5,
        transport: httpx.AsyncBaseTransport | None = None,
        capacity: JudgmentCapacity = _SHARED_CAPACITY,
    ) -> None:
        if not api_key:
            msg = "a TypeSafe API key is required"
            raise ValueError(msg)
        if model != PINNED_MODEL:
            msg = "the judgment client requires the pinned evaluated model"
            raise ValueError(msg)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            msg = "the judgment timeout must be finite and positive"
            raise ValueError(msg)
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._capacity = capacity

    @staticmethod
    def _result[AnswerT](
        request: JudgmentRequest,
        started: float,
        *,
        failure: JudgmentFailure | None,
        response: JudgmentResponse[AnswerT] | None = None,
    ) -> JudgmentResult[AnswerT]:
        return JudgmentResult(
            answer=None if response is None else response.answer,
            failure=failure,
            model_id=None if response is None else response.model,
            latency_ms=max(0, round((perf_counter() - started) * 1000)),
            input_tokens=None if response is None else response.usage.input_tokens,
            output_tokens=None if response is None else response.usage.output_tokens,
            state_bytes=request.state_bytes,
        )

    async def _post(self, body: bytes) -> bytes:
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(self._timeout_seconds),
            trust_env=False,
        ) as client:
            outbound = client.build_request(
                "POST",
                _TYPE_SAFE_ENDPOINT,
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

    async def _evaluate[AnswerT](
        self,
        body: bytes,
        decode: Callable[[bytes], JudgmentResponse[AnswerT]],
    ) -> tuple[JudgmentResponse[AnswerT] | None, JudgmentFailure | None]:
        response: JudgmentResponse[AnswerT] | None = None
        failure: JudgmentFailure | None = None
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response_body = await self._post(body)
                response = decode(response_body)
        except (TimeoutError, httpx.TimeoutException):
            failure = "timeout"
        except _ResponseTooLargeError:
            failure = "response_too_large"
        except httpx.HTTPStatusError as exc:
            failure = "rate_limited" if exc.response.status_code in {429, 529} else "http_error"
        except httpx.HTTPError:
            failure = "transport_error"
        except JudgmentModelDriftError:
            failure = "model_drift"
        except InvalidJudgmentResponseError:
            failure = "invalid_response"
        return response, failure

    async def judge(
        self,
        request: JudgmentRequest,
        *,
        owner: str,
        allow_network: bool = False,
    ) -> JudgmentResult[ChoiceAnswer]:
        """Evaluate the fixed queued-message Choice contract."""
        return await self._judge(
            request,
            owner=owner,
            allow_network=allow_network,
            decode=partial(decode_response, expected_model=self._model),
        )

    async def judge_participation(
        self,
        request: JudgmentRequest,
        *,
        owner: str,
        allow_network: bool = False,
    ) -> JudgmentResult[NoulAnswer]:
        """Evaluate the fixed participation Noul contract with the same transport budgets."""
        return await self._judge(
            request,
            owner=owner,
            allow_network=allow_network,
            decode=partial(_decode_participation_response, expected_model=self._model),
        )

    async def _judge[AnswerT](
        self,
        request: JudgmentRequest,
        *,
        owner: str,
        allow_network: bool,
        decode: Callable[[bytes], JudgmentResponse[AnswerT]],
    ) -> JudgmentResult[AnswerT]:
        """Evaluate one complete request; failures return categories and cancellation propagates."""
        started = perf_counter()
        if not request.complete or request.body is None:
            return self._result(request, started, failure="incomplete_state")
        if (
            request.model != self._model
            or request.state_bytes != len(request.body)
            or len(request.body) > MAX_REQUEST_BYTES
            or hashlib.sha256(request.body).hexdigest() != request.request_hash
        ):
            return self._result(request, started, failure="invalid_request")
        if not allow_network:
            return self._result(request, started, failure="network_disabled")
        if not owner:
            return self._result(request, started, failure="invalid_request")
        lease = self._capacity.acquire_nowait(owner)
        if lease is None:
            return self._result(request, started, failure="capacity_exhausted")
        try:
            response, failure = await self._evaluate(request.body, decode)
            if perf_counter() - started > self._timeout_seconds:
                response, failure = None, "timeout"
            return self._result(request, started, failure=failure, response=response)
        finally:
            lease.release()
