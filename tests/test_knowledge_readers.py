"""Which reader answers for a knowledge file, and where it puts the chunk boundaries.

Chunk boundaries decide what a knowledge base's vector store contains, and
almost nothing else asserts them: a reader carrying the wrong chunking still
indexes, still publishes, and still answers queries, only against different
vectors. These tests start from a base's authored config, assert every reader
field that can move a boundary by value, and read real text so the boundaries
are observed rather than inferred from attributes.

They exercise ``FileIndexer``'s reader construction directly rather than
through ``reindex_all``. That is deliberate, and the reason is the hazard in
:func:`test_two_bases_neither_share_a_reader_nor_poison_the_factory_cache`:
``_build_reader`` configures the reader ``ReaderFactory`` returns in place,
which is safe only while the factory builds a new reader per call. Were readers
shared again, the existing end-to-end suites would still pass, because within
one refresh every ``_build_reader`` call re-applies that indexer's own chunking
before the reader is used, so a sequential refresh stays internally consistent.
The damage needs two bases' refreshes to interleave -- a real possibility, since
the source-root lock is per root and file reads run on worker threads, but not
something a deterministic end-to-end test can stage. Observing the constructed
reader is the only way to hold the invariant.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from importlib.util import find_spec
from itertools import pairwise
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.embeddings.batching import RefreshEmbedder
from mindroom.knowledge.chunking import (
    DocumentChunking,
    FixedSizeChunking,
    LegacyDocumentChunking,
    LegacyFixedSizeChunking,
    RowChunking,
    SafeFixedSizeChunking,
)
from mindroom.knowledge.file_indexer import FileIndexer, _InMemoryTextReader, _MalformedJSONSourceError
from mindroom.knowledge.readers import (
    CSVReader,
    Document,
    DocxReader,
    ExcelReader,
    MarkdownReader,
    PDFReader,
    PPTXReader,
    ReaderFactory,
    TextReader,
    _clean_page_numbers,
    _sanitize_pdf_text,
)
from mindroom.knowledge.vector_store import KnowledgeCollection
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.knowledge_test_support import _FakeEmbedder

if TYPE_CHECKING:
    from mindroom.knowledge.readers import Reader


def _package_available(package: str) -> bool:
    """Return whether optional dependency ``package`` can currently be imported."""
    if package in sys.modules:
        return sys.modules[package] is not None
    try:
        return find_spec(package) is not None
    except ValueError:
        return False


#: Chunk settings no factory default could be mistaken for (default is 5000/0).
_CHUNK_SIZE = 137
_CHUNK_OVERLAP = 11

#: Suffixes whose reader MindRoom reconfigures with the base's chunking.
_CHUNKED_SUFFIXES = (".md", ".markdown", ".txt", ".py", ".yaml", ".html", ".xyz", "")
#: Suffixes whose reader owns its own splitting and must be left alone.
_UNTOUCHED_SUFFIXES = (
    ".csv",
    pytest.param(
        ".xlsx",
        marks=pytest.mark.skipif(not _package_available("openpyxl"), reason="openpyxl not installed"),
    ),
    pytest.param(
        ".docx",
        marks=pytest.mark.skipif(not _package_available("docx"), reason="python-docx not installed"),
    ),
)


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
    return FileIndexer(
        base_id="docs",
        config=config,
        runtime_paths=runtime_paths_for(config),
        knowledge_root=docs_path,
        collection=KnowledgeCollection(path=root / "collections", name="docs"),
        build_embedder=lambda: RefreshEmbedder(_FakeEmbedder()),
        max_concurrent_file_indexes=1,
    )


@pytest.fixture
def indexer(tmp_path: Path) -> FileIndexer:
    """Return the file indexer of a base that authors distinctive chunk settings."""
    return _indexer(tmp_path)


def _distinct_text(length: int) -> str:
    """Return whitespace-free text whose every position is identifiable.

    No whitespace means chunk boundaries land exactly on the size, so the
    emitted chunks pin ``chunk_size``, and the repeating alphabet makes the
    characters one chunk shares with the next pin ``overlap``.
    """
    return "".join(chr(ord("a") + index % 26) for index in range(length))


def _assert_carries_base_chunking(reader: Reader) -> None:
    """Assert every field of ``reader`` that can move a chunk boundary."""
    strategy = reader.chunking_strategy
    assert type(strategy) is SafeFixedSizeChunking
    assert strategy.chunk_size == _CHUNK_SIZE
    assert strategy.overlap == _CHUNK_OVERLAP
    assert strategy.min_chunk_fill_ratio == 0.5
    assert reader.chunk_size == _CHUNK_SIZE
    # None is how these readers spell UTF-8 (``self.encoding or "utf-8"``).
    assert reader.encoding is None


def test_two_bases_neither_share_a_reader_nor_poison_the_factory_cache(tmp_path: Path) -> None:
    """Configuring a reader must leave every other caller's reader as it was.

    ``_build_reader`` configures the factory's reader in place. Were that
    instance shared, two bases would get the same object: whichever refreshed
    second would re-chunk the other's corpus at its own size, and the factory
    would keep serving that size for the rest of the process.
    """
    cached = ReaderFactory.get_reader_for_extension(".md")
    cached_strategy = cached.chunking_strategy
    cached_chunk_size = cached.chunk_size

    small = _indexer(tmp_path / "small", chunk_size=137, chunk_overlap=11)._build_reader(Path("a.md"))
    large = _indexer(tmp_path / "large", chunk_size=2000, chunk_overlap=0)._build_reader(Path("b.md"))

    assert small is not large
    assert (small.chunk_size, large.chunk_size) == (137, 2000)
    assert small.chunking_strategy is not large.chunking_strategy
    # The shared cache entry must be exactly as it was found.
    assert cached is not small
    assert cached is not large
    assert cached.chunking_strategy is cached_strategy
    assert cached.chunk_size == cached_chunk_size


def test_the_json_reader_does_not_borrow_the_cached_chunking_strategy(indexer: FileIndexer) -> None:
    """The JSON subclass must not share its chunker with another caller's reader.

    Nothing mutates a JSON reader's strategy today, so this pins the same
    no-sharing rule one branch over rather than a present defect: a shared
    strategy would put one in-place edit between every base that reads JSON.
    """
    cached = ReaderFactory.get_reader_for_extension(".json")

    reader = indexer._build_reader(Path("source.json"))

    assert reader is not cached
    assert reader.chunking_strategy is not cached.chunking_strategy


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
    tmp_path: Path,
    indexer: FileIndexer,
) -> None:
    """JSON keeps 5000/0 fixed-size split, and its chunks collapse Unicode whitespace as legacy indexing did."""
    source = tmp_path / "source.json"
    source.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    documents = indexer._build_reader(source).read(source, name=source.name)

    assert [document.content for document in documents] == expected


@pytest.mark.parametrize(
    "suffix",
    [
        ".pdf",
        pytest.param(
            ".docx",
            marks=pytest.mark.skipif(not _package_available("docx"), reason="python-docx not installed"),
        ),
        # MindRoom ships no python-pptx, so its indexer skips PPTX files unless a deployment installs it.
        pytest.param(
            ".pptx",
            marks=pytest.mark.skipif(not _package_available("pptx"), reason="python-pptx not installed"),
        ),
    ],
)
def test_document_readers_chunk_paragraphs_as_agno_3_0_did(suffix: str, indexer: FileIndexer) -> None:
    """PDF, DOCX, and PPTX keep paragraph chunking at 5000 chars and collapse intra-paragraph whitespace."""
    paragraphs = ["\n".join(f"Paragraph {n}, line {i}:\tsome  words\there." for i in range(70)) for n in range(3)]
    reader = indexer._build_reader(Path(f"source{suffix}"))
    strategy = reader.chunking_strategy

    assert isinstance(strategy, LegacyDocumentChunking)
    assert strategy.chunk_size == 5000
    chunks = strategy.chunk(Document(name="source", content="\n\n".join(paragraphs)))
    # One chunk per 2719-character paragraph.
    assert [chunk.content for chunk in chunks] == [
        " ".join(f"Paragraph {n}, line {i}: some words here." for i in range(70)) for n in range(3)
    ]


@pytest.mark.parametrize("suffix", _CHUNKED_SUFFIXES)
def test_text_readers_carry_this_bases_chunking(suffix: str, indexer: FileIndexer) -> None:
    """A text-like reader must chunk by the base's policy, not the factory's 5000/0."""
    reader = indexer._build_reader(Path(f"source{suffix}"))

    assert isinstance(reader, (TextReader, MarkdownReader))
    _assert_carries_base_chunking(reader)


def test_text_reader_splits_a_file_on_the_bases_boundaries(tmp_path: Path, indexer: FileIndexer) -> None:
    """Observe the boundaries a real read produces rather than trusting attributes.

    Both settings are visible in the output: chunk length is capped at
    ``chunk_size``, and each chunk after the first opens with the ``overlap``
    characters that closed its predecessor.
    """
    source = tmp_path / "notes.txt"
    source.write_text(_distinct_text(400), encoding="utf-8")
    reader = indexer._build_reader(source)

    chunks = [document.content for document in reader.read(source, name="notes")]

    assert [len(chunk) for chunk in chunks] == [137, 137, 137, 22]
    for earlier, later in pairwise(chunks):
        assert later[:_CHUNK_OVERLAP] == earlier[-_CHUNK_OVERLAP:]


def test_a_second_base_moves_the_reader_and_its_boundaries(tmp_path: Path) -> None:
    """A different authored config must reach the reader and change where it splits.

    Every other case here uses one chunk size, which cannot tell a value read
    from config apart from a constant that happens to equal it.
    """
    source = tmp_path / "notes.txt"
    source.write_text(_distinct_text(1200), encoding="utf-8")
    reader = _indexer(tmp_path / "base", chunk_size=512, chunk_overlap=64)._build_reader(source)

    assert reader.chunk_size == 512

    chunks = [document.content for document in reader.read(source, name="notes")]

    assert [len(chunk) for chunk in chunks] == [512, 512, 304]
    for earlier, later in pairwise(chunks):
        assert later[:64] == earlier[-64:]


@pytest.mark.parametrize("suffix", _UNTOUCHED_SUFFIXES)
def test_non_text_readers_keep_their_factory_chunking(suffix: str, indexer: FileIndexer) -> None:
    """Row and document readers own their splitting; MindRoom must not overwrite it."""
    reader = indexer._build_reader(Path(f"source{suffix}"))

    assert not isinstance(reader.chunking_strategy, SafeFixedSizeChunking)


def test_json_keeps_structured_chunking_and_tags_its_parse_failures(
    tmp_path: Path,
    indexer: FileIndexer,
) -> None:
    """JSON must be read as JSON, and a parse failure must carry its source text.

    Taking MindRoom's text chunking here would split structured documents by
    size, and losing the tagging drops the malformed-source fallback entirely.
    """
    reader = indexer._build_reader(Path("source.json"))

    assert type(reader.chunking_strategy) is LegacyFixedSizeChunking
    assert (reader.chunking_strategy.chunk_size, reader.chunking_strategy.overlap) == (5000, 0)

    # The broken key is indented so line and column differ; equal values would
    # make the assertion below blind to the two being swapped.
    malformed = tmp_path / "claim.json"
    malformed.write_text('{\n  "claim": "kept",\n     “broken”: true\n}\n', encoding="utf-8")
    with pytest.raises(_MalformedJSONSourceError) as raised:
        reader.read(malformed)
    assert raised.value.source_text.startswith('{\n  "claim": "kept"')
    assert (raised.value.line, raised.value.column) == (3, 6)

    # The other direction: tagging must be reached only by a parse failure, or
    # every JSON file in the corpus would divert to the text fallback. The
    # caller's name has to reach every document too: search results report it
    # as the document name, so dropping it renames every chunk of the file.
    valid = tmp_path / "valid.json"
    valid.write_text('[{"claim": "one"}, {"claim": "two"}]', encoding="utf-8")
    documents = reader.read(valid, name="valid.json")
    assert [document.content for document in documents] == ['{"claim": "one"}', '{"claim": "two"}']
    assert {document.name for document in documents} == {"valid.json"}


def test_json_elements_above_the_factory_chunk_size_are_still_split(
    tmp_path: Path,
    indexer: FileIndexer,
) -> None:
    """JSON keeps the factory's 5000-character chunking, so a larger value must split."""
    source = tmp_path / "big.json"
    source.write_text(json.dumps({"claim": "x" * 6013}), encoding="utf-8")
    reader = indexer._build_reader(source)

    documents = reader.read(source)

    assert len(documents) > 1
    assert max(len(document.content) for document in documents) <= 5000


