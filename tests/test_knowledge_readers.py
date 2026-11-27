"""Knowledge file readers, chunking policies, and error propagation invariants."""

from __future__ import annotations

import json
import sys
import zipfile
from datetime import date, datetime
from itertools import pairwise
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from pypdf import PdfWriter
from pypdf.errors import PdfStreamError
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.embeddings.batching import RefreshEmbedder
from mindroom.knowledge.chunking import (
    KnowledgeChunk,
    chunk_fixed,
    chunk_memory_entries,
    chunk_paragraphs,
    chunk_rows,
    chunk_text,
)
from mindroom.knowledge.file_indexer import FileIndexer
from mindroom.knowledge.readers import (
    KnowledgeReadError,
    _clean_page_numbers,
    _sanitize_pdf_text,
    ensure_reader_available,
    read_knowledge_file,
)
from mindroom.knowledge.vector_store import KnowledgeCollection
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.knowledge_test_support import _FakeEmbedder, collection_meta

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

#: Chunk settings no default could be mistaken for (default is 5000/0).
_CHUNK_SIZE = 137
_CHUNK_OVERLAP = 11

#: Suffixes that use the knowledge base's text chunking policy.
_TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".py", ".yaml", ".html", ".xyz", "")


def _write_text_pdf(path: Path, pages: list[str]) -> None:
    """Write a minimal multi-page PDF with extractable Helvetica text using only ``pypdf``."""
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        },
    )
    font_ref = writer._add_object(font)
    for page_text in pages:
        page = writer.add_blank_page(width=400, height=400)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})},
        )
        ops = ["BT", "/F1 12 Tf", "50 350 Td", "14 TL"]
        for index, line in enumerate(page_text.split("\n")):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({escaped}) Tj" if index == 0 else f"T* ({escaped}) Tj")
        ops.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(ops).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


def _write_locked_pdf(path: Path, user_code: str, owner_code: str | None = None) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_code, owner_code)
    with path.open("wb") as handle:
        writer.write(handle)


def _indexer(root: Path, *, chunk_size: int = _CHUNK_SIZE, chunk_overlap: int = _CHUNK_OVERLAP) -> FileIndexer:
    """Build the file indexer of one knowledge base authoring these chunk settings."""
    docs_path = root / "docs"
    docs_path.mkdir(parents=True, exist_ok=True)
    runtime_paths = test_runtime_paths(root)
    config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=["docs"])},
            memory={},
            knowledge_bases={
                "docs": KnowledgeBaseConfig(
                    path=str(docs_path),
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                ),
            },
        ),
        runtime_paths,
    )
    collection = KnowledgeCollection(path=root / "collections", name="docs")
    collection.create(collection_meta())
    return FileIndexer(
        base_id="docs",
        config=config,
        runtime_paths=runtime_paths_for(config),
        knowledge_root=docs_path,
        collection=collection,
        build_embedder=lambda: RefreshEmbedder(_FakeEmbedder()),
        max_concurrent_file_indexes=1,
    )


@pytest.fixture
def indexer(tmp_path: Path) -> FileIndexer:
    """Return the file indexer of a base that authors distinctive chunk settings."""
    return _indexer(tmp_path)


def _distinct_text(length: int) -> str:
    """Return whitespace-free text whose every position is identifiable."""
    return "".join(chr(ord("a") + index % 26) for index in range(length))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"value": "A\u00a0B\u2028C\u3000D"}, ['{"value": "A B C D"}']),
        # Preserving Unicode whitespace would split this into chunks of 5, 5000, and 1005 characters.
        ({"v": ("A\u00a0\u00a0" * 2000) + "B"}, ['{"v": "' + "A " * 2000 + 'B"}']),
    ],
    ids=["unicode-whitespace", "long"],
)
def test_json_files_chunk_as_agno_3_0_did(
    value: dict[str, str],
    expected: list[str],
    indexer: FileIndexer,
) -> None:
    """JSON keeps 5000/0 fixed-size split, and its chunks collapse Unicode whitespace as legacy indexing did."""
    source = indexer.knowledge_root / "source.json"
    source.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    prepared = indexer._read_file(source)

    assert prepared is not None
    assert [chunk.text for chunk in prepared.chunks] == expected


