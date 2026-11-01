"""Tests for token counting through a route's counter: kinds, cached exact counts, calibration, and fallbacks."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import math
import threading
import time
from typing import TYPE_CHECKING

import tiktoken
from structlog.testing import capture_logs

from mindroom.llm.provider import ModelIdentity, TokenCounter
from mindroom.token_counting import counts, families
from mindroom.token_counting.counts import TokenCount, count, count_exact, fit_text, prefetch, text_tokens
from mindroom.token_counting.families import counter_for_model, load_o200k
from tests.fakes.token_counter import fake_token_counter, quarter_tokens

if TYPE_CHECKING:
    import pytest


def _o200k(text: str) -> int:
    return len(load_o200k().encode(text, disallowed_special=()))


class _Endpoint:
    """A counting endpoint that counts one token per character and records each text it counted."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, text: str) -> int:
        self.texts.append(text)
        return len(text)


def test_openai_ids_count_exactly_only_on_openai_routes() -> None:
    load_o200k()
    for model_id in ("gpt-4o", "openai/gpt-4.1", "gpt-oss-120b"):
        counter = counter_for_model(ModelIdentity(id=model_id, provider="openai"), local_encoder=True)
        assert counter.exact_locally
        assert counter.key == "tiktoken:o200k_base"
        assert count(counter, "hello world") == TokenCount(_o200k("hello world"), "exact")
    endpoint = _Endpoint()
    alias = counter_for_model(
        ModelIdentity(id="gpt-4o", provider="llama_cpp", endpoint="http://127.0.0.1:8080/v1"),
        count=endpoint,
    )
    assert not alias.exact_locally
    assert alias.count is endpoint
    assert alias.key == "llama_cpp:http://127.0.0.1:8080/v1:gpt-4o"


def test_unmapped_ids_estimate_with_their_family_margin() -> None:
    load_o200k()
    text = "Ein Satz mit Zahlen 2026-10-31 und `code()`.\n" * 20
    claude = counter_for_model(ModelIdentity(id="claude-opus-5-5", provider="anthropic"))
    assert claude.estimate(text) == math.ceil(_o200k(text) * 1.8)
    bedrock = counter_for_model(ModelIdentity(id="anthropic.claude-haiku-4-5-v1:0", provider="bedrock"))
    assert bedrock.estimate(text) == math.ceil(_o200k(text) * 1.8)
    unmapped_openai = counter_for_model(ModelIdentity(id="gpt-6-astra", provider="openai"), local_encoder=True)
    assert not unmapped_openai.exact_locally
    assert unmapped_openai.estimate(text) == _o200k(text)
    other = counter_for_model(ModelIdentity(id="qwen3.8:27b", provider="ollama"))
    assert other.estimate(text) == math.ceil(_o200k(text) * 1.25)
    assert other.key == "ollama::qwen3.8:27b"


