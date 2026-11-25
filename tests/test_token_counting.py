"""Tests for token counting through a route's counter: its own tokenizer, family estimates, and stand-ins."""
# ruff: noqa: D103

from __future__ import annotations

import io
import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import tiktoken
from pypdf import PdfWriter

from mindroom.llm.media import File
from mindroom.llm.provider import ModelIdentity, TokenCounter
from mindroom.token_counting import families
from mindroom.token_counting.counts import count, fit_text
from mindroom.token_counting.families import counter_for_model, load_o200k
from mindroom.token_counting.requests import MEDIA_ALLOWANCE_TOKENS, file_tokens_within_budget
from tests.fakes.token_counter import quarter_tokens

if TYPE_CHECKING:
    import pytest


def _o200k(text: str) -> int:
    return len(load_o200k().encode(text, disallowed_special=()))


class _OneTokenPerCharacter:
    def encode(self, text: str, *, disallowed_special: tuple[str, ...]) -> list[str]:  # noqa: ARG002
        return list(text)


def test_openai_ids_count_with_their_own_encoding_only_on_openai_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """gpt-4 uses cl100k_base on OpenAI's routes; a server that may serve another model under its name estimates."""
    load_o200k()
    monkeypatch.setitem(families._encoders, "cl100k_base", families._Encoder(value=_OneTokenPerCharacter()))
    gpt4 = ModelIdentity(id="gpt-4", provider="openai")

    assert count(counter_for_model(gpt4, local_encoder=True), "hello world") == 11
    assert count(counter_for_model(gpt4), "hello world") == _o200k("hello world")
    for model_id in ("gpt-4o", "openai/gpt-4.1", "gpt-oss-120b"):
        counter = counter_for_model(ModelIdentity(id=model_id, provider="openai"), local_encoder=True)
        assert count(counter, "hello world") == _o200k("hello world")


def test_a_fine_tuned_model_counts_with_its_base_model_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tiktoken maps every ft:gpt-4 prefix to cl100k_base, including fine-tunes of gpt-4.1, which use o200k_base."""
    load_o200k()
    monkeypatch.setitem(families._encoders, "cl100k_base", families._Encoder(value=_OneTokenPerCharacter()))
    for model_id, tokens in (
        ("ft:gpt-4.1-2025-04-14:acme::abc123", _o200k("hello world")),
        ("ft:gpt-3.5-turbo-0125:acme::abc123", 11),
    ):
        counter = counter_for_model(ModelIdentity(id=model_id, provider="openai"), local_encoder=True)
        assert count(counter, "hello world") == tokens


def test_unmapped_ids_estimate_with_their_family_margin() -> None:
    load_o200k()
    text = "Ein Satz mit Zahlen 2026-10-31 und `code()`.\n" * 20
    claude = counter_for_model(ModelIdentity(id="claude-opus-5-5", provider="anthropic"))
    assert claude.estimate(text) == math.ceil(_o200k(text) * 1.8)
    bedrock = counter_for_model(ModelIdentity(id="anthropic.claude-haiku-4-5-v1:0", provider="bedrock"))
    assert bedrock.estimate(text) == math.ceil(_o200k(text) * 1.8)
    unmapped_openai = counter_for_model(ModelIdentity(id="gpt-6-astra", provider="openai"), local_encoder=True)
    assert unmapped_openai.estimate(text) == _o200k(text)
    other = counter_for_model(ModelIdentity(id="qwen3.8:27b", provider="ollama"))
    assert other.estimate(text) == math.ceil(_o200k(text) * 1.25)


def test_a_gemini_model_prices_pdf_pages_low_on_any_route() -> None:
    """Gemini charges far less than the allowance for a PDF page, also behind OpenRouter; other models keep it."""
    routed = counter_for_model(ModelIdentity(id="google/gemini-3.8-flash", provider="openrouter"))
    native = counter_for_model(ModelIdentity(id="models/default", provider="gemini"), family="gemini")
    claude = counter_for_model(ModelIdentity(id="anthropic/claude-opus-5.5", provider="openrouter"))
    assert routed.pdf_page_tokens == native.pdf_page_tokens == 2_000
    assert claude.pdf_page_tokens is None


def test_estimates_wait_for_the_encoder_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    real_get_encoding = tiktoken.get_encoding

    def slow_get_encoding(name: str) -> tiktoken.Encoding:
        release.wait()
        return real_get_encoding(name)

    monkeypatch.setitem(families._encoders, "o200k_base", families._Encoder())
    monkeypatch.setattr(tiktoken, "get_encoding", slow_get_encoding)
    counter = counter_for_model(ModelIdentity(id="claude-opus-5-5", provider="anthropic"))
    text = "naïve text"

    assert count(counter, text) == -(-len(text) // 4)

    release.set()
    assert load_o200k() is not None
    assert count(counter, text) == math.ceil(_o200k(text) * 1.8)


def test_a_failed_vocabulary_load_retries_after_a_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A network blip at startup must not leave the process on the stand-in until it restarts.

    A download has no timeout, so the retry waits rather than starting with every count.
    """

    def offline(_name: str) -> tiktoken.Encoding:
        msg = "no network"
        raise OSError(msg)

    monkeypatch.setitem(families._encoders, "o200k_base", families._Encoder())
    monkeypatch.setattr(tiktoken, "get_encoding", offline)
    families._load_in_background("o200k_base")
    encoder = families._encoders["o200k_base"]
    loads: list[str] = []
    monkeypatch.setattr(families, "_load_in_background", loads.append)

    assert families._loaded("o200k_base") is None
    assert not encoder.started, "the pause after a failure has not passed"

    monkeypatch.setattr(families.time, "monotonic", lambda: encoder.retry_at)
    families._loaded("o200k_base")

    assert encoder.started


