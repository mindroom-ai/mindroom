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
    return {
        "model": _PINNED_MODEL,
        "state": payload["state"],
        "questions": {
            question["id"]: {
                "type": "choice" if question.get("type") == "choice" else "noul",
                "instructions": {"question": question["instructions"], "guidance": payload["guidance"]},
                "criteria": question["criteria"],
            }
            for question in payload.get("questions") or [payload["question"]]
        },
    }


def _answers(
    root: object,
    question_ids: tuple[str, ...],
    keys: set[str],
    answer_type: str,
) -> tuple[TokenUsage, tuple[dict[str, object], ...]]:
    """Validate the exact envelope, model pin, and answer keys before reading values, in question order."""
    body = exact_keys(root, {"model", "answers", "usage"}, "body")
    if body["model"] != _PINNED_MODEL:
        msg = "response model does not match the pinned model"
        raise JudgmentModelDriftError(msg)
    usage = token_usage(exact_keys(body["usage"], {"input_tokens", "output_tokens"}, "usage"))
    answers = exact_keys(body["answers"], set(question_ids), "question map")
    ordered = tuple(exact_keys(answers[question_id], keys, "judgment answer") for question_id in question_ids)
    if any(answer["type"] != answer_type for answer in ordered):
        msg = f"response answer type is not {answer_type}"
        raise InvalidJudgmentResponseError(msg)
    return usage, ordered


def _decode_probabilities(root: object, question_ids: tuple[str, ...]) -> WireAnswer[tuple[float | None, ...]]:
    usage, answers = _answers(root, question_ids, {"type", "noul"}, "noul")
    return WireAnswer(
        _PINNED_MODEL,
        usage,
        tuple(probability(answer["noul"], "judgment probability") for answer in answers),
    )


def _decode_choice(root: object, question_id: str) -> WireAnswer[WireChoice]:
    usage, (answer,) = _answers(
        root,
        (question_id,),
        {"type", "choice", "confidence", "probabilities"},
        "choice",
    )
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
    decode_probabilities=_decode_probabilities,
    decode_choice=_decode_choice,
)