def test_malformed_json_fallback_reader_chunks_like_this_bases_text(indexer: FileIndexer) -> None:
    """Text served from a failed parse is chunked like any other text of this base.

    Serving it unchunked would turn one long malformed file into a single
    oversized vector, which is the failure the fallback exists to avoid.
    """
    reader = indexer._configure_text_reader(_InMemoryTextReader(_distinct_text(400)))
    _assert_carries_base_chunking(reader)

    chunks = [document.content for document in reader.read(Path("claim.json"), name="claim")]

    assert [len(chunk) for chunk in chunks] == [137, 137, 137, 22]


def test_fallback_documents_keep_the_callers_name(indexer: FileIndexer) -> None:
    """The name the caller passed must reach every chunk of the fallback read.

    Search results report it as the document name, so a fallback that
    substituted its own would show the file's chunks under the wrong name.
    """
    reader = indexer._configure_text_reader(_InMemoryTextReader("retained source text"))

    documents = reader.read(Path("a.json"), name="a")

    assert {document.name for document in documents} == {"a"}


@pytest.mark.parametrize("kind", ["pdf", "docx", "json"])
def test_exact_indexed_chunk_output_for_pdf_docx_and_json(
    kind: str,
    tmp_path: Path,
    indexer: FileIndexer,
) -> None:
    """Pin exact chunk text, name, and metadata across PDF, DOCX, and JSON indexing paths."""
    if kind == "pdf":
        source = tmp_path / "report.pdf"
        _write_text_pdf(source, ["1\nFirst line\nwrapped", "2\nSecond page text"])
        docs = indexer._build_reader(source).read(source, name=source.name)
        assert [(doc.name, doc.meta_data, doc.content) for doc in docs] == [
            ("report.pdf", {"page": 1}, "<start page 1>\nFirst line wrapped\n<end page 1>"),
            ("report.pdf", {"page": 2}, "<start page 2>\nSecond page text\n<end page 2>"),
        ]
        return

    if kind == "docx":
        docx = pytest.importorskip("docx")
        source = tmp_path / "spec.docx"
        para_a = " ".join(f"alpha{i}" for i in range(420))
        para_b = " ".join(f"beta{i}" for i in range(420))
        document = docx.Document()
        document.add_paragraph(para_a.replace(" ", "\t", 1))
        document.add_paragraph(para_b)
        document.save(source)
        docs = indexer._build_reader(source).read(source, name=source.name)
        assert [(doc.name, doc.meta_data, doc.content) for doc in docs] == [
            ("spec.docx", {"chunk": 1, "chunk_size": len(para_a)}, para_a),
            ("spec.docx", {"chunk": 2, "chunk_size": len(para_b)}, para_b),
        ]
        return

    source = tmp_path / "items.json"
    source.write_text(json.dumps([{"k": "a\u00a0b"}, {"k": "c"}], ensure_ascii=False), encoding="utf-8")
    docs = indexer._build_reader(source).read(source, name=source.name)
    assert [(doc.name, doc.meta_data, doc.content) for doc in docs] == [
        ("items.json", {"page": 1, "chunk": 1, "chunk_size": 12}, '{"k": "a b"}'),
        ("items.json", {"page": 2, "chunk": 1, "chunk_size": 10}, '{"k": "c"}'),
    ]