def test_without_its_vocabulary_a_size_that_must_not_undercount_counts_bytes() -> None:
    """Compaction input still has an upper bound when o200k_base cannot load, while history keeps the quarter token.

    A quarter token per character counts Japanese text at a quarter of its real size, so a summary request sized by it
    would fail every time.
    """
    text = "今日は会議の議事録をまとめました。" * 20
    counter = TokenCounter(estimate=lambda _text: None)
    assert count(counter, text) == -(-len(text) // 4)
    assert count(counter, text, upper_bound=True) == len(text.encode())


def test_without_its_vocabulary_no_text_counts_as_free() -> None:
    """A short line costs at least one token, so a token limit still binds on text of short lines."""
    counter = TokenCounter(estimate=lambda _text: None)
    assert count(counter, "ab\n") == 1
    assert count(counter, "") == 0


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
    assert count(counter, prose) == _o200k(prose)


def test_pieces_count_exactly_unless_a_run_has_no_newline_or_word_boundary() -> None:
    """Pieces cut after a newline or before a word count as one encode does; only a hard cut can add a token."""
    load_o200k()
    counter = counter_for_model(ModelIdentity(id="gpt-4o", provider="openai"), local_encoder=True)
    single_line = " ".join(f"word{index}" for index in range(8_000))
    assert count(counter, single_line) == _o200k(single_line)
    spaced = "a  b   c    d " * 3_000
    assert count(counter, spaced) == _o200k(spaced)
    for run in ("abc" * 6_000, " " * 20_000):
        assert abs(count(counter, run) - _o200k(run)) <= len(run) // 8_192 + 1


def _padded_pdf(pages: int, padding_bytes: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Padding": "p" * padding_bytes})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_file_tokens_within_budget_skips_reading_and_tokenizing_large_text_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized non-PDF text files fail the fast byte guard via stat() + 5-byte peek without full reads or tokenizing."""
    export_path = tmp_path / "export.json"
    export_path.write_bytes(b'{"items": "' + b"x" * 700_000 + b'"}')
    untyped_log = tmp_path / "app.log"
    untyped_log.write_bytes(b"entry\n" * 120_000)
    binary_archive = tmp_path / "archive.zip"
    binary_archive.write_bytes(b"PK\x03\x04\x89\xff" + b"\xff" * 700_000)

    tokenized: list[int] = []
    counter = TokenCounter(estimate=lambda value: (tokenized.append(len(value)), quarter_tokens(value))[1])

    def forbid_read_bytes(self: Path) -> bytes:
        msg = f"unexpected full read of {self}"
        raise AssertionError(msg)

    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)

    assert (
        file_tokens_within_budget(
            counter,
            File(filepath=str(export_path), filename="export.json", mime_type="application/json"),
            32_000,
        )
        is None
    )
    assert file_tokens_within_budget(counter, File(filepath=str(untyped_log), filename="app.log"), 32_000) is None
    assert (
        file_tokens_within_budget(
            counter,
            File(filepath=str(binary_archive), filename="archive.zip", mime_type="application/zip"),
            32_000,
        )
        == MEDIA_ALLOWANCE_TOKENS
    )
    assert tokenized == []


def test_file_tokens_within_budget_counts_large_pdfs_by_page_and_preserves_small_and_missing_files(
    tmp_path: Path,
) -> None:
    """Multi-MB low-page PDFs still count by page, small files by tokens, and missing files by MEDIA_ALLOWANCE_TOKENS."""
    pdf_path = tmp_path / "report.pdf"
    pdf_bytes = _padded_pdf(2, 1_100_000)
    assert len(pdf_bytes) > 1_000_000
    pdf_path.write_bytes(pdf_bytes)
    notes_path = tmp_path / "notes.txt"
    notes_path.write_text("hello world " * 40, encoding="utf-8")

    counter = TokenCounter(estimate=quarter_tokens, pdf_page_tokens=2_000)
    pdf_file = File(filepath=str(pdf_path), filename="report.pdf", mime_type="application/pdf")
    notes_file = File(filepath=str(notes_path), filename="notes.txt", mime_type="text/plain")
    missing_file = File(filepath=str(tmp_path / "missing.json"), filename="missing.json", mime_type="application/json")

    assert file_tokens_within_budget(counter, pdf_file, 32_000) == 4_000
    assert file_tokens_within_budget(counter, pdf_file, 3_000) is None
    assert file_tokens_within_budget(counter, notes_file, 500) == quarter_tokens("hello world " * 40)
    assert file_tokens_within_budget(counter, notes_file, 50) is None
    assert file_tokens_within_budget(counter, missing_file, 32_000) == MEDIA_ALLOWANCE_TOKENS
    assert file_tokens_within_budget(counter, notes_file, 0) is None
