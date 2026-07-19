"""Tests for the compaction sizing API in mindroom.token_budget."""
# ruff: noqa: D103

from __future__ import annotations

from functools import partial

import pytest
import tiktoken
from agno.models.message import Message

from mindroom.history.compaction import _build_summary_input
from mindroom.token_budget import (
    approximate_o200k_tokens,
    compaction_estimate_kind,
    compaction_payload_token_upper_bound,
)
from tests.history_helpers import (
    _ALL_HISTORY_SETTINGS,
    _completed_run,
)

_BOUND_PAYLOADS = [
    pytest.param("structured: true", id="ascii"),
    pytest.param("汉字漫游天下一路顺风" * 3, id="cjk"),
    pytest.param("🎉🎊🥳🚀🌍", id="emoji"),
    pytest.param("café résumé", id="combining-marks"),
    pytest.param("👩‍💻👨‍👩‍👧‍👦", id="zero-width-joiners"),
    pytest.param("hello 世界 🎉 café — done", id="mixed"),
]


def test_kind_resolves_known_tiktoken_models() -> None:
    assert compaction_estimate_kind("gpt-4o") == "model_tiktoken_tokens"


@pytest.mark.parametrize(
    "model_id",
    ["claude-sonnet-5", "claude-haiku-4-5@20251001", "us.anthropic.claude-sonnet-5-v1:0"],
)
def test_kind_resolves_claude_models_to_the_byte_bound(model_id: str) -> None:
    assert compaction_estimate_kind(model_id) == "utf8_bytes_token_upper_bound"


@pytest.mark.parametrize("model_id", ["gemini-3.5-flash", "local-test-model", None])
def test_kind_resolves_unknown_models_to_the_o200k_surrogate(model_id: str | None) -> None:
    assert compaction_estimate_kind(model_id) == "o200k_base_tokens"


@pytest.mark.parametrize("payload", _BOUND_PAYLOADS)
def test_byte_bound_branch_is_exactly_the_utf8_byte_count(payload: str) -> None:
    """Any divisor heuristic here would undercount dense CJK/emoji payloads."""
    assert compaction_payload_token_upper_bound(payload, model_id="claude-sonnet-5") == len(payload.encode("utf-8"))


@pytest.mark.parametrize("payload", _BOUND_PAYLOADS)
def test_known_model_branch_counts_with_the_model_encoding(payload: str) -> None:
    expected = len(tiktoken.encoding_for_model("gpt-4o").encode(payload, disallowed_special=()))
    assert compaction_payload_token_upper_bound(payload, model_id="gpt-4o") == expected


@pytest.mark.parametrize("payload", _BOUND_PAYLOADS)
def test_approximate_o200k_tokens_matches_the_o200k_encoding(payload: str) -> None:
    expected = len(tiktoken.get_encoding("o200k_base").encode(payload, disallowed_special=()))
    assert approximate_o200k_tokens(payload) == expected


def test_unknown_model_keeps_the_o200k_surrogate_count() -> None:
    payload = "hello 世界 🎉"
    expected = approximate_o200k_tokens(payload)
    assert compaction_payload_token_upper_bound(payload, model_id="gemini-3.5-flash") == expected


@pytest.mark.parametrize("payload_char", ["汉", "🎉"])
def test_chunk_selection_with_the_byte_bound_never_applies_a_prose_ratio(payload_char: str) -> None:
    """Dense non-ASCII payloads must be selected by byte count, not a prose token ratio."""
    estimator = partial(compaction_payload_token_upper_bound, model_id="claude-sonnet-5")
    runs = [
        _completed_run(f"run-{index}", messages=[Message(role="user", content=payload_char * 2_000)])
        for index in range(12)
    ]
    full_input, full_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=runs,
        max_input_tokens=1_000_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=estimator,
    )
    assert len(full_runs) == len(runs)

    tight_budget = estimator(full_input) - 50
    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=runs,
        max_input_tokens=tight_budget,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=estimator,
    )
    assert 0 < len(included_runs) < len(runs)
    assert len(summary_input.encode("utf-8")) <= tight_budget