def test_paragraph_chunking_collapses_intra_paragraph_whitespace_at_5000_chars() -> None:
    """PDF, DOCX, and PPTX keep paragraph chunking at 5000 chars and collapse intra-paragraph whitespace."""
    paragraphs = ["\n".join(f"Paragraph {n}, line {i}:\tsome  words\there." for i in range(70)) for n in range(3)]

    chunks = chunk_paragraphs("\n\n".join(paragraphs), name="source", chunk_size=5000)

    # One chunk per 2719-character paragraph.
    assert [chunk.text for chunk in chunks] == [
        " ".join(f"Paragraph {n}, line {i}: some words here." for i in range(70)) for n in range(3)
    ]


@pytest.mark.parametrize("suffix", _TEXT_SUFFIXES)
def test_text_files_carry_this_bases_chunking(suffix: str, indexer: FileIndexer) -> None:
    """Every text-like extension splits on the base's configured chunk size and overlap."""
    source = indexer.knowledge_root / f"source{suffix}"
    source.write_text(_distinct_text(400), encoding="utf-8")

    prepared = indexer._read_file(source)

    assert prepared is not None
    chunks = [chunk.text for chunk in prepared.chunks]
    assert [len(chunk) for chunk in chunks] == [137, 137, 137, 22]
    for earlier, later in pairwise(chunks):
        assert later[:_CHUNK_OVERLAP] == earlier[-_CHUNK_OVERLAP:]


def test_a_second_base_moves_chunk_boundaries(tmp_path: Path) -> None:
    """A different authored config reaches the reader and moves where chunks split."""
    other_indexer = _indexer(tmp_path / "base", chunk_size=512, chunk_overlap=64)
    source = other_indexer.knowledge_root / "notes.txt"
    source.write_text(_distinct_text(1200), encoding="utf-8")

    prepared = other_indexer._read_file(source)

    assert prepared is not None
    chunks = [chunk.text for chunk in prepared.chunks]
    assert [len(chunk) for chunk in chunks] == [512, 512, 304]
    for earlier, later in pairwise(chunks):
        assert later[:64] == earlier[-64:]


def test_non_text_formats_ignore_base_text_chunk_size(indexer: FileIndexer) -> None:
    """CSV, valid JSON, and PDF keep their format-specific chunking instead of the base's text size."""
    csv_source = indexer.knowledge_root / "rows.csv"
    csv_source.write_text(f"header\n{'x' * 300}\n", encoding="utf-8")
    csv_prepared = indexer._read_file(csv_source)
    assert csv_prepared is not None
    assert [chunk.metadata["row_number"] for chunk in csv_prepared.chunks] == [1, 2]
    assert len(csv_prepared.chunks[1].text) == 300

    json_source = indexer.knowledge_root / "data.json"
    json_source.write_text(json.dumps({"value": "x" * 300}), encoding="utf-8")
    json_prepared = indexer._read_file(json_source)
    assert json_prepared is not None
    assert len(json_prepared.chunks) == 1
    assert json_prepared.chunks[0].metadata["page"] == 1

    pdf_source = indexer.knowledge_root / "doc.pdf"
    _write_text_pdf(pdf_source, ["x" * 300])
    pdf_prepared = indexer._read_file(pdf_source)
    assert pdf_prepared is not None
    assert len(pdf_prepared.chunks) == 1
    assert pdf_prepared.chunks[0].metadata == {"page": 1}


