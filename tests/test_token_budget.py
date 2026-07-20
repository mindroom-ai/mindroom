"""Tests for compaction sizing metadata and unchanged estimator behavior."""
# ruff: noqa: D103

from __future__ import annotations

import pytest
import tiktoken

from mindroom.token_budget import (
    approximate_o200k_tokens,
    compaction_estimate_kind,
    estimate_compaction_input_tokens,
)

_PAYLOADS = [
    pytest.param("structured: true", id="ascii"),
    pytest.param("汉字漫游天下一路顺风" * 3, id="cjk"),
    pytest.param("🎉🎊🥳🚀🌍", id="emoji"),
    pytest.param("cafe\u0301 re\u0301sume\u0301", id="combining-marks"),
    pytest.param("👩‍💻👨‍👩‍👧‍👦", id="zero-width-joiners"),
]


def test_known_model_kind_wins_over_conservative_fallback() -> None:
    assert compaction_estimate_kind("gpt-4o", conservative_fallback=True) == "model_tiktoken_tokens"


def test_unknown_model_kinds_preserve_existing_selection() -> None:
    assert compaction_estimate_kind("claude-sonnet-5", conservative_fallback=True) == "utf8_bytes_token_upper_bound"
    assert compaction_estimate_kind("local-model") == "o200k_base_tokens"


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_byte_upper_bound_is_utf8_size(payload: str) -> None:
    assert estimate_compaction_input_tokens(
        payload,
        model_id="claude-sonnet-5",
        conservative_fallback=True,
    ) == len(payload.encode("utf-8"))


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_known_model_uses_model_encoding(payload: str) -> None:
    expected = len(tiktoken.encoding_for_model("gpt-4o").encode(payload, disallowed_special=()))
    assert estimate_compaction_input_tokens(payload, model_id="gpt-4o") == expected


@pytest.mark.parametrize("payload", _PAYLOADS)
def test_unknown_model_keeps_o200k_estimate(payload: str) -> None:
    expected = len(tiktoken.get_encoding("o200k_base").encode(payload, disallowed_special=()))
    assert approximate_o200k_tokens(payload) == expected
    assert estimate_compaction_input_tokens(payload, model_id="local-model") == expected


def test_byte_upper_bound_accepts_unpaired_surrogates() -> None:
    for payload in ("\ud800", "\udc00", "🎉\ud800 tail"):
        assert estimate_compaction_input_tokens(
            payload,
            model_id="claude-sonnet-5",
            conservative_fallback=True,
        ) == len(payload.encode("utf-8", errors="surrogatepass"))
