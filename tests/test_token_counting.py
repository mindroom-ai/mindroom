"""Tests for token counting through a route's counter: its own tokenizer, family estimates, and stand-ins."""
# ruff: noqa: D103

from __future__ import annotations

import codecs
import io
import math
import threading
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import tiktoken

from mindroom.llm.media import File
from mindroom.llm.provider import ModelIdentity, TokenCounter
from mindroom.token_counting import families
from mindroom.token_counting.counts import count, fit_text
from mindroom.token_counting.families import counter_for_model, load_o200k, o200k_counter
from mindroom.token_counting.requests import MEDIA_ALLOWANCE_TOKENS, _file_tokens, file_tokens_within_budget
from tests.fakes.token_counter import quarter_tokens
from tests.history_helpers import blank_pdf

if TYPE_CHECKING:
    from typing import BinaryIO


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


def test_file_tokens_within_budget_accepts_sparse_utf8_and_utf16_reports_over_512kb(
    tmp_path: Path,
) -> None:
    """Fixed-width whitespace reports over 512 KB (UTF-8) and 1.09 MB (UTF-16 BOM) under 32k tokens are accepted."""
    load_o200k()
    counter = o200k_counter()
    report_text = ("ITEM-001" + " " * 200 + "42.00\n") * 2_800
    exact_tokens = count(counter, report_text)
    assert 20_000 < exact_tokens < 32_000

    utf8_path = tmp_path / "report-utf8.txt"
    utf8_bytes = report_text.encode("utf-8")
    assert len(utf8_bytes) > 545_000
    utf8_path.write_bytes(utf8_bytes)

    utf16_path = tmp_path / "report-utf16.txt"
    utf16_bytes = codecs.BOM_UTF16_LE + report_text.encode("utf-16-le")
    assert len(utf16_bytes) > 1_090_000
    utf16_path.write_bytes(utf16_bytes)

    assert file_tokens_within_budget(
        counter,
        File(filepath=str(utf8_path), filename="report-utf8.txt", mime_type="text/plain"),
        text_budget=32_000,
        headroom_budget=None,
    ) == (exact_tokens, True)
    assert file_tokens_within_budget(
        counter,
        File(filepath=str(utf16_path), filename="report-utf16.txt", mime_type="text/plain"),
        text_budget=32_000,
        headroom_budget=None,
    ) == (exact_tokens, True)