def test_json_keeps_structured_chunking_and_falls_back_on_malformed_json(tmp_path: Path) -> None:
    """Valid JSON splits per element at 5000/0; malformed JSON reports line/col and falls back to text chunking."""
    valid = tmp_path / "valid.json"
    valid.write_text('[{"claim": "one"}, {"claim": "two"}]', encoding="utf-8")
    callback_calls: list[tuple[int, int]] = []
    valid_chunks = read_knowledge_file(
        valid,
        name="valid.json",
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
        on_malformed_json=lambda line, col: callback_calls.append((line, col)),
    )
    assert callback_calls == []
    assert [chunk.text for chunk in valid_chunks] == ['{"claim": "one"}', '{"claim": "two"}']
    assert {chunk.name for chunk in valid_chunks} == {"valid.json"}

    big = tmp_path / "big.json"
    big.write_text(json.dumps({"claim": "x" * 6013}), encoding="utf-8")
    big_chunks = read_knowledge_file(big, chunk_size=_CHUNK_SIZE, chunk_overlap=_CHUNK_OVERLAP)
    assert [len(chunk.text) for chunk in big_chunks] == [9, 5000, 1017]

    # Indented broken key so line and column differ.
    malformed = tmp_path / "claim.json"
    malformed.write_text('{\n  "claim": "' + _distinct_text(400) + '",\n     “broken”: true\n}\n', encoding="utf-8")
    malformed_chunks = read_knowledge_file(
        malformed,
        name="claim.json",
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
        on_malformed_json=lambda line, col: callback_calls.append((line, col)),
    )
    assert callback_calls == [(3, 6)]
    assert {chunk.name for chunk in malformed_chunks} == {"claim.json"}
    assert len(malformed_chunks) > 1
    assert max(len(chunk.text) for chunk in malformed_chunks) <= _CHUNK_SIZE


@pytest.mark.parametrize("kind", ["pdf", "docx", "json"])
def test_exact_indexed_chunk_output_for_pdf_docx_and_json(
    kind: str,
    indexer: FileIndexer,
) -> None:
    """Pin exact chunk text, name, and metadata across PDF, DOCX, and JSON indexing paths."""
    if kind == "pdf":
        source = indexer.knowledge_root / "report.pdf"
        _write_text_pdf(source, ["1\nFirst line\nwrapped", "2\nSecond page text"])
        prepared = indexer._read_file(source)
        assert prepared is not None
        assert [(chunk.name, chunk.metadata, chunk.text) for chunk in prepared.chunks] == [
            ("report.pdf", {"page": 1}, "<start page 1>\nFirst line wrapped\n<end page 1>"),
            ("report.pdf", {"page": 2}, "<start page 2>\nSecond page text\n<end page 2>"),
        ]
        return

    if kind == "docx":
        docx = pytest.importorskip("docx")
        source = indexer.knowledge_root / "spec.docx"
        para_a = " ".join(f"alpha{i}" for i in range(420))
        para_b = " ".join(f"beta{i}" for i in range(420))
        document = docx.Document()
        document.add_paragraph(para_a.replace(" ", "\t", 1))
        document.add_paragraph(para_b)
        document.save(source)
        prepared = indexer._read_file(source)
        assert prepared is not None
        assert [(chunk.name, chunk.metadata, chunk.text) for chunk in prepared.chunks] == [
            ("spec.docx", {"chunk": 1, "chunk_size": len(para_a)}, para_a),
            ("spec.docx", {"chunk": 2, "chunk_size": len(para_b)}, para_b),
        ]
        return

    source = indexer.knowledge_root / "items.json"
    source.write_text(json.dumps([{"k": "a\u00a0b"}, {"k": "c"}], ensure_ascii=False), encoding="utf-8")
    prepared = indexer._read_file(source)
    assert prepared is not None
    assert [(chunk.name, chunk.metadata, chunk.text) for chunk in prepared.chunks] == [
        ("items.json", {"page": 1, "chunk": 1, "chunk_size": 12}, '{"k": "a b"}'),
        ("items.json", {"page": 2, "chunk": 1, "chunk_size": 10}, '{"k": "c"}'),
    ]