def test_document_to_dict_matches_serialized_contract() -> None:
    """``Document.to_dict`` omits ``None`` names while always keeping ``content`` and ``meta_data``."""
    assert Document(content="hello").to_dict() == {"meta_data": {}, "content": "hello"}
    assert Document(name="page", meta_data={"url": "https://example.test"}, content="body").to_dict() == {
        "name": "page",
        "meta_data": {"url": "https://example.test"},
        "content": "body",
    }


def test_fixed_size_chunking_preserves_line_breaks_and_validates_overlap() -> None:
    """Fixed-size chunking validates overlap, aligns to spaces, and preserves line breaks for website pages."""
    with pytest.raises(ValueError, match="overlap"):
        FixedSizeChunking(chunk_size=10, overlap=10)

    strategy = FixedSizeChunking(chunk_size=12, overlap=3)
    assert strategy.chunk(Document(content="")) == []

    chunks = strategy.chunk(Document(content="alpha  beta\n\ngamma delta"))
    assert [(chunk.content, chunk.meta_data) for chunk in chunks] == [
        ("alpha beta", {"chunk": 1, "chunk_size": 10}),
        ("eta\ngamma", {"chunk": 2, "chunk_size": 9}),
        ("mma delta", {"chunk": 3, "chunk_size": 9}),
    ]


