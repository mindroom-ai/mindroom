"""OpenAI Decisions API wire format for probability judgments."""

from __future__ import annotations

import re
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
from mindroom.model_defaults import OPENAI_DECISIONS_MODEL

if TYPE_CHECKING:
    from mindroom.judgment.answers import TokenUsage

_ENDPOINT = "https://api.openai.com/v1/decisions"
# The alias may resolve to a dated snapshot of the same model.
_MODEL_ID = re.compile(rf"{re.escape(OPENAI_DECISIONS_MODEL)}(?:-\d{{4}}-\d{{2}}-\d{{2}})?")


def _encode(payload: dict[str, Any]) -> dict[str, object]:
    question = payload["question"]
    criteria = question["criteria"]
    instructions = question["instructions"]
    if question.get("type") == "choice":
        wire_question: dict[str, object] = {
            "type": "choice",
            "choices": [{"value": value, "description": description} for value, description in criteria.items()],
        }
    else:
        # Predicates take no criteria field, so the rubric joins the instructions.
        wire_question = {"type": "predicate"}
        instructions += f"\nTrue when: {criteria['true']}\nFalse when: {criteria['false']}"
    if payload["guidance"]:
        instructions += f"\nGuidance: {payload['guidance']}"
    return {
        "model": OPENAI_DECISIONS_MODEL,
        # The endpoint accepts only user messages, so each one names its speaker's role.
        "input": [
            {"role": "user", "content": f"{message['role']}: {message['text']}"}
            for message in payload["state"]["conversation"]
        ],
        "questions": [{**wire_question, "name": question["id"], "instructions": instructions}],
    }


def _answer(root: object, question_id: str) -> tuple[str, TokenUsage, dict[str, object] | None]:
    """Validate the model and the one requested answer; None is an explicit refusal.

    Unknown envelope and usage metadata is ignored, unlike unknown answer fields.
    """
    if not isinstance(root, dict) or not {"answers", "model", "usage"} <= root.keys():
        msg = "response body is missing required keys"
        raise InvalidJudgmentResponseError(msg)
    body = cast("dict[str, object]", root)
    model = body["model"]
    if not isinstance(model, str) or _MODEL_ID.fullmatch(model) is None:
        msg = "response model does not match the pinned model"
        raise JudgmentModelDriftError(msg)
    if not isinstance(body["usage"], dict):
        msg = "response usage must be an object"
        raise InvalidJudgmentResponseError(msg)
    usage = token_usage(cast("dict[str, object]", body["usage"]))
    answers = body["answers"]
    if not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict):
        msg = "response must contain exactly one answer"
        raise InvalidJudgmentResponseError(msg)
    named = cast("dict[str, object]", answers[0])
    if named.get("name") not in (None, question_id):
        msg = "response answer names another question"
        raise InvalidJudgmentResponseError(msg)
    answer = {key: value for key, value in named.items() if key != "name"}
    return model, usage, None if answer == {"type": "refusal"} else answer


def _decode_probability(root: object, question_id: str) -> WireAnswer[float]:
    model, usage, raw = _answer(root, question_id)
    if raw is None:
        return WireAnswer(model, usage, None)
    answer = exact_keys(raw, {"type", "probability"}, "predicate answer")
    if answer["type"] != "predicate":
        msg = "response answer type is not predicate"
        raise InvalidJudgmentResponseError(msg)
    return WireAnswer(model, usage, probability(answer["probability"], "predicate probability"))


def _decode_choice(root: object, question_id: str) -> WireAnswer[WireChoice]:
    model, usage, raw = _answer(root, question_id)
    if raw is None:
        return WireAnswer(model, usage, None)
    answer = exact_keys(raw, {"type", "choice", "confidence", "probabilities"}, "choice answer")
    choice, items = answer["choice"], answer["probabilities"]
    if answer["type"] != "choice" or not isinstance(choice, str) or not isinstance(items, list):
        msg = "response choice is malformed"
        raise InvalidJudgmentResponseError(msg)
    probabilities: dict[str, float] = {}
    for item in items:
        entry = exact_keys(item, {"value", "probability"}, "choice probability")
        value = entry["value"]
        if not isinstance(value, str) or value in probabilities:
            msg = "response choice probabilities are malformed"
            raise InvalidJudgmentResponseError(msg)
        probabilities[value] = probability(entry["probability"], "choice probability")
    return WireAnswer(model, usage, WireChoice(choice, probability(answer["confidence"], "confidence"), probabilities))


OPENAI_DECISIONS = JudgmentWire(
    endpoint=_ENDPOINT,
    encode=_encode,
    decode_probability=_decode_probability,
    decode_choice=_decode_choice,
)
