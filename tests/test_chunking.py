"""Worst-case chunk-expansion bounds for :class:`SafeFixedSizeChunking`.

The embedding prefetch in ``mindroom.knowledge.manager`` decides whether to
read a file at all from ``max_chunk_text_bytes``, so a bound that ever
underestimates what ``chunk`` materializes would silently unbound prefetch
memory. Every test here therefore compares the bound against the real chunker
rather than against a restatement of its formula.
"""

from __future__ import annotations

import itertools

import pytest
from agno.knowledge.document.base import Document
from hypothesis import given, settings
from hypothesis import strategies as st

from mindroom.chunking import SafeFixedSizeChunking

#: One 2-byte, one 3-byte and one 4-byte character, so generated content
#: exercises the gap between character counts and UTF-8 byte counts.
_MULTIBYTE = "é中\U0001f600"

#: Fill ratios covering an overlap below, at and above the shortest accepted
#: chunk, which is where the smallest chunk-to-chunk advance changes shape.
_FILL_RATIOS = (0.1, 0.5, 0.9, 1.0)


def _emitted_and_bound(
    content: str,
    *,
    chunk_size: int,
    overlap: int,
    fill_ratio: float = 0.5,
) -> tuple[int, int]:
    """Return the UTF-8 bytes chunking really emits and the bound claimed for them."""
    strategy = SafeFixedSizeChunking(
        chunk_size=chunk_size,
        overlap=overlap,
        min_chunk_fill_ratio=fill_ratio,
    )
    documents = strategy.chunk(Document(content=content))
    emitted = sum(len(document.content.encode("utf-8")) for document in documents)
    return emitted, strategy.max_chunk_text_bytes(len(content.encode("utf-8")))


@pytest.mark.parametrize("chunk_size", [128, 1_000, 5_000])
def test_zero_overlap_keeps_the_source_size_as_its_bound(chunk_size: int) -> None:
    """Without overlap the chunks partition the content, so the bound is the file size."""
    strategy = SafeFixedSizeChunking(chunk_size=chunk_size, overlap=0)

    assert strategy.max_chunk_text_bytes(0) == 0
    assert strategy.max_chunk_text_bytes(1) == 1
    assert strategy.max_chunk_text_bytes(8_000_000) == 8_000_000


def test_moderate_overlap_expands_by_a_small_factor() -> None:
    """A 10% overlap must stay cheap enough for ordinary files to be admitted."""
    strategy = SafeFixedSizeChunking(chunk_size=1_000, overlap=100)

    assert strategy.max_chunk_text_bytes(2_000_000) <= 2 * 2_000_000


def test_near_total_overlap_bound_reflects_the_real_amplification() -> None:
    """Overlap one below the chunk size really does multiply a small file."""
    content = "x" * 4_000
    source_bytes = len(content.encode("utf-8"))

    emitted, bound = _emitted_and_bound(content, chunk_size=128, overlap=127)

    assert emitted > 100 * source_bytes, "the pathological case stopped amplifying"
    assert emitted <= bound
    assert bound <= 128 * source_bytes


_SCENARIOS = {
    "empty": ("", 128, 64),
    "single character": ("a", 128, 64),
    "shorter than one chunk": ("word " * 10, 128, 64),
    "no overlap": ("word " * 500, 128, 0),
    "moderate overlap": ("word " * 500, 1_000, 100),
    "overlap one below chunk size": ("word " * 500, 128, 127),
    "no whitespace at all": ("x" * 2_000, 128, 64),
    "whitespace just inside the boundary": (("x" * 126 + " x"), 128, 64),
    "whitespace just past the minimum fill": (("x" * 65 + " "), 128, 64),
    "whitespace far from every boundary": (("x" * 200 + " ") * 5, 128, 32),
    "multibyte without overlap": (_MULTIBYTE * 500, 128, 0),
    "multibyte with moderate overlap": (_MULTIBYTE * 500, 128, 32),
    "multibyte with near-total overlap": (_MULTIBYTE * 200, 128, 127),
    "multibyte around whitespace": ((_MULTIBYTE * 20 + " ") * 20, 128, 64),
}


@pytest.mark.parametrize(("content", "chunk_size", "overlap"), _SCENARIOS.values(), ids=list(_SCENARIOS))
def test_bound_covers_named_scenarios(content: str, chunk_size: int, overlap: int) -> None:
    """Every hand-picked shape stays inside the bound computed from its byte size."""
    emitted, bound = _emitted_and_bound(content, chunk_size=chunk_size, overlap=overlap)

    assert emitted <= bound, f"chunking emitted {emitted} bytes past a bound of {bound}"


@pytest.mark.parametrize("fill_ratio", _FILL_RATIOS)
def test_bound_covers_every_small_whitespace_layout(fill_ratio: float) -> None:
    """Exhaust the whitespace layouts that drive the chunker's boundary search.

    Whether a chunk keeps its whitespace boundary or falls back to a hard split
    is what decides how far the next chunk start advances, so the smallest
    inputs where every layout is enumerable are the strongest evidence that no
    layout beats the bound. The fill ratio is swept too: it sets the shortest
    accepted chunk, so it decides where the smallest advance sits relative to
    the overlap, and fixing it at one value leaves that arithmetic untested.
    """
    for chunk_size in range(2, 9):
        for overlap in range(chunk_size):
            # Small chunk sizes get longer inputs so several chunks, and so
            # several overlap-driven advances, fit inside one enumerated layout.
            for length in range(13 if chunk_size <= 4 else 9):
                for layout in itertools.product("a ", repeat=length):
                    content = "".join(layout)
                    emitted, bound = _emitted_and_bound(
                        content,
                        chunk_size=chunk_size,
                        overlap=overlap,
                        fill_ratio=fill_ratio,
                    )
                    assert emitted <= bound, (
                        f"chunk_size={chunk_size} overlap={overlap} fill_ratio={fill_ratio} "
                        f"content={content!r} emitted {emitted} bytes past a bound of {bound}"
                    )


@st.composite
def _chunking_cases(draw: st.DrawFn) -> tuple[str, int, int, float]:
    chunk_size = draw(st.integers(min_value=2, max_value=64))
    overlap = draw(st.integers(min_value=0, max_value=chunk_size - 1))
    fill_ratio = draw(st.sampled_from(_FILL_RATIOS))
    content = draw(st.text(alphabet="ab \n\t" + _MULTIBYTE, max_size=200))
    return content, chunk_size, overlap, fill_ratio


@settings(max_examples=300, deadline=None)
@given(_chunking_cases())
def test_bound_never_underestimates_emitted_chunk_bytes(case: tuple[str, int, int, float]) -> None:
    """Generated content must never chunk into more bytes than the bound allows."""
    content, chunk_size, overlap, fill_ratio = case

    emitted, bound = _emitted_and_bound(
        content,
        chunk_size=chunk_size,
        overlap=overlap,
        fill_ratio=fill_ratio,
    )

    assert emitted <= bound