def test_chunk_fixed_validates_parameters_and_handles_safe_and_legacy_modes() -> None:
    """``chunk_fixed`` validates bounds, collapses whitespace, and distinguishes safe vs legacy fill ratios."""
    with pytest.raises(ValueError, match="chunk_size"):
        chunk_fixed("abc", chunk_size=0)
    with pytest.raises(ValueError, match="overlap"):
        chunk_fixed("abc", chunk_size=10, overlap=10)
    with pytest.raises(ValueError, match="overlap"):
        chunk_fixed("abc", chunk_size=10, overlap=-1)
    with pytest.raises(ValueError, match="min_chunk_fill_ratio"):
        chunk_fixed("abc", chunk_size=10, min_chunk_fill_ratio=0)
    with pytest.raises(ValueError, match="min_chunk_fill_ratio"):
        chunk_fixed("abc", chunk_size=10, min_chunk_fill_ratio=1.5)

    assert chunk_fixed("", chunk_size=12, overlap=3) == []

    # Whitespace sits at index 2 (< 50% of chunk_size=10): safe mode hard-splits at 10; legacy mode splits at 2.
    text = "ab cdefghijklmnop"
    safe = chunk_fixed(text, chunk_size=10, overlap=0, min_chunk_fill_ratio=0.5)
    legacy = chunk_fixed(text, chunk_size=10, overlap=0, min_chunk_fill_ratio=None)
    assert [chunk.text for chunk in safe] == ["ab cdefghi", "jklmnop"]
    assert [chunk.text for chunk in legacy] == ["ab", " cdefghijk", "lmnop"]


def test_chunk_paragraphs_splits_oversized_paragraphs_by_sentence() -> None:
    """``chunk_paragraphs`` keeps short text intact and splits oversized paragraphs on sentence boundaries."""
    assert chunk_paragraphs("Fits\nin one chunk.", name="short", chunk_size=40) == [
        KnowledgeChunk(text="Fits\nin one chunk.", name="short", metadata={}),
    ]

    long_para = "First sentence here. Second sentence here. Third sentence here."
    chunks = chunk_paragraphs(f"Intro para.\n\n{long_para}", name="doc", chunk_size=40)
    assert [(chunk.text, chunk.metadata) for chunk in chunks] == [
        ("Intro para.", {"chunk": 1, "chunk_size": 11}),
        ("First sentence here.", {"chunk": 2, "chunk_size": 20}),
        ("Second sentence here.", {"chunk": 3, "chunk_size": 21}),
        ("Third sentence here.", {"chunk": 4, "chunk_size": 20}),
    ]


def test_chunk_rows_skips_blank_rows_without_shifting_row_numbers() -> None:
    """``chunk_rows`` normalizes whitespace and preserves 1-based logical row numbers across blank lines."""
    chunks = chunk_rows("h1, h2\n  a ,  1 \n\nb, 2\n", name="data")
    assert [(chunk.metadata["row_number"], chunk.text) for chunk in chunks] == [
        (1, "h1, h2"),
        (2, "a , 1"),
        (4, "b, 2"),
    ]


def test_chunk_memory_entries_splits_entries_and_falls_back_when_empty() -> None:
    """``chunk_memory_entries`` emits one chunk per entry and falls back to fixed-size chunking for headings alone."""
    entries = chunk_text(
        "- [id=m_20260101120000_aaaa] first fact\n- [id=m_20260101120100_bbbb] second  fact\n",
        name="daily.md",
        chunk_size=200,
        strategy="memory_entries",
    )
    assert [(chunk.text, chunk.metadata) for chunk in entries] == [
        ("- [id=m_20260101120000_aaaa] first fact", {"chunk": 1, "chunk_size": 39}),
        ("- [id=m_20260101120100_bbbb] second fact", {"chunk": 2, "chunk_size": 40}),
    ]

    fallback = chunk_memory_entries("# Lone heading\n", name="topic.md", chunk_size=200)
    assert [(chunk.text, chunk.metadata) for chunk in fallback] == [
        ("# Lone heading ", {"chunk": 1, "chunk_size": 15}),
    ]


def test_csv_reader_normalizes_multiline_cells(tmp_path: Path) -> None:
    """CSV reading flattens newlines inside quoted cells and chunks rows cleanly."""
    source = tmp_path / "notes.csv"
    source.write_text('name,notes\n"Alice","line1\nline2"\n\nBob,42\n', encoding="utf-8")

    chunks = read_knowledge_file(source, name="notes.csv")
    assert [(c.name, c.metadata["row_number"], c.text) for c in chunks] == [
        ("notes.csv", 1, "name, notes"),
        ("notes.csv", 2, "Alice, line1 line2"),
        ("notes.csv", 4, "Bob, 42"),
    ]


