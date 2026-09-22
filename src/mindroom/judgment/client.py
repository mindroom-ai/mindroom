"""Bounded direct HTTP adapter for TypeSafe System One boolean judgments."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, cast

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
from mindroom.judgment.state import MAX_REQUEST_BYTES, JudgmentRequest

if TYPE_CHECKING:
    from collections.abc import Callable

PINNED_MODEL = "jev-1.13.0"

_TYPE_SAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
_MAX_RESPONSE_BYTES = 64 * 1024


class _InvalidJudgmentResponseError(ValueError):
    """The response did not match the requested judgment contract."""


class _ResponseTooLargeError(ValueError):
    """The decoded response crossed the configured byte ceiling."""


class _JudgmentModelDriftError(_InvalidJudgmentResponseError):
    """The provider returned a model other than the configured pin."""


def _reject_constant(_value: str) -> object:
    msg = "response contains a non-finite number"
    raise _InvalidJudgmentResponseError(msg)


def _exact_keys(value: object, expected: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        msg = f"response {label} has unexpected keys"
        raise _InvalidJudgmentResponseError(msg)
    return cast("dict[str, object]", value)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"response {label} must be a number"
        raise _InvalidJudgmentResponseError(msg)
    if not 0 <= value <= 1:
        msg = f"response {label} is outside the allowed range"
        raise _InvalidJudgmentResponseError(msg)
    result = float(value)
    if not math.isfinite(result):
        msg = f"response {label} must be a finite number"
        raise _InvalidJudgmentResponseError(msg)
    return result


def _token_count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = f"response {label} must be a nonnegative integer"
        raise _InvalidJudgmentResponseError(msg)
    return value


def _decode_envelope(body: bytes, *, expected_model: str) -> tuple[dict[str, object], TokenUsage]:
    """Validate JSON, model pin, and usage before reading the probability."""
    if len(body) > _MAX_RESPONSE_BYTES:
        msg = "response exceeds the byte limit"
        raise _InvalidJudgmentResponseError(msg)
    try:
        parsed = json.loads(body, object_pairs_hook=object_with_unique_keys, parse_constant=_reject_constant)
    except _InvalidJudgmentResponseError:
        raise
    except DuplicateJSONKeyError as exc:
        msg = "response contains a duplicate JSON key"
        raise _InvalidJudgmentResponseError(msg) from exc
    except (ValueError, RecursionError) as exc:
        msg = "response is not valid JSON"
        raise _InvalidJudgmentResponseError(msg) from exc

    root = _exact_keys(parsed, {"model", "answers", "usage"}, "body")
    model = root["model"]
    if not isinstance(model, str) or model != expected_model:
        msg = "response model does not match the pinned model"
        raise _JudgmentModelDriftError(msg)

    usage = _exact_keys(root["usage"], {"input_tokens", "output_tokens"}, "usage")
    token_usage = TokenUsage(
        input_tokens=_token_count(usage["input_tokens"], "input_tokens"),
        output_tokens=_token_count(usage["output_tokens"], "output_tokens"),
    )
    return root, token_usage


def _decode_response(
    body: bytes,
    *,
    expected_model: str,
    expected_question: str,
    threshold: float,
) -> JudgmentResponse[bool]:
    """Accept only the requested judgment probability, never a generated decision."""
    root, usage = _decode_envelope(body, expected_model=expected_model)
    answers = _exact_keys(root["answers"], {expected_question}, "question map")
    answer = _exact_keys(answers[expected_question], {"type", "noul"}, "judgment answer")
    if answer["type"] != "noul":
        msg = "response answer type is not noul"
        raise _InvalidJudgmentResponseError(msg)
    probability = _number(answer["noul"], "judgment probability")
    return JudgmentResponse(
        model=expected_model,
        decision=probability >= threshold,
        probability=probability,
        usage=usage,
    )


def _decode_choice_response(
    body: bytes,
    *,
    expected_model: str,
    expected_question: str,
    options: set[str],
    threshold: float,
) -> JudgmentResponse[ChoiceDecision]:
    """Validate the full distribution before accepting a unique, confident winner."""
    root, usage = _decode_envelope(body, expected_model=expected_model)
    answers = _exact_keys(root["answers"], {expected_question}, "question map")
    answer = _exact_keys(answers[expected_question], {"type", "choice", "confidence", "probabilities"}, "choice answer")
    choice = answer["choice"]
    if answer["type"] != "choice" or not isinstance(choice, str) or choice not in options:
        msg = "response choice is not a requested option"
        raise _InvalidJudgmentResponseError(msg)
    confidence = _number(answer["confidence"], "confidence")
    raw = _exact_keys(answer["probabilities"], options, "choice probabilities")
    probabilities = {key: _number(value, "choice probability") for key, value in raw.items()}
    probability = probabilities[choice]
    if not math.isclose(sum(probabilities.values()), 1.0, abs_tol=0.01) or probability != max(probabilities.values()):
        msg = "response choice distribution is inconsistent"
        raise _InvalidJudgmentResponseError(msg)
    unique = sum(value == probability for value in probabilities.values()) == 1
    decision = (
        ChoiceDecision(choice, confidence, tuple(probabilities.items()))
        if unique and min(confidence, probability) >= threshold
        else None
    )
    return JudgmentResponse(model=expected_model, decision=decision, probability=probability, usage=usage)


class SystemOneClient:
    """Call the one fixed TypeSafe endpoint under strict local budgets."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float = 1.5,
        threshold: float = 0.8,
        transport: httpx.AsyncBaseTransport | None = None,
        capacity: JudgmentCapacity = SHARED_CAPACITY,
    ) -> None:
        if not api_key:
            msg = "a TypeSafe API key is required"
            raise ValueError(msg)
        if model != PINNED_MODEL:
            msg = "the judgment client requires the pinned model"
            raise ValueError(msg)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            msg = "the judgment timeout must be finite and positive"
            raise ValueError(msg)
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            msg = "the judgment threshold must be between zero and one"
            raise ValueError(msg)
        self._threshold = threshold
        self._api_key = api_key
        self._model = model
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

    async def _evaluate(self, request: JudgmentRequest) -> JudgmentResponse[bool]:
        return await self._evaluate_with_decoder(request, choice=False, decode=_decode_response)

    async def _evaluate_choice(self, request: JudgmentRequest) -> JudgmentResponse[ChoiceDecision]:
        return await self._evaluate_with_decoder(request, choice=True, decode=_decode_choice_response)

    async def _evaluate_with_decoder[T](
        self,
        request: JudgmentRequest,
        *,
        choice: bool,
        decode: Callable[..., JudgmentResponse[T]],
    ) -> JudgmentResponse[T]:
        assert request.body is not None
        payload = json.loads(request.body)
        question = payload["question"]
        if (question.get("type") == "choice") != choice:
            raise JudgmentError(failure="invalid_request")
        body = json.dumps(
            {
                "model": self._model,
                "state": payload["state"],
                "questions": {
                    question["id"]: {
                        "type": "choice" if choice else "noul",
                        "instructions": {"question": question["instructions"], "guidance": payload["guidance"]},
                        "criteria": question["criteria"],
                    },
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(body) > MAX_REQUEST_BYTES:
            raise JudgmentError(failure="incomplete_state")
        try:
            response_body = await self._post(body)
            return decode(
                response_body,
                expected_model=self._model,
                expected_question=question["id"],
                threshold=self._threshold,
                **({"options": set(question["criteria"])} if choice else {}),
            )
        except httpx.TimeoutException as error:
            raise JudgmentError(failure="timeout") from error
        except _ResponseTooLargeError as error:
            raise JudgmentError(failure="response_too_large") from error
        except httpx.HTTPStatusError as error:
            failure = "rate_limited" if error.response.status_code in {429, 529} else "http_error"
            raise JudgmentError(failure) from error
        except httpx.HTTPError as error:
            raise JudgmentError(failure="transport_error") from error
        except _JudgmentModelDriftError as error:
            raise JudgmentError(failure="model_drift") from error
        except _InvalidJudgmentResponseError as error:
            raise JudgmentError(failure="invalid_response") from error

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
