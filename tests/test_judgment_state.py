"""Participation requests preserve bounded context and refuse unsafe input."""

from __future__ import annotations

import hashlib
import json

import pytest

from mindroom.judgment.state import (
    MAX_BATCH_QUESTIONS,
    MAX_BATCH_REQUEST_BYTES,
    MAX_REQUEST_BYTES,
    JudgmentMessage,
    JudgmentQuestion,
    build_batch_request,
    build_judgment_request,
    fit_judgment_request,
)
from mindroom.participation import PARTICIPATION_QUESTION


def test_participation_request_has_stable_hashes_and_complete_context() -> None:
    """Identical inputs produce identical wire bytes; conversation changes invalidate them."""
    messages = (JudgmentMessage("user", "How does it work?"), JudgmentMessage("assistant", "Like this."))
    request = build_judgment_request(PARTICIPATION_QUESTION, messages, instructions="Help when useful.")
    assert request == build_judgment_request(PARTICIPATION_QUESTION, messages, instructions="Help when useful.")
    assert request.complete
    assert request.body is not None
    payload = json.loads(request.body)
    assert payload["state"]["conversation"] == [
        {"role": "user", "text": "How does it work?"},
        {"role": "assistant", "text": "Like this."},
    ]
    assert payload["guidance"] == "Help when useful."
    assert request.request_hash == hashlib.sha256(request.body).hexdigest()
    assert request.state_bytes == len(request.body) <= MAX_REQUEST_BYTES
    changed = build_judgment_request(
        PARTICIPATION_QUESTION,
        (JudgmentMessage("user", "New question"),),
        instructions="Help when useful.",
    )
    assert changed.request_hash != request.request_hash


@pytest.mark.parametrize(
    ("messages", "instructions"),
    [
        ((), ""),
        ((JudgmentMessage("assistant", "No user input"),), ""),
        ((JudgmentMessage("user", " "),), ""),
        ((JudgmentMessage("user", "Question"), JudgmentMessage("system", "Private")), ""),
        ((JudgmentMessage("user", "Question"),) * 9, ""),
        ((JudgmentMessage("user", "x" * MAX_REQUEST_BYTES),), ""),
        ((JudgmentMessage("user", "é" * (MAX_REQUEST_BYTES // 2)),), ""),
        ((JudgmentMessage("user", "Question"),), "x" * MAX_REQUEST_BYTES),
        ((JudgmentMessage("user", "token=sk-secret"),), ""),
        ((JudgmentMessage("user", "Question"),), "token=sk-secret"),
        ((JudgmentMessage("user", "\ud800"),), ""),
        ((JudgmentMessage("user", "Question"),), "\ud800"),
    ],
)
def test_incomplete_participation_input_never_produces_wire_bytes(
    messages: tuple[JudgmentMessage, ...],
    instructions: str,
) -> None:
    """Unsupported, oversized, secret-bearing, or unencodable input must fall back locally."""
    request = build_judgment_request(PARTICIPATION_QUESTION, messages, instructions=instructions)
    assert not request.complete
    assert request.body is None
    assert request.incomplete_reason


def test_fit_judgment_request_drops_oldest() -> None:
    """Long threads reach the judge as their newest messages that fit, never as a refusal."""
    history = [JudgmentMessage("user", f"message {index} " + "x" * 1_900) for index in range(20)]
    request = fit_judgment_request(
        PARTICIPATION_QUESTION,
        history,
        JudgmentMessage("user", "Current request"),
        instructions="",
        max_context_messages=63,
    )
    assert request is not None
    assert request.complete
    assert request.body is not None
    conversation = json.loads(request.body)["state"]["conversation"]
    assert conversation[-1] == {"role": "user", "text": "Current request"}
    kept = [message["text"].split(" x")[0].split("x")[0].strip() for message in conversation[:-1]]
    assert 0 < len(kept) < 20
    assert kept == [f"message {index}" for index in range(20 - len(kept), 20)]


def _candidates(count: int, text: str = "Passage about parking.") -> tuple[JudgmentQuestion, ...]:
    return tuple(
        JudgmentQuestion(
            id=f"c{index}",
            instructions=f"Does this passage help answer the message? Passage: {text}",
            when_true="The passage is relevant.",
            when_false="The passage is irrelevant.",
        )
        for index in range(count)
    )


def test_batch_request_shares_one_user_message() -> None:
    """A batch carries every question in order over one shared user message, and no single question."""
    request = build_batch_request(_candidates(3), "Where do I park?", instructions="Judge relevance.")
    assert request == build_batch_request(_candidates(3), "Where do I park?", instructions="Judge relevance.")
    assert request.complete
    assert request.body is not None
    assert request.request_hash == hashlib.sha256(request.body).hexdigest()
    payload = json.loads(request.body)
    assert "question" not in payload
    assert [question["id"] for question in payload["questions"]] == ["c0", "c1", "c2"]
    assert payload["questions"][1]["criteria"] == {
        "true": "The passage is relevant.",
        "false": "The passage is irrelevant.",
    }
    assert payload["guidance"] == "Judge relevance."
    assert payload["state"] == {"conversation": [{"role": "user", "text": "Where do I park?"}]}


def test_batch_over_128000_bytes_is_incomplete() -> None:
    """A batch may exceed the single-question limit, but never the batch limit."""
    assert MAX_BATCH_REQUEST_BYTES == 128_000
    text = "The garage is open late. " * 80
    fits = build_batch_request(_candidates(50, text), "Where do I park?", instructions="")
    assert fits.complete
    assert MAX_REQUEST_BYTES < fits.state_bytes <= MAX_BATCH_REQUEST_BYTES
    request = build_batch_request(_candidates(70, text), "Where do I park?", instructions="")
    assert not request.complete
    assert request.body is None
    assert request.incomplete_reason == "essential_input_too_large"


@pytest.mark.parametrize(
    ("questions", "message", "instructions"),
    [
        (_candidates(2, "token=sk-secret"), "Where do I park?", ""),
        (_candidates(2), "token=sk-secret", ""),
        (_candidates(2), "Where do I park?", "token=sk-secret"),
    ],
)
def test_batch_with_unredacted_secret_is_incomplete(
    questions: tuple[JudgmentQuestion, ...],
    message: str,
    instructions: str,
) -> None:
    """A credential anywhere in a batch keeps the whole batch local."""
    request = build_batch_request(questions, message, instructions=instructions)
    assert request.body is None
    assert request.incomplete_reason == "essential_input_redacted"


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        ((), "Where do I park?"),
        (_candidates(2), " "),
        ((*_candidates(2), _candidates(1)[0]), "Where do I park?"),
        (_candidates(1, "\ud800"), "Where do I park?"),
    ],
)
def test_batch_without_distinct_questions_or_message_is_incomplete(
    questions: tuple[JudgmentQuestion, ...],
    message: str,
) -> None:
    """Empty, duplicate-id, or unencodable batches never produce wire bytes."""
    request = build_batch_request(questions, message, instructions="")
    assert request.body is None
    assert request.incomplete_reason


def test_batch_over_400_questions_is_incomplete() -> None:
    """OpenAI accepts at most 400 questions per request, so a larger batch never produces wire bytes."""
    assert MAX_BATCH_QUESTIONS == 400
    assert build_batch_request(_candidates(400, "x"), "Where do I park?", instructions="").complete
    request = build_batch_request(_candidates(401, "x"), "Where do I park?", instructions="")
    assert request.body is None
    assert request.incomplete_reason == "essential_input_too_large"