def test_excel_reader_reads_xlsx_sheets(tmp_path: Path) -> None:
    """XLSX reading normalizes cell types, trims trailing empties, and skips empty sheets."""
    openpyxl = pytest.importorskip("openpyxl")
    source = tmp_path / "metrics.xlsx"
    workbook = openpyxl.Workbook()
    first = workbook.active
    assert first is not None
    first.title = "Metrics"
    first.append(["City", "Count", "Date", "Notes", None])
    first.append(["Amsterdam", 42.0, date(2026, 11, 25), "line1\r\nline2", ""])
    first.append(["Berlin", 3.5, datetime(2026, 11, 25, 10, 30), "ok", None])  # noqa: DTZ001
    workbook.create_sheet("Empty")
    second = workbook.create_sheet("Summary")
    second.append(["Key", "Value"])
    second.append(["total", 2])
    workbook.save(source)

    chunks = read_knowledge_file(source, name="metrics.xlsx")
    assert [(c.name, c.metadata["sheet_name"], c.metadata["sheet_index"], c.text) for c in chunks] == [
        ("metrics", "Metrics", 1, "City, Count, Date, Notes"),
        ("metrics", "Metrics", 1, "Amsterdam, 42, 2026-11-25T00:00:00, line1 line2"),
        ("metrics", "Metrics", 1, "Berlin, 3.5, 2026-11-25T10:30:00, ok"),
        ("metrics", "Summary", 3, "Key, Value"),
        ("metrics", "Summary", 3, "total, 2"),
    ]


def test_excel_reader_streams_xls_sheets_lazily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``.xls`` reading streams one sheet's rows into the formatter before opening the next sheet."""
    events: list[tuple[str, object]] = []

    class _TrackedValue:
        def __init__(self, label: str) -> None:
            self.label = label

        def __str__(self) -> str:
            events.append(("format", self.label))
            return self.label

    class _FakeSheet:
        def __init__(self, name: str, rows: list[list[tuple[object, int]]]) -> None:
            self.name = name
            self._rows = rows
            self.nrows = len(rows)
            self.ncols = len(rows[0]) if rows else 0

        def cell_value(self, row: int, col: int) -> object:
            events.append(("read", f"{self.name}:{row}:{col}"))
            return self._rows[row][col][0]

        def cell_type(self, row: int, col: int) -> int:
            return self._rows[row][col][1]

    class _FakeWorkbook:
        datemode = 0

        def __init__(self) -> None:
            self._sheets = [
                _FakeSheet("First", [[(_TrackedValue("a1"), 1)], [(_TrackedValue("a2"), 1)]]),
                _FakeSheet("Second", [[(46000.0, 3), (1, 4)]]),
            ]
            self.nsheets = len(self._sheets)

        def sheet_by_index(self, index: int) -> _FakeSheet:
            events.append(("open", index))
            return self._sheets[index]

    fake_xlrd = ModuleType("xlrd")
    fake_xlrd.XL_CELL_DATE = 3  # type: ignore[attr-defined]
    fake_xlrd.XL_CELL_BOOLEAN = 4  # type: ignore[attr-defined]
    fake_xlrd.open_workbook = lambda **_kwargs: _FakeWorkbook()  # type: ignore[attr-defined]
    fake_xlrd.xldate_as_tuple = lambda _val, _mode: (2026, 11, 25, 0, 0, 0)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "xlrd", fake_xlrd)

    source = tmp_path / "legacy.xls"
    source.write_bytes(b"fake-xls")
    chunks = read_knowledge_file(source, name="legacy.xls")

    assert [(c.name, c.metadata["sheet_name"], c.metadata["sheet_index"], c.text) for c in chunks] == [
        ("legacy", "First", 1, "a1"),
        ("legacy", "First", 1, "a2"),
        ("legacy", "Second", 2, "2026-11-25T00:00:00, True"),
    ]
    assert events == [
        ("open", 0),
        ("read", "First:0:0"),
        ("format", "a1"),
        ("read", "First:1:0"),
        ("format", "a2"),
        ("open", 1),
        ("read", "Second:0:0"),
        ("read", "Second:0:1"),
    ]


