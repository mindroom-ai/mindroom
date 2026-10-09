"""TypeSafe System One wire format for probability judgments."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from mindroom.judgment.client import (
    InvalidJudgmentResponseError,
    JudgmentModelDriftError,
    JudgmentWire,
    WireAnswer,
    WireChoice,
    exact_keys,
    probability,
    token_usage,
)

if TYPE_CHECKING:
    from mindroom.judgment.answers import TokenUsage

_PINNED_MODEL = "jev-1.13.0"

_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def _encode(payload: dict[str, Any]) -> dict[str, object]:
    question = payload["question"]
    return {
        "model": _PINNED_MODEL,
        "state": payload["state"],
        "questions": {
            question["id"]: {
                "type": "choice" if question.get("type") == "choice" else "noul",
                "instructions": {"question": question["instructions"], "guidance": payload["guidance"]},
                "criteria": question["criteria"],
            },
        },
    }


def _answer(root: object, question_id: str, keys: set[str], answer_type: str) -> tuple[TokenUsage, dict[str, object]]:
    """Validate the exact envelope, model pin, and answer keys before reading values."""
    body = exact_keys(root, {"model", "answers", "usage"}, "body")
    if body["model"] != _PINNED_MODEL:
        msg = "response model does not match the pinned model"
        raise JudgmentModelDriftError(msg)
    usage = token_usage(exact_keys(body["usage"], {"input_tokens", "output_tokens"}, "usage"))
    answers = exact_keys(body["answers"], {question_id}, "question map")
    answer = exact_keys(answers[question_id], keys, "judgment answer")
    if answer["type"] != answer_type:
        msg = f"response answer type is not {answer_type}"
        raise InvalidJudgmentResponseError(msg)
    return usage, answer


def _decode_probability(root: object, question_id: str) -> WireAnswer[float]:
    usage, answer = _answer(root, question_id, {"type", "noul"}, "noul")
    return WireAnswer(_PINNED_MODEL, usage, probability(answer["noul"], "judgment probability"))


def _decode_choice(root: object, question_id: str) -> WireAnswer[WireChoice]:
    usage, answer = _answer(root, question_id, {"type", "choice", "confidence", "probabilities"}, "choice")
    choice, raw = answer["choice"], answer["probabilities"]
    if not isinstance(choice, str) or not isinstance(raw, dict):
        msg = "response choice is malformed"
        raise InvalidJudgmentResponseError(msg)
    probabilities = {
        key: probability(value, "choice probability") for key, value in cast("dict[str, object]", raw).items()
    }
    return WireAnswer(
        _PINNED_MODEL,
        usage,
        WireChoice(choice, probability(answer["confidence"], "confidence"), probabilities),
    )


SYSTEM_ONE = JudgmentWire(
    endpoint=_ENDPOINT,
    encode=_encode,
    decode_probability=_decode_probability,
    decode_choice=_decode_choice,
)
