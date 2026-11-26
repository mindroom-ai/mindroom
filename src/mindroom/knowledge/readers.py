"""MindRoom-owned file readers and factory for knowledge indexing."""

from __future__ import annotations

import contextlib
import csv
import json
import re
import sys
from datetime import date, datetime
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast

from mindroom.knowledge.chunking import (
    ChunkingStrategy,
    Document,
    LegacyDocumentChunking,
    LegacyFixedSizeChunking,
    RowChunking,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

logger = get_logger(__name__)

_EXCEL_PACKAGES = {".xlsx": "openpyxl", ".xls": "xlrd"}
_PAGE_NUMBERING_CORRECTNESS_RATIO = 0.4
_PAGE_NUMBER_RE = re.compile(r"^\s*(\d+)\s*|\s*(\d+)\s*$")


def _require_package(package: str, pip_name: str | None = None) -> None:
    """Raise ``ImportError`` when optional reader dependency ``package`` is unavailable."""
    install_name = pip_name or package
    message = f"`{install_name}` is not installed. Please install it via `pip install {install_name}`."
    if package in sys.modules:
        if sys.modules[package] is None:
            raise ImportError(message)
        return
    try:
        if find_spec(package) is None:
            raise ImportError(message)
    except ValueError as exc:
        raise ImportError(message) from exc


def _ensure_exists(path: Path) -> Path:
    if not path.exists():
        msg = f"Could not find file: {path}"
        raise FileNotFoundError(msg)
    return path


def _row_default(_chunk_size: int) -> ChunkingStrategy:
    return RowChunking()


class Reader:
    """Base class for file readers that emit chunked ``Document`` lists."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy]] = LegacyFixedSizeChunking
    _required_package: ClassVar[tuple[str, str] | None] = None

    def __init__(
        self,
        chunk_size: int = 5000,
        chunking_strategy: ChunkingStrategy | None = None,
        encoding: str | None = None,
    ) -> None:
        if (required := type(self)._required_package) is not None:
            _require_package(*required)
        self.chunk_size = chunk_size
        self.chunking_strategy: ChunkingStrategy = (
            type(self)._default_strategy(chunk_size) if chunking_strategy is None else chunking_strategy
        )
        self.encoding = encoding

    def read(self, file: Path, name: str | None = None) -> list[Document]:
        """Read ``file`` and return its chunked documents."""
        raise NotImplementedError

    def chunk_document(self, document: Document) -> list[Document]:
        """Apply ``self.chunking_strategy`` to ``document``."""
        return self.chunking_strategy.chunk(document)

    def _chunk_documents(self, documents: list[Document]) -> list[Document]:
        return [chunk for document in documents for chunk in self.chunk_document(document)]


class TextReader(Reader):
    """Reader for plain text files."""

    def read(self, file: Path, name: str | None = None) -> list[Document]:
        """Read a text file into chunked documents."""
        try:
            _ensure_exists(file)
            contents = file.read_text(encoding=self.encoding or "utf-8")
            return self._chunk_documents([Document(name=name or file.stem, content=contents)])
        except Exception as exc:
            logger.error("Failed reading text file", file=str(file), error=str(exc))  # noqa: TRY400
            return []


class MarkdownReader(TextReader):
    """Reader for Markdown files."""


class JSONReader(Reader):
    """Reader for JSON files, emitting one document per top-level element."""

    def read(self, file: Path, name: str | None = None) -> list[Document]:
        """Read and parse JSON, letting decode errors propagate to callers."""
        _ensure_exists(file)
        contents = json.loads(file.read_text(encoding=self.encoding or "utf-8"))
        items = contents if isinstance(contents, list) else [contents]
        doc_name = name or file.stem
        return self._chunk_documents(
            [
                Document(name=doc_name, meta_data={"page": page}, content=json.dumps(item, ensure_ascii=False))
                for page, item in enumerate(items, start=1)
            ],
        )


def _stringify_cell_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _excel_rows_to_documents(
    *,
    workbook_name: str,
    sheets: Iterable[tuple[str, int, Iterable[Sequence[object]]]],
) -> list[Document]:
    documents: list[Document] = []
    for sheet_name, sheet_index, rows in sheets:
        lines: list[str] = []
        for row in rows:
            values = [_stringify_cell_value(value) for value in row]
            while values and values[-1] == "":
                values.pop()
            if line := ", ".join(values):
                lines.append(line)
        if lines:
            documents.append(
                Document(
                    name=workbook_name,
                    meta_data={"sheet_name": sheet_name, "sheet_index": sheet_index},
                    content="\n".join(lines),
                ),
            )
    return documents


class CSVReader(Reader):
    """Reader for CSV files using row-based chunking by default."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy]] = _row_default

    def read(self, file: Path, name: str | None = None) -> list[Document]:
        """Read a CSV file and return row-chunked documents."""
        _ensure_exists(file)
        try:
            with file.open(newline="", mode="r", encoding=self.encoding or "utf-8") as csvfile:
                csv_lines = [", ".join(_stringify_cell_value(cell) for cell in row) for row in csv.reader(csvfile)]
            return self._chunk_documents([Document(name=name or file.stem, content="\n".join(csv_lines))])
        except Exception as exc:
            logger.error("Failed reading CSV file", file=str(file), error=str(exc))  # noqa: TRY400
            return []


class ExcelReader(Reader):
    """Reader for Excel workbooks (``.xlsx`` and ``.xls``) using row-based chunking."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy]] = _row_default

    def _read_xlsx(self, file: Path, *, workbook_name: str) -> list[Document]:
        import openpyxl  # noqa: PLC0415

        workbook = openpyxl.load_workbook(filename=str(file), read_only=True, data_only=True)
        try:
            sheets = (
                (worksheet.title, index, worksheet.iter_rows(values_only=True))
                for index, worksheet in enumerate(workbook.worksheets, start=1)
            )
            return _excel_rows_to_documents(workbook_name=workbook_name, sheets=sheets)
        finally:
            workbook.close()

    def _read_xls(self, file: Path, *, workbook_name: str) -> list[Document]:
        import xlrd  # noqa: PLC0415

        workbook = xlrd.open_workbook(filename=str(file), encoding_override=self.encoding)

        def _convert_cell(value: object, cell_type: int) -> object:
            if cell_type == xlrd.XL_CELL_DATE:
                with contextlib.suppress(Exception):
                    return datetime(*xlrd.xldate_as_tuple(value, workbook.datemode))  # noqa: DTZ001
                return value
            return bool(value) if cell_type == xlrd.XL_CELL_BOOLEAN else value

        def _iter_sheets() -> Iterator[tuple[str, int, Iterator[list[object]]]]:
            for index in range(workbook.nsheets):
                sheet = workbook.sheet_by_index(index)
                rows = (
                    [_convert_cell(sheet.cell_value(r, c), sheet.cell_type(r, c)) for c in range(sheet.ncols)]
                    for r in range(sheet.nrows)
                )
                yield sheet.name, index + 1, rows

        return _excel_rows_to_documents(workbook_name=workbook_name, sheets=_iter_sheets())

    def read(self, file: Path, name: str | None = None) -> list[Document]:
        """Read an Excel workbook and return row-chunked sheet documents."""
        _ensure_exists(file)
        extension = file.suffix.lower()
        if extension not in _EXCEL_PACKAGES:
            msg = f"Unsupported file extension: '{extension}'. Expected .xlsx or .xls"
            raise ValueError(msg)
        _require_package(_EXCEL_PACKAGES[extension])
        workbook_name = Path(name).stem if name else file.stem
        try:
            reader = self._read_xlsx if extension == ".xlsx" else self._read_xls
            return self._chunk_documents(reader(file, workbook_name=workbook_name))
        except Exception as exc:
            logger.error("Failed reading Excel file", file=str(file), error=str(exc))  # noqa: TRY400
            return []


def _sanitize_pdf_text(text: str) -> str:
    text = re.sub(r"\n[ \t]+\n", " ", text)
    text = re.sub(r"(?<!\n)[ \t]*\n[ \t]*(?!\n)", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _clean_page_numbers(page_content_list: list[str]) -> tuple[list[str], int | None]:
    page_numbers = [
        int(match.group(1) or match.group(2)) if (match := _PAGE_NUMBER_RE.search(content)) else None
        for content in page_content_list
    ]
    if all(number is None or number > 5 for number in page_numbers) or sum(n is not None for n in page_numbers) < 2:
        return page_content_list, None

    best_count, best_shift, best_match = max(
        (
            (
                sum(
                    actual == expected or str(actual).startswith(str(expected)) or str(actual).endswith(str(expected))
                    for actual, expected in zip(page_numbers, expected_numbers)
                ),
                shift,
                expected_numbers,
            )
            for shift in (-2, -1, 0, 1, 2)
            for expected_numbers in ([index + shift for index in range(len(page_numbers))],)
        ),
        key=lambda item: item[0],
    )
    if best_count == 0 or best_count / len(page_numbers) < _PAGE_NUMBERING_CORRECTNESS_RATIO:
        return page_content_list, None

    cleaned_pages = [
        f"<start page {expected}>\n"
        + re.sub(rf"^\s*{expected}\s*|\s*{expected}\s*$", "", content)
        + f"\n<end page {expected}>"
        for content, expected in zip(page_content_list, best_match)
    ]
    return cleaned_pages, best_shift


class PDFReader(Reader):
    """Reader for PDF files using paragraph-based chunking."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy]] = LegacyDocumentChunking
    _required_package: ClassVar[tuple[str, str] | None] = ("pypdf", "pypdf")

    def read(self, file: Path, name: str | None = None) -> list[Document]:
        """Read a PDF file and return chunked page documents."""
        from pypdf import PdfReader as PyPdfReader  # noqa: PLC0415
        from pypdf.errors import PdfStreamError  # noqa: PLC0415

        doc_name = name or file.stem.replace(" ", "_")
        try:
            pdf_reader = PyPdfReader(file)
        except PdfStreamError as exc:
            logger.error("Error reading PDF", document=doc_name, error=str(exc))  # noqa: TRY400
            return []

        if pdf_reader.is_encrypted:
            decrypted = False
            with contextlib.suppress(Exception):
                decrypted = bool(pdf_reader.decrypt(""))
            if not decrypted:
                logger.error("PDF file is password protected and no password was provided", document=doc_name)
                return []

        pdf_content = [_sanitize_pdf_text(page.extract_text()) for page in pdf_reader.pages]
        pdf_content, shift = _clean_page_numbers(pdf_content)
        return self._chunk_documents(
            [
                Document(name=doc_name, meta_data={"page": page_number}, content=page_content)
                for page_number, page_content in enumerate(pdf_content, start=shift if shift is not None else 1)
            ],
        )


class DocxReader(Reader):
    """Reader for Microsoft Word ``.docx`` files."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy]] = LegacyDocumentChunking
    _required_package: ClassVar[tuple[str, str] | None] = ("docx", "python-docx")

    def read(self, file: Path, name: str | None = None) -> list[Document]:
        """Read a ``.docx`` file into paragraph-chunked documents."""
        from docx import Document as DocxDocument  # noqa: PLC0415

        try:
            docx_document = DocxDocument(str(_ensure_exists(file)))
            doc_content = "\n\n".join(para.text for para in docx_document.paragraphs)
            return self._chunk_documents([Document(name=name or file.stem, content=doc_content)])
        except Exception as exc:
            logger.error("Error reading DOCX file", file=str(file), error=str(exc))  # noqa: TRY400
            return []


def _shape_texts(shapes: Iterable[object], group_shape_type: type[object]) -> list[str]:
    texts: list[str] = []
    for shape in shapes:
        if isinstance(shape, group_shape_type):
            texts.extend(_shape_texts(cast("Any", shape).shapes, group_shape_type))
            continue
        text = getattr(shape, "text", "")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


class PPTXReader(Reader):
    """Reader for Microsoft PowerPoint ``.pptx`` presentations."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy]] = LegacyDocumentChunking
    _required_package: ClassVar[tuple[str, str] | None] = ("pptx", "python-pptx")

    def read(self, file: Path, name: str | None = None) -> list[Document]:
        """Read a ``.pptx`` file into slide-ordered, paragraph-chunked documents."""
        from pptx import Presentation  # noqa: PLC0415  # ty: ignore[import-not-found]
        from pptx.shapes.group import GroupShape  # noqa: PLC0415  # ty: ignore[import-not-found]

        try:
            presentation = Presentation(str(_ensure_exists(file)))
            slide_texts = [
                f"Slide {slide_number}:\n"
                + ("\n".join(texts) if (texts := _shape_texts(slide.shapes, GroupShape)) else "(No text content)")
                for slide_number, slide in enumerate(presentation.slides, start=1)
            ]
            return self._chunk_documents([Document(name=name or file.stem, content="\n\n".join(slide_texts))])
        except Exception as exc:
            logger.error("Error reading PPTX file", file=str(file), error=str(exc))  # noqa: TRY400
            return []


class ReaderFactory:
    """Factory that builds a fresh format-specific ``Reader`` for each file extension."""

    _EXTENSION_READERS: ClassVar[dict[str, type[Reader]]] = {
        ".pdf": PDFReader,
        ".csv": CSVReader,
        ".xlsx": ExcelReader,
        ".xls": ExcelReader,
        ".docx": DocxReader,
        ".doc": DocxReader,
        ".pptx": PPTXReader,
        ".json": JSONReader,
        ".md": MarkdownReader,
        ".markdown": MarkdownReader,
    }

    @classmethod
    def get_reader_for_extension(cls, extension: str) -> Reader:
        """Return a newly instantiated reader matching ``extension`` (or ``TextReader`` as fallback)."""
        ext = extension.lower()
        if required_package := _EXCEL_PACKAGES.get(ext):
            _require_package(required_package)
        return cls._EXTENSION_READERS.get(ext, TextReader)()


__all__ = [
    "CSVReader",
    "Document",
    "DocxReader",
    "ExcelReader",
    "JSONReader",
    "MarkdownReader",
    "PDFReader",
    "PPTXReader",
    "Reader",
    "ReaderFactory",
    "TextReader",
]