def test_docx_reader_extracts_paragraphs(tmp_path: Path) -> None:
    """DOCX reading joins document paragraphs with blank lines."""
    docx = pytest.importorskip("docx")
    source = tmp_path / "spec.docx"
    document = docx.Document()
    document.add_paragraph("First paragraph.")
    document.add_paragraph("Second paragraph.")
    document.save(source)

    chunks = read_knowledge_file(source, name="spec.docx")
    assert [(c.name, c.text) for c in chunks] == [("spec.docx", "First paragraph.\n\nSecond paragraph.")]


def test_pdf_reader_helpers_and_encryption_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PDF reading sanitizes lines, cleans page numbers, unlocks blank passwords, and rejects locked PDFs."""
    assert _sanitize_pdf_text("word1\nword2\n\npara2\nline2") == "word1 word2\n\npara2 line2"
    cleaned, shift = _clean_page_numbers(["1\nFirst", "2\nSecond", "3\nThird"])
    assert shift == 1
    assert cleaned == [
        "<start page 1>\nFirst\n<end page 1>",
        "<start page 2>\nSecond\n<end page 2>",
        "<start page 3>\nThird\n<end page 3>",
    ]

    locked = tmp_path / "locked.pdf"
    _write_locked_pdf(locked, "open-sesame")
    with pytest.raises(KnowledgeReadError, match="password protected"):
        read_knowledge_file(locked)

    blank_encrypted = tmp_path / "restricted.pdf"
    _write_locked_pdf(blank_encrypted, "", "owner-only")
    unlocked = read_knowledge_file(blank_encrypted, name="restricted.pdf")
    assert [(c.name, c.metadata, c.text) for c in unlocked] == [("restricted.pdf", {"page": 1}, "")]

    # A decrypt error also surfaces as KnowledgeReadError rather than silently returning [].
    class _ExplodingPdfReader:
        is_encrypted = True

        def __init__(self, _file: Path) -> None:
            pass

        def decrypt(self, _password: str) -> int:
            msg = "unsupported cipher"
            raise NotImplementedError(msg)

    monkeypatch.setattr("pypdf.PdfReader", _ExplodingPdfReader)
    with pytest.raises(KnowledgeReadError, match="password protected") as exc_info:
        read_knowledge_file(locked)
    assert isinstance(exc_info.value.__cause__, NotImplementedError)


def test_pptx_reader_extracts_grouped_shapes_and_reports_missing_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PPTX reading extracts text recursively through group shapes and fails cleanly when ``pptx`` is absent."""
    monkeypatch.setitem(sys.modules, "pptx", None)
    with pytest.raises(ImportError, match="python-pptx"):
        ensure_reader_available(".pptx")

    class _FakeGroupShape:
        def __init__(self, shapes: list[object]) -> None:
            self.shapes = shapes

    class _FakeTextShape:
        def __init__(self, text: str) -> None:
            self.text = text

    class _FakePresentation:
        def __init__(self, _source: object) -> None:
            self.slides = [
                SimpleNamespace(shapes=[_FakeTextShape("Title"), _FakeGroupShape([_FakeTextShape("Grouped")])]),
                SimpleNamespace(shapes=[]),
            ]

    fake_pptx = ModuleType("pptx")
    fake_pptx.Presentation = _FakePresentation  # type: ignore[attr-defined]
    fake_shapes = ModuleType("pptx.shapes")
    fake_group = ModuleType("pptx.shapes.group")
    fake_group.GroupShape = _FakeGroupShape  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pptx", fake_pptx)
    monkeypatch.setitem(sys.modules, "pptx.shapes", fake_shapes)
    monkeypatch.setitem(sys.modules, "pptx.shapes.group", fake_group)

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"fake")
    chunks = read_knowledge_file(deck, name="deck.pptx")
    assert [(c.name, c.text) for c in chunks] == [
        ("deck.pptx", "Slide 1:\nTitle\nGrouped\n\nSlide 2:\n(No text content)"),
    ]