def test_document_chunking_splits_oversized_paragraphs_by_sentence() -> None:
    """``DocumentChunking`` keeps short text intact and splits oversized paragraphs on sentence boundaries."""
    strategy = DocumentChunking(chunk_size=40)
    short = Document(name="short", content="Fits in one chunk.")
    assert strategy.chunk(short) == [short]

    long_para = "First sentence here. Second sentence here. Third sentence here."
    chunks = strategy.chunk(Document(name="doc", content=f"Intro para.\n\n{long_para}"))
    assert [(chunk.content, chunk.meta_data) for chunk in chunks] == [
        ("Intro para.", {"chunk": 1, "chunk_size": 11}),
        ("First sentence here.", {"chunk": 2, "chunk_size": 20}),
        ("Second sentence here.", {"chunk": 3, "chunk_size": 21}),
        ("Third sentence here.", {"chunk": 4, "chunk_size": 20}),
    ]


def test_row_chunking_skips_blank_rows_without_shifting_row_numbers() -> None:
    """``RowChunking`` normalizes whitespace and preserves 1-based logical row numbers across blank lines."""
    doc = Document(name="data", content="h1, h2\n  a ,  1 \n\nb, 2\n")
    chunks = RowChunking().chunk(doc)
    assert [(c.meta_data["row_number"], c.content) for c in chunks] == [
        (1, "h1, h2"),
        (2, "a , 1"),
        (4, "b, 2"),
    ]