def test_estimates_wait_for_the_encoder_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    real_get_encoding = tiktoken.get_encoding

    def slow_get_encoding(name: str) -> tiktoken.Encoding:
        release.wait()
        return real_get_encoding(name)

    monkeypatch.setattr(families, "_o200k_encoder", families._Encoder())
    monkeypatch.setattr(tiktoken, "get_encoding", slow_get_encoding)
    endpoint = _Endpoint()
    counter = counter_for_model(ModelIdentity(id="claude-opus-5-5", provider="anthropic"), count=endpoint)
    text = "naïve text"

    assert count(counter, text) == TokenCount(-(-len(text) // 4), "estimated")
    asyncio.run(count_exact(counter, "other text"))
    assert count(counter, text).kind == "estimated", "no calibration sample against the stand-in"

    release.set()
    assert load_o200k() is not None
    assert count(counter, text) == TokenCount(math.ceil(_o200k(text) * 1.8), "estimated")


def test_count_kinds() -> None:
    endpoint = _Endpoint()
    counter = fake_token_counter(count=endpoint)
    assert count(counter, "") == TokenCount(0, "exact")
    assert count(counter, "a" * 40) == TokenCount(10, "estimated")

    assert asyncio.run(count_exact(counter, "b" * 40)) == TokenCount(40, "exact")
    # The endpoint counted four times the estimate, so later estimates of this counter scale by four.
    assert count(counter, "a" * 40) == TokenCount(40, "calibrated")
    assert count(counter, "b" * 40) == TokenCount(40, "exact")
    assert count(fake_token_counter(key="other"), "a" * 40) == TokenCount(10, "estimated")


def test_cached_count_makes_no_endpoint_call() -> None:
    endpoint = _Endpoint()
    counter = fake_token_counter(count=endpoint)

    async def twice() -> None:
        await count_exact(counter, "same text")
        await count_exact(counter, "same text")

    asyncio.run(twice())
    assert endpoint.texts == ["same text"]
    assert count(counter, "same text") == TokenCount(9, "exact")
    assert endpoint.texts == ["same text"]


def test_without_its_vocabulary_a_size_that_must_not_undercount_counts_bytes() -> None:
    """Compaction input still has an upper bound when o200k_base cannot load, while history keeps the quarter token.

    A quarter token per character counts Japanese text at a quarter of its real size, so a summary request sized by it
    would fail every time.
    """
    text = "今日は会議の議事録をまとめました。" * 20
    for counter in (
        TokenCounter(key="no-vocabulary", estimate=lambda _text: None),
        TokenCounter(key="no-vocabulary-own", estimate=lambda _text: None, exact_locally=True),
    ):
        assert count(counter, text) == TokenCount(-(-len(text) // 4), "estimated")
        assert count(counter, text, calibrated=False) == TokenCount(len(text.encode()), "estimated")


def test_without_its_vocabulary_no_text_counts_as_free() -> None:
    """A short line costs at least one token, so a preload cap still binds on a file of short lines."""
    counter = TokenCounter(key="no-vocabulary", estimate=lambda _text: None)
    assert count(counter, "ab\n") == TokenCount(1, "estimated")


def test_calibration_weighs_the_newest_provider_counts_most() -> None:
    """The route's content drifts, so each earlier provider count weighs less than the one after it."""

    async def endpoint(text: str) -> int:
        return len(text) * (2 if text.startswith("a") else 1)

    counter = TokenCounter(key="drifting", estimate=len, count=endpoint)
    asyncio.run(count_exact(counter, "a" * 100))
    asyncio.run(count_exact(counter, "b" * 100))

    # The first count weighs 0.9 of the second: (200 * 0.9 + 100) / (100 * 0.9 + 100) tokens per estimated token.
    assert count(counter, "c" * 1_000) == TokenCount(math.ceil(1_000 * 280 / 190), "calibrated")


def test_the_exact_count_cache_keeps_the_newest_counts_within_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(counts, "_MAX_CACHED", 3)
    counter = fake_token_counter(count=_Endpoint())
    texts = [f"text {index}" for index in range(4)]
    for text in texts:
        asyncio.run(count_exact(counter, text))

    assert counts.cached_count(counter, texts[0]) is None
    assert [counts.cached_count(counter, text) for text in texts[1:]] == [6, 6, 6]


def test_blank_text_never_reaches_the_endpoint() -> None:
    endpoint = _Endpoint()
    counter = fake_token_counter(count=endpoint)
    assert asyncio.run(count_exact(counter, "    ")) == TokenCount(1, "estimated")
    assert asyncio.run(count_exact(counter, "")) == TokenCount(0, "exact")
    assert endpoint.texts == []


def test_text_tokens_removes_the_message_framing() -> None:
    async def framed(text: str) -> int:
        return len(text) + 9

    assert asyncio.run(text_tokens(framed, "abcd")) == 4


def test_failed_count_falls_back_logs_kind_and_cools_down_only_its_key() -> None:
    calls: list[str] = []

    async def failing(text: str) -> int:
        calls.append(text)
        msg = "endpoint down"
        raise RuntimeError(msg)

    broken = fake_token_counter(key="broken", count=failing)
    healthy_endpoint = _Endpoint()
    healthy = fake_token_counter(key="healthy", count=healthy_endpoint)

    with capture_logs() as logs:
        assert asyncio.run(count_exact(broken, "x" * 8)) == TokenCount(2, "estimated")
    assert [(log["event"], log["key"], log["fallback_kind"]) for log in logs] == [
        ("Token count failed", "broken", "estimated"),
    ]
    assert asyncio.run(count_exact(broken, "y" * 8)) == TokenCount(2, "estimated")
    assert calls == ["x" * 8], "a cooling key sends no further counts"
    assert asyncio.run(count_exact(healthy, "y" * 8)) == TokenCount(8, "exact")


def test_a_route_whose_counting_keeps_failing_warns_once_until_it_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy without a counting endpoint fails after every pause; only the first failure of a streak warns."""
    monkeypatch.setattr(counts, "_COOLDOWN_SECONDS", 0.0)
    down = [True]

    async def endpoint(text: str) -> int:
        if down[0]:
            msg = "no count_tokens on this proxy"
            raise RuntimeError(msg)
        return len(text)

    counter = fake_token_counter(key="proxy", count=endpoint)
    with capture_logs() as logs:
        asyncio.run(count_exact(counter, "a" * 8))
        asyncio.run(count_exact(counter, "b" * 8))
        down[0] = False
        asyncio.run(count_exact(counter, "c" * 8))
        down[0] = True
        asyncio.run(count_exact(counter, "d" * 8))

    assert [(log["event"], log["log_level"]) for log in logs] == [
        ("Token count failed", "warning"),
        ("Token count failed", "debug"),
        ("Token count failed", "warning"),
    ]


def test_slow_count_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hanging(text: str) -> int:
        await asyncio.Event().wait()
        return len(text)

    monkeypatch.setattr(counts, "_TIMEOUT_SECONDS", 0.01)
    counter = fake_token_counter(count=hanging)
    with capture_logs() as logs:
        assert asyncio.run(count_exact(counter, "x" * 8)) == TokenCount(2, "estimated")
    assert [log["event"] for log in logs] == ["Token count failed"]


def test_prefetch_dedupes_caps_and_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(counts, "_MAX_PENDING", 8)
    gate = asyncio.Event()
    counted: list[str] = []

    async def gated(text: str) -> int:
        await gate.wait()
        counted.append(text)
        return len(text)

    counter = fake_token_counter(count=gated)

    async def scenario() -> None:
        prefetch(counter, "same", "same", *(f"text {index}" for index in range(20)))
        pending = list(counts._state.pending.values())
        assert len(pending) == 8
        gate.set()
        await asyncio.gather(*pending)
        assert counts._state.pending == {}
        assert sorted(counted) == sorted(["same", *(f"text {index}" for index in range(7))])
        prefetch(counter, "same")
        assert counts._state.pending == {}, "a cached text is not counted again"

        gate.clear()
        prefetch(counter, "cancelled")
        (task,) = counts._state.pending.values()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert counts._state.pending == {}

    asyncio.run(scenario())


def test_prefetch_skips_counters_without_an_endpoint_or_with_a_local_tokenizer() -> None:
    async def scenario() -> None:
        prefetch(fake_token_counter(), "text")
        load_o200k()
        prefetch(counter_for_model(ModelIdentity(id="gpt-4o", provider="openai"), local_encoder=True), "text")
        assert counts._state.pending == {}

    asyncio.run(scenario())


def test_fit_text_always_fits() -> None:
    text = "".join(f"word{index} " for index in range(400))
    notice = "\n[cut]"
    for budget in range(1, 60):
        fitted = fit_text(text, budget, quarter_tokens, notice=notice)
        assert quarter_tokens(fitted) <= budget
        assert fitted.endswith(notice)
        assert text.startswith(fitted.removesuffix(notice))
    assert fit_text("short", 10, quarter_tokens, notice=notice) == "short"
    assert fit_text(text, 0, quarter_tokens, notice=notice) == ""


def test_long_runs_without_whitespace_count_in_linear_time() -> None:
    """Tiktoken's cost grows with the square of a run without whitespace, so the encoder counts in pieces."""
    load_o200k()
    counter = counter_for_model(ModelIdentity(id="gpt-4o", provider="openai"), local_encoder=True)
    started = time.perf_counter()
    count(counter, "x" * 200_000)
    # About 0.1 s in pieces on an idle host, and 18 s in one piece.
    assert time.perf_counter() - started < 5.0
    prose = "The agent reads the file, then answers the question in a few short sentences.\n" * 2_000
    assert count(counter, prose).tokens == _o200k(prose)


def test_background_counts_encode_off_the_event_loop() -> None:
    """A background count encodes in a worker thread, so a long text never stalls the replies sharing the loop."""
    encoded_on: list[int] = []

    def estimate(text: str) -> int:
        encoded_on.append(threading.get_ident())
        return len(text) // 4

    async def endpoint(text: str) -> int:
        return len(text)

    counter = TokenCounter(key="threads", estimate=estimate, count=endpoint)

    async def scenario() -> int:
        await count_exact(counter, "some text to count")
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    assert encoded_on
    assert loop_thread not in encoded_on


def test_a_frozen_counter_keeps_the_calibration_it_started_with() -> None:
    """Comparing sizes measured at two times needs one basis, whatever the provider counted in between."""

    async def endpoint(text: str) -> int:
        return len(text)

    counter = fake_token_counter(count=endpoint)
    asyncio.run(count_exact(counter, "a" * 400))
    frozen = counts.frozen(counter)
    before = count(frozen, "b" * 400).tokens

    async def halved(text: str) -> int:
        return len(text) // 8

    for index in range(40):
        asyncio.run(count_exact(fake_token_counter(count=halved), f"{index} " * 200))
    assert count(counter, "b" * 400).tokens < 400, "the live calibration moved"
    assert count(frozen, "b" * 400).tokens == before == 400


def test_pieces_count_exactly_unless_a_run_has_no_newline_or_word_boundary() -> None:
    """Pieces cut after a newline or before a word count as one encode does; only a hard cut can add a token."""
    load_o200k()
    counter = counter_for_model(ModelIdentity(id="gpt-4o", provider="openai"), local_encoder=True)
    single_line = " ".join(f"word{index}" for index in range(8_000))
    assert count(counter, single_line).tokens == _o200k(single_line)
    spaced = "a  b   c    d " * 3_000
    assert count(counter, spaced).tokens == _o200k(spaced)
    for run in ("abc" * 6_000, " " * 20_000):
        assert abs(count(counter, run).tokens - _o200k(run)) <= len(run) // 8_192 + 1


def test_a_frozen_counter_keeps_the_encoder_it_started_with(monkeypatch: pytest.MonkeyPatch) -> None:
    """A counter frozen before the encoder loads keeps its stand-in, so its two measurements share one basis."""
    release = threading.Event()
    real_get_encoding = tiktoken.get_encoding

    def slow_get_encoding(name: str) -> tiktoken.Encoding:
        release.wait()
        return real_get_encoding(name)

    monkeypatch.setattr(families, "_o200k_encoder", families._Encoder())
    monkeypatch.setattr(tiktoken, "get_encoding", slow_get_encoding)
    counter = counter_for_model(ModelIdentity(id="qwen3.8:27b", provider="ollama"))
    frozen = counts.frozen(counter)
    before = count(frozen, "some memory text " * 50)

    release.set()
    load_o200k()

    assert count(frozen, "some memory text " * 50) == before