@pytest.mark.parametrize(
    ("extension", "package"),
    [
        (".pdf", "pypdf"),
        (".docx", "docx"),
        (".doc", "docx"),
        (".xlsx", "openpyxl"),
        (".xls", "xlrd"),
        (".pptx", "pptx"),
    ],
)
def test_ensure_reader_available_raises_import_error_only_when_format_dependency_is_missing(
    extension: str,
    package: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing optional format dependencies raise ``ImportError`` when checking that format."""
    monkeypatch.setitem(sys.modules, package, None)
    with pytest.raises(ImportError):
        ensure_reader_available(extension)


def test_corrupt_and_unreadable_files_raise_explicit_errors(tmp_path: Path) -> None:
    """Readers never swallow decode or parser failures into empty chunk lists."""
    with pytest.raises(FileNotFoundError):
        read_knowledge_file(tmp_path / "missing.txt")

    corrupt_pdf = tmp_path / "corrupt.pdf"
    corrupt_pdf.write_bytes(b"not a valid pdf")
    with pytest.raises(PdfStreamError):
        read_knowledge_file(corrupt_pdf)

    for suffix in (".txt", ".json", ".csv"):
        invalid = tmp_path / f"invalid{suffix}"
        invalid.write_bytes(b"\xff\xfe\xfd")
        with pytest.raises(UnicodeDecodeError):
            read_knowledge_file(invalid)


def test_corrupt_office_files_raise_explicit_errors(tmp_path: Path) -> None:
    """Corrupt DOCX, XLSX, and XLS files raise parser errors instead of returning empty chunk lists."""
    docx_exc = pytest.importorskip("docx.opc.exceptions")
    corrupt_docx = tmp_path / "corrupt.docx"
    corrupt_docx.write_bytes(b"not a docx")
    with pytest.raises(docx_exc.PackageNotFoundError):
        read_knowledge_file(corrupt_docx)

    pytest.importorskip("openpyxl")
    corrupt_xlsx = tmp_path / "corrupt.xlsx"
    corrupt_xlsx.write_bytes(b"not an xlsx")
    with pytest.raises(zipfile.BadZipFile):
        read_knowledge_file(corrupt_xlsx)

    xlrd = pytest.importorskip("xlrd")
    corrupt_xls = tmp_path / "corrupt.xls"
    corrupt_xls.write_bytes(b"not an xls")
    with pytest.raises(xlrd.XLRDError):
        read_knowledge_file(corrupt_xls)


def _write_corrupt_bytes(path: Path) -> None:
    path.write_bytes(b"not a valid file")


def _write_locked_pdf_fixture(path: Path) -> None:
    _write_locked_pdf(path, "open-sesame")


def _write_invalid_utf8(path: Path) -> None:
    path.write_bytes(b"\xff\xfe\xfd")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "writer", "expected_error_type", "optional_pkg"),
    [
        ("corrupt.pdf", _write_corrupt_bytes, "PdfStreamError", None),
        ("locked.pdf", _write_locked_pdf_fixture, "KnowledgeReadError", None),
        ("invalid.txt", _write_invalid_utf8, "UnicodeDecodeError", None),
        ("corrupt.docx", _write_corrupt_bytes, "PackageNotFoundError", "docx"),
        ("corrupt.xlsx", _write_corrupt_bytes, "BadZipFile", "openpyxl"),
    ],
)
async def test_file_indexer_isolates_corrupt_files_and_records_first_error(
    indexer: FileIndexer,
    filename: str,
    writer: Callable[[Path], None],
    expected_error_type: str,
    optional_pkg: str | None,
) -> None:
    """A corrupt file fails with a recorded error while healthy sibling files still index cleanly."""
    if optional_pkg is not None:
        pytest.importorskip(optional_pkg)
    healthy = indexer.knowledge_root / "healthy.md"
    healthy.write_text("Healthy sibling document", encoding="utf-8")
    broken = indexer.knowledge_root / filename
    writer(broken)

    completed: dict[str, tuple[int, int, str]] = {}
    failed: set[str] = set()
    vanished: set[str] = set()

    indexed = await indexer.index_files(
        [broken, healthy],
        completed=completed,
        failed=failed,
        vanished=vanished,
        on_file_done=lambda: None,
    )

    assert indexed == 1
    assert set(completed) == {"healthy.md"}
    assert failed == {filename}
    assert vanished == set()
    assert indexer.first_error == f"knowledge indexing failed ({expected_error_type})"
