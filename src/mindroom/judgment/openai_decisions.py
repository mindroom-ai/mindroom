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
from mindroom.model_defaults import OPENAI_BASE_URL_DEFAULT, OPENAI_DECISIONS_MODEL

if TYPE_CHECKING:
    from mindroom.judgment.answers import TokenUsage

_ENDPOINT = f"{OPENAI_BASE_URL_DEFAULT}/decisions"
# The alias may resolve to a dated snapshot of the same model.
_MODEL_ID = re.compile(rf"{re.escape(OPENAI_DECISIONS_MODEL)}(?:-\d{{4}}-\d{{2}}-\d{{2}})?")


def _question(question: dict[str, Any], guidance: str) -> dict[str, object]:
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
    if guidance:
        instructions += f"\nGuidance: {guidance}"
    return {**wire_question, "name": question["id"], "instructions": instructions}


def _encode(payload: dict[str, Any]) -> dict[str, object]:
    return {
        "model": OPENAI_DECISIONS_MODEL,
        # The endpoint accepts only user messages, so each one names its speaker's role.
        "input": [
            {"role": "user", "content": f"{message['role']}: {message['text']}"}
            for message in payload["state"]["conversation"]
        ],
        "questions": [
            _question(question, payload["guidance"]) for question in payload.get("questions") or [payload["question"]]
        ],
    }


def _answers(
    root: object,
    question_ids: tuple[str, ...],
) -> tuple[str, TokenUsage, tuple[dict[str, object] | None, ...]]:
    """Validate the model and one answer per requested question, in question order; None is an explicit refusal.

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
    if (
        not isinstance(answers, list)
        or len(answers) != len(question_ids)
        or not all(isinstance(answer, dict) for answer in answers)
    ):
        msg = "response must contain one answer per question"
        raise InvalidJudgmentResponseError(msg)
    by_name: dict[str, dict[str, object]] = {}
    for position, named in enumerate(cast("list[dict[str, object]]", answers)):
        # Answers come back in question order, so an unnamed one answers the question at its position.
        name = named.get("name")
        if name is None:
            name = question_ids[position]
        if not isinstance(name, str) or name not in question_ids or name in by_name:
            msg = "response answer names another question"
            raise InvalidJudgmentResponseError(msg)
        by_name[name] = {key: value for key, value in named.items() if key != "name"}
    ordered = (by_name[question_id] for question_id in question_ids)
    return model, usage, tuple(None if answer == {"type": "refusal"} else answer for answer in ordered)


def _predicate_probability(raw: dict[str, object]) -> float:
    answer = exact_keys(raw, {"type", "probability"}, "predicate answer")
    if answer["type"] != "predicate":
        msg = "response answer type is not predicate"
        raise InvalidJudgmentResponseError(msg)
    return probability(answer["probability"], "predicate probability")


def _decode_probabilities(root: object, question_ids: tuple[str, ...]) -> WireAnswer[tuple[float | None, ...]]:
    model, usage, raws = _answers(root, question_ids)
    return WireAnswer(model, usage, tuple(None if raw is None else _predicate_probability(raw) for raw in raws))


def _decode_choice(root: object, question_id: str) -> WireAnswer[WireChoice]:
    model, usage, (raw,) = _answers(root, (question_id,))
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
    decode_probabilities=_decode_probabilities,
    decode_choice=_decode_choice,
)
