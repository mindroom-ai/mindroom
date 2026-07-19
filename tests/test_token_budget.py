"""Tests for the compaction sizing API in mindroom.token_budget."""
# ruff: noqa: D103

from __future__ import annotations

import unicodedata
from functools import partial

import httpx
import pytest
import tiktoken
from agno.models.message import Message
from agno.models.openai.chat import OpenAIChat
from agno.models.openai.like import OpenAILike
from agno.models.openai.open_responses import OpenResponses
from agno.models.openai.responses import OpenAIResponses
from openai import AsyncOpenAI, OpenAI

from mindroom.history.compaction import _build_summary_input
from mindroom.model_instance_checks import is_genuine_openai_endpoint
from mindroom.token_budget import (
    approximate_o200k_tokens,
    compaction_estimate_kind,
    compaction_payload_token_upper_bound,
)
from tests.conftest import FakeModel
from tests.history_helpers import (
    _ALL_HISTORY_SETTINGS,
    _completed_run,
)

_BOUND_PAYLOADS = [
    pytest.param("structured: true", id="ascii"),
    pytest.param("汉字漫游天下一路顺风" * 3, id="cjk"),
    pytest.param("🎉🎊🥳🚀🌍", id="emoji"),
    # Decomposed on purpose: base letters followed by U+0301 combining acute.
    pytest.param("cafe\u0301 re\u0301sume\u0301", id="combining-marks"),
    pytest.param("👩‍💻👨‍👩‍👧‍👦", id="zero-width-joiners"),
    pytest.param("hello 世界 🎉 café — done", id="mixed"),
]

_SURROGATE_PAYLOADS = [
    pytest.param("\ud800", id="lone-high-surrogate"),
    pytest.param("\udc00", id="lone-low-surrogate"),
    pytest.param("🎉\ud800 tail", id="valid-pair-adjacent-to-lone-surrogate"),
]


def test_kind_resolves_known_tiktoken_models_on_the_genuine_endpoint() -> None:
    assert compaction_estimate_kind("gpt-4o", genuine_openai_endpoint=True) == "model_tiktoken_tokens"


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-sonnet-5",
        "claude-haiku-4-5@20251001",
        "us.anthropic.claude-sonnet-5-v1:0",
        "gemini-3.5-flash",
        "local-test-model",
        None,
    ],
)
def test_kind_resolves_models_without_a_local_tokenizer_to_the_byte_bound(model_id: str | None) -> None:
    assert compaction_estimate_kind(model_id, genuine_openai_endpoint=False) == "utf8_bytes_token_upper_bound"


@pytest.mark.parametrize("model_id", ["gpt-4o", "gpt-4o-local", "gpt-4o-mini:latest"])
def test_kind_sends_custom_endpoint_openai_aliases_to_the_byte_bound(model_id: str) -> None:
    """A tiktoken-recognized id served by a custom endpoint must not get o200k counting.

    Custom OpenAI-compatible endpoints (llama.cpp, LM Studio, proxies) can serve
    arbitrary models under OpenAI-style ids, and tiktoken prefix-matching would
    silently undercount their real tokenizers.
    """
    assert compaction_estimate_kind(model_id, genuine_openai_endpoint=False) == "utf8_bytes_token_upper_bound"
    payload = "hello 世界 🎉"
    expected = len(payload.encode("utf-8"))
    assert compaction_payload_token_upper_bound(payload, model_id=model_id, genuine_openai_endpoint=False) == expected


def test_kind_pins_gpt_5_6_to_the_byte_bound() -> None:
    """Pinned decision (ISSUE-246 fix round 1): gpt-5.6 stays on the byte bound.

    Provider-aware guessing would reintroduce undercount risk, so any model the
    installed tiktoken does not recognize — including gpt-5.6 today — is sized
    by the byte bound even on the genuine OpenAI endpoint. When a tiktoken
    upgrade learns gpt-5.6, this test should be consciously UPDATED to expect
    "model_tiktoken_tokens".
    """
    assert compaction_estimate_kind("gpt-5.6", genuine_openai_endpoint=True) == "utf8_bytes_token_upper_bound"


@pytest.mark.parametrize("model_id", ["claude-sonnet-5", "gemini-3.5-flash", None])
@pytest.mark.parametrize("payload", _BOUND_PAYLOADS)
def test_byte_bound_branch_is_exactly_the_utf8_byte_count(payload: str, model_id: str | None) -> None:
    """Any divisor heuristic here would undercount dense CJK/emoji payloads."""
    bound = compaction_payload_token_upper_bound(payload, model_id=model_id, genuine_openai_endpoint=False)
    assert bound == len(payload.encode("utf-8"))


def test_byte_bound_documents_the_nfkc_normalization_gap() -> None:
    """Known theoretical undercount class, accepted in ISSUE-246 fix round 2.

    U+FDFA is 3 UTF-8 bytes but NFKC-normalizes to 18 code points, so a
    tokenizer that normalizes text before segmentation can emit more tokens
    than the payload has bytes. The byte bound is proven only for byte-level
    BPE tokenizers (every token consumes at least one byte), which covers the
    providers routed through this path today. This test pins the current
    behavior so the gap stays discoverable instead of silent.
    """
    payload = "\ufdfa"
    assert len(payload.encode("utf-8")) == 3
    assert len(unicodedata.normalize("NFKC", payload)) == 18
    assert compaction_payload_token_upper_bound(payload, model_id="unknown-model", genuine_openai_endpoint=False) == 3