def test_csv_reader_normalizes_multiline_cells(tmp_path: Path) -> None:
    """``CSVReader`` flattens newlines inside quoted cells and chunks rows cleanly."""
    source = tmp_path / "notes.csv"
    source.write_text('name,notes\n"Alice","line1\nline2"\n\nBob,42\n', encoding="utf-8")

    docs = CSVReader().read(source, name="notes.csv")
    assert [(d.name, d.meta_data["row_number"], d.content) for d in docs] == [
        ("notes.csv", 1, "name, notes"),
        ("notes.csv", 2, "Alice, line1 line2"),
        ("notes.csv", 4, "Bob, 42"),
    ]


def test_excel_reader_reads_xlsx_sheets(tmp_path: Path) -> None:
    """``ExcelReader`` normalizes cell types, trims trailing empties, and skips empty sheets."""
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

    docs = ExcelReader().read(source, name="metrics.xlsx")
    assert [(d.name, d.meta_data["sheet_name"], d.meta_data["sheet_index"], d.content) for d in docs] == [
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
    docs = ExcelReader().read(source, name="legacy.xls")

    assert [(d.name, d.meta_data["sheet_name"], d.meta_data["sheet_index"], d.content) for d in docs] == [
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
    """``DocxReader`` joins document paragraphs with blank lines."""
    docx = pytest.importorskip("docx")
    source = tmp_path / "spec.docx"
    document = docx.Document()
    document.add_paragraph("First paragraph.")
    document.add_paragraph("Second paragraph.")
    document.save(source)

    docs = DocxReader().read(source, name="spec.docx")
    assert [(d.name, d.content) for d in docs] == [("spec.docx", "First paragraph.\n\nSecond paragraph.")]


def test_pdf_reader_helpers_and_encryption_handling(tmp_path: Path) -> None:
    """``PDFReader`` sanitizes wrapped lines, cleans page numbers, and attempts blank-password decryption."""
    assert _sanitize_pdf_text("word1\nword2\n\npara2\nline2") == "word1 word2\n\npara2 line2"
    cleaned, shift = _clean_page_numbers(["1\nFirst", "2\nSecond", "3\nThird"])
    assert shift == 1
    assert cleaned == [
        "<start page 1>\nFirst\n<end page 1>",
        "<start page 2>\nSecond\n<end page 2>",
        "<start page 3>\nThird\n<end page 3>",
    ]

    locked = tmp_path / "locked.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("open-sesame")
    with locked.open("wb") as handle:
        writer.write(handle)
    assert PDFReader().read(locked) == []

    blank_encrypted = tmp_path / "restricted.pdf"
    blank_writer = PdfWriter()
    blank_writer.add_blank_page(width=200, height=200)
    owner_code = "owner-only"
    blank_writer.encrypt("", owner_code)
    with blank_encrypted.open("wb") as handle:
        blank_writer.write(handle)
    unlocked = PDFReader().read(blank_encrypted, name="restricted.pdf")
    assert [(d.name, d.meta_data, d.content) for d in unlocked] == [("restricted.pdf", {"page": 1}, "")]


def test_pptx_reader_extracts_grouped_shapes_and_reports_missing_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PPTXReader`` extracts text recursively through group shapes and fails cleanly when ``pptx`` is absent."""
    monkeypatch.setitem(sys.modules, "pptx", None)
    with pytest.raises(ImportError, match="python-pptx"):
        ReaderFactory.get_reader_for_extension(".pptx")

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
    docs = PPTXReader().read(deck, name="deck.pptx")
    assert [(d.name, d.content) for d in docs] == [
        ("deck.pptx", "Slide 1:\nTitle\nGrouped\n\nSlide 2:\n(No text content)"),
    ]


@pytest.mark.parametrize(
    ("extension", "package"),
    [
        (".pdf", "pypdf"),
        (".docx", "docx"),
        (".xlsx", "openpyxl"),
        (".xls", "xlrd"),
        (".pptx", "pptx"),
    ],
)
def test_reader_factory_raises_import_error_only_when_format_dependency_is_missing(
    extension: str,
    package: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing optional format dependencies raise ``ImportError`` when requesting that format's reader."""
    monkeypatch.setitem(sys.modules, package, None)
    with pytest.raises(ImportError):
        ReaderFactory.get_reader_for_extension(extension)