def test_file_tokens_within_budget_aborts_early_without_reading_entire_large_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared text MIME files and early-binary archives abort reads early; untyped UTF-8 stops tokenizing early."""
    export_path = tmp_path / "export.json"
    export_path.write_bytes(b'{"items": "' + b"a" * 2_100_000 + b'"}')
    notes_path = tmp_path / "notes.txt"
    notes_path.write_bytes(b"entry line\n" * 200_000)
    untyped_log = tmp_path / "app.log"
    untyped_log.write_bytes(b"entry line\n" * 200_000)
    binary_archive = tmp_path / "archive.zip"
    binary_archive.write_bytes(b"PK\x03\x04\x89\xff" + b"\xff" * 2_100_000)

    bytes_read_by_path: dict[Path, int] = {}
    real_open = Path.open

    class _CountingReader:
        def __init__(self, inner: BinaryIO, target: Path) -> None:
            self._inner = inner
            self._target = target

        def read(self, size: int = -1) -> bytes:
            data = self._inner.read(size)
            bytes_read_by_path[self._target] = bytes_read_by_path.get(self._target, 0) + len(data)
            return data

        def __enter__(self) -> _CountingReader:
            return self

        def __exit__(self, *args: object) -> None:
            self._inner.close()

    def tracking_open(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        handle = real_open(self, mode, *args, **kwargs)  # type: ignore[arg-type]
        if "b" in mode and self in {export_path, notes_path, untyped_log, binary_archive}:
            return _CountingReader(handle, self)  # type: ignore[arg-type]
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)
    tokenized_chars = 0

    def counting_quarter_tokens(text: str) -> int:
        nonlocal tokenized_chars
        tokenized_chars += len(text)
        return quarter_tokens(text)

    counter = TokenCounter(estimate=counting_quarter_tokens)

    assert (
        file_tokens_within_budget(
            counter,
            File(filepath=str(export_path), filename="export.json", mime_type="application/json"),
            text_budget=32_000,
            headroom_budget=None,
        )
        is None
    )
    assert bytes_read_by_path[export_path] < 300_000

    assert (
        file_tokens_within_budget(
            counter,
            File(filepath=str(notes_path), filename="notes.txt", mime_type="text/plain"),
            text_budget=32_000,
            headroom_budget=None,
        )
        is None
    )
    assert bytes_read_by_path[notes_path] < 300_000

    # Pure UTF-8 untyped file stops tokenizing early while scanning remaining bytes to rule out binary fallback.
    tokenized_chars = 0
    assert (
        file_tokens_within_budget(
            counter,
            File(filepath=str(untyped_log), filename="app.log"),
            text_budget=32_000,
            headroom_budget=None,
        )
        is None
    )
    assert tokenized_chars < 300_000

    # When binary fallback cannot fit headroom either, an untyped file aborts reading early too.
    bytes_read_by_path[untyped_log] = 0
    assert (
        file_tokens_within_budget(
            counter,
            File(filepath=str(untyped_log), filename="app.log"),
            text_budget=32_000,
            headroom_budget=4_000,
        )
        is None
    )
    assert bytes_read_by_path[untyped_log] < 300_000

    # Binary archive stays allowed under headroom even when text_budget is exhausted, and stops on chunk 1.
    assert file_tokens_within_budget(
        counter,
        File(filepath=str(binary_archive), filename="archive.zip", mime_type="application/zip"),
        text_budget=0,
        headroom_budget=20_000,
    ) == (MEDIA_ALLOWANCE_TOKENS, False)
    assert bytes_read_by_path[binary_archive] <= 65_536


def test_file_tokens_within_budget_classifies_uncompressed_zip_with_long_utf8_prefix_as_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uncompressed ZIP or binary file with a >32k-token valid-UTF-8 prefix still classifies as binary."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo("log.txt", date_time=(1980, 1, 1, 0, 0, 0))
        archive.writestr(info, ("entry line\n" * 20_000) + "xxxxx")
    zip_bytes = buf.getvalue()
    # The first 200 KB (>50k tokens under quarter_tokens) is valid UTF-8; binary central-directory bytes follow.
    zip_bytes[:200_000].decode("utf-8")
    with pytest.raises(UnicodeDecodeError):
        zip_bytes.decode("utf-8")

    zip_path = tmp_path / "bundle.zip"
    zip_path.write_bytes(zip_bytes)

    trailing_binary_path = tmp_path / "payload.bin"
    trailing_binary_path.write_bytes(b"entry line\n" * 20_000 + b"\xff" + b"a" * 2_000_000)
    truncated_utf8_path = tmp_path / "truncated.bin"
    truncated_utf8_path.write_bytes(b"entry line\n" * 20_000 + b"\xc3")

    bytes_read_by_path: dict[Path, int] = {}
    real_open = Path.open

    class _CountingReader:
        def __init__(self, inner: BinaryIO, target: Path) -> None:
            self._inner = inner
            self._target = target

        def read(self, size: int = -1) -> bytes:
            data = self._inner.read(size)
            bytes_read_by_path[self._target] = bytes_read_by_path.get(self._target, 0) + len(data)
            return data

        def __enter__(self) -> _CountingReader:
            return self

        def __exit__(self, *args: object) -> None:
            self._inner.close()

    def tracking_open(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        handle = real_open(self, mode, *args, **kwargs)  # type: ignore[arg-type]
        if "b" in mode and self == trailing_binary_path:
            return _CountingReader(handle, self)  # type: ignore[arg-type]
        return handle

    monkeypatch.setattr(Path, "open", tracking_open)
    estimate_calls = 0

    def counting_quarter_tokens(text: str) -> int:
        nonlocal estimate_calls
        estimate_calls += 1
        return quarter_tokens(text)

    counter = TokenCounter(estimate=counting_quarter_tokens)

    for mime_type in ("application/zip", None):
        zip_file = File(filepath=str(zip_path), filename="bundle.zip", mime_type=mime_type)
        assert _file_tokens(counter, zip_file) == MEDIA_ALLOWANCE_TOKENS
        estimate_calls = 0
        assert file_tokens_within_budget(
            counter,
            zip_file,
            text_budget=32_000,
            headroom_budget=20_000,
        ) == (MEDIA_ALLOWANCE_TOKENS, False)
        assert estimate_calls <= 3
        # Also classifies as binary when text_budget is already 0, without invoking the tokenizer.
        estimate_calls = 0
        assert file_tokens_within_budget(
            counter,
            zip_file,
            text_budget=0,
            headroom_budget=20_000,
        ) == (MEDIA_ALLOWANCE_TOKENS, False)
        assert estimate_calls == 0
        assert (
            file_tokens_within_budget(
                counter,
                zip_file,
                text_budget=32_000,
                headroom_budget=4_000,
            )
            is None
        )

    # Scanning stops immediately at the first invalid UTF-8 chunk without reading trailing megabytes.
    assert file_tokens_within_budget(
        counter,
        File(filepath=str(trailing_binary_path), filename="payload.bin"),
        text_budget=32_000,
        headroom_budget=20_000,
    ) == (MEDIA_ALLOWANCE_TOKENS, False)
    assert bytes_read_by_path[trailing_binary_path] < 400_000

    # A truncated multi-byte UTF-8 sequence at EOF after a long valid-UTF-8 prefix also classifies as binary.
    assert file_tokens_within_budget(
        counter,
        File(filepath=str(truncated_utf8_path), filename="truncated.bin"),
        text_budget=32_000,
        headroom_budget=20_000,
    ) == (MEDIA_ALLOWANCE_TOKENS, False)


def test_file_tokens_within_budget_separates_pdf_headroom_from_text_cap_and_handles_missing_and_cp1252(
    tmp_path: Path,
) -> None:
    """PDFs obey headroom_budget (not the 32k text cap), cp1252 text falls back cleanly, and missing files keep allowance."""
    pdf_path = tmp_path / "report.pdf"
    pdf_bytes = blank_pdf(10, padding_bytes=1_100_000)
    assert len(pdf_bytes) > 1_000_000
    pdf_path.write_bytes(pdf_bytes)
    notes_path = tmp_path / "notes.txt"
    notes_path.write_bytes(b"smart \x93quotes\x94 " * 20)

    counter = TokenCounter(estimate=quarter_tokens)
    pdf_file = File(filepath=str(pdf_path), filename="report.pdf", mime_type="application/pdf")
    notes_file = File(filepath=str(notes_path), filename="notes.txt", mime_type="text/plain")
    missing_file = File(filepath=str(tmp_path / "missing.json"), filename="missing.json", mime_type="application/json")

    # 10 pages * 5,000 = 50,000 tokens exceeds text_budget=32,000, but fits headroom_budget=90,000.
    assert file_tokens_within_budget(counter, pdf_file, text_budget=32_000, headroom_budget=90_000) == (50_000, False)
    assert file_tokens_within_budget(counter, pdf_file, text_budget=32_000, headroom_budget=40_000) is None
    assert file_tokens_within_budget(counter, pdf_file, text_budget=32_000, headroom_budget=None) == (50_000, False)
    assert file_tokens_within_budget(counter, notes_file, text_budget=500, headroom_budget=500) == (
        quarter_tokens(b"smart \x93quotes\x94 ".decode("cp1252") * 20),
        True,
    )
    assert file_tokens_within_budget(counter, notes_file, text_budget=10, headroom_budget=500) is None
    assert file_tokens_within_budget(counter, missing_file, text_budget=0, headroom_budget=32_000) == (
        MEDIA_ALLOWANCE_TOKENS,
        False,
    )
    assert file_tokens_within_budget(counter, notes_file, text_budget=32_000, headroom_budget=0) is None