@pytest.mark.parametrize("payload", _BOUND_PAYLOADS)
def test_known_model_branch_counts_with_the_model_encoding(payload: str) -> None:
    expected = len(tiktoken.encoding_for_model("gpt-4o").encode(payload, disallowed_special=()))
    assert compaction_payload_token_upper_bound(payload, model_id="gpt-4o", genuine_openai_endpoint=True) == expected


@pytest.mark.parametrize("payload", _BOUND_PAYLOADS)
def test_approximate_o200k_tokens_matches_the_o200k_encoding(payload: str) -> None:
    expected = len(tiktoken.get_encoding("o200k_base").encode(payload, disallowed_special=()))
    assert approximate_o200k_tokens(payload) == expected


@pytest.mark.parametrize("model_id", ["claude-sonnet-5", "gemini-3.5-flash", None])
@pytest.mark.parametrize("payload", _SURROGATE_PAYLOADS)
def test_byte_bound_tolerates_unpaired_surrogates(payload: str, model_id: str | None) -> None:
    """JSON-decoded provider payloads can carry unpaired surrogates; sizing must not raise."""
    expected = len(payload.encode("utf-8", errors="surrogatepass"))
    assert compaction_payload_token_upper_bound(payload, model_id=model_id, genuine_openai_endpoint=False) == expected


@pytest.mark.parametrize("payload", _SURROGATE_PAYLOADS)
def test_chunk_selection_tolerates_unpaired_surrogates(payload: str) -> None:
    estimator = partial(
        compaction_payload_token_upper_bound,
        model_id="claude-sonnet-5",
        genuine_openai_endpoint=False,
    )
    runs = [_completed_run("run-0", messages=[Message(role="user", content=payload * 100)])]
    summary_input, included_runs = _build_summary_input(
        previous_summary=None,
        compacted_runs=runs,
        max_input_tokens=10_000,
        history_settings=_ALL_HISTORY_SETTINGS,
        token_estimator=estimator,
    )
    assert included_runs == runs
    assert estimator(summary_input) <= 10_000


@pytest.mark.parametrize("payload_char", ["汉", "🎉"])
def test_chunk_selection_with_the_byte_bound_never_applies_a_prose_ratio(payload_char: str) -> None:
    """Dense non-ASCII payloads must be selected by byte count, not a prose token ratio."""
    estimator = partial(
        compaction_payload_token_upper_bound,
        model_id="claude-sonnet-5",
        genuine_openai_endpoint=False,
    )
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


def test_genuine_openai_endpoint_accepts_clean_openai_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert is_genuine_openai_endpoint(OpenAIChat(id="gpt-4o")) is True
    assert is_genuine_openai_endpoint(OpenAIResponses(id="gpt-4o")) is True


def test_genuine_openai_endpoint_rejects_custom_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert is_genuine_openai_endpoint(OpenAIChat(id="gpt-4o", base_url="http://localhost:9292/v1")) is False


def test_genuine_openai_endpoint_rejects_env_base_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:9292/v1")
    assert is_genuine_openai_endpoint(OpenAIChat(id="gpt-4o")) is False


def test_genuine_openai_endpoint_rejects_openai_like_and_foreign_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert is_genuine_openai_endpoint(OpenAILike(id="gpt-4o")) is False
    assert is_genuine_openai_endpoint(OpenResponses(id="gpt-4o")) is False
    assert is_genuine_openai_endpoint(FakeModel(id="gpt-4o", provider="fake")) is False


def test_genuine_openai_endpoint_rejects_client_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agno merges client_params over the base client kwargs, so they can redirect the endpoint."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    custom = {"base_url": "http://localhost:9292/v1"}
    assert is_genuine_openai_endpoint(OpenAIChat(id="gpt-4o", client_params=custom)) is False
    assert is_genuine_openai_endpoint(OpenAIResponses(id="gpt-4o", client_params=custom)) is False
    # Fail-closed pin (ISSUE-246 fix round 3): ANY client_params content is
    # distrusted, not just base_url — timeouts-only users get the byte bound.
    assert is_genuine_openai_endpoint(OpenAIChat(id="gpt-4o", client_params={"timeout": 5})) is False


def test_genuine_openai_endpoint_rejects_prebuilt_and_custom_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    prebuilt = OpenAI(api_key="test-key", base_url="http://localhost:9292/v1")
    assert is_genuine_openai_endpoint(OpenAIChat(id="gpt-4o", client=prebuilt)) is False
    prebuilt_async = AsyncOpenAI(api_key="test-key", base_url="http://localhost:9292/v1")
    assert is_genuine_openai_endpoint(OpenAIChat(id="gpt-4o", async_client=prebuilt_async)) is False
    with httpx.Client() as http_client:
        assert is_genuine_openai_endpoint(OpenAIChat(id="gpt-4o", http_client=http_client)) is False
