"""MindRoom-owned file readers and factory for knowledge indexing."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import re
import sys
import uuid
from datetime import date, datetime
from importlib.util import find_spec
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, ClassVar, cast

from mindroom.knowledge.chunking import (
    ChunkingStrategy,
    Document,
    DocumentChunking,
    FixedSizeChunking,
    RowChunking,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from pypdf import PdfReader as PyPdfReader

logger = get_logger(__name__)

_PAGE_START_NUMBERING_FORMAT_DEFAULT = "<start page {page_nr}>"
_PAGE_END_NUMBERING_FORMAT_DEFAULT = "<end page {page_nr}>"
_PAGE_NUMBERING_CORRECTNESS_RATIO = 0.4
_PAGE_NUMBER_RE = re.compile(r"^\s*(\d+)\s*|\s*(\d+)\s*$")


def _require_package(package: str, message: str) -> None:
    """Raise ``ImportError`` when optional reader dependency ``package`` is unavailable."""
    if package in sys.modules:
        if sys.modules[package] is None:
            raise ImportError(message)
        return
    try:
        spec = find_spec(package)
    except ValueError as exc:
        raise ImportError(message) from exc
    if spec is None:
        raise ImportError(message)


def _ensure_exists(path: Path) -> Path:
    if not path.exists():
        msg = f"Could not find file: {path}"
        raise FileNotFoundError(msg)
    return path


def _stream_stem(stream: IO[Any], default: str) -> str:
    stream_name = getattr(stream, "name", None)
    return stream_name.split(".")[0] if isinstance(stream_name, str) else default


def _read_stream_text(stream: IO[Any], encoding: str) -> str:
    stream.seek(0)
    raw = stream.read()
    return raw.decode(encoding) if isinstance(raw, bytes) else raw


def _read_stream_bytes(stream: IO[Any]) -> bytes:
    stream.seek(0)
    raw = stream.read()
    return raw.encode("utf-8", errors="replace") if isinstance(raw, str) else raw


def _fixed_size_default(chunk_size: int) -> ChunkingStrategy:
    return FixedSizeChunking(chunk_size=chunk_size)


def _document_default(chunk_size: int) -> ChunkingStrategy:
    return DocumentChunking(chunk_size=chunk_size)


def _row_default(_chunk_size: int) -> ChunkingStrategy:
    return RowChunking()


class Reader:
    """Base class for file readers that emit chunked ``Document`` lists."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy] | None] = None
    _required_package: ClassVar[tuple[str, str] | None] = None

    def __init__(
        self,
        chunk: bool = True,
        chunk_size: int = 5000,
        chunking_strategy: ChunkingStrategy | None = None,
        name: str | None = None,
        description: str | None = None,
        encoding: str | None = None,
    ) -> None:
        required = type(self)._required_package
        if required is not None:
            _require_package(*required)
        self.chunk = chunk
        self.chunk_size = chunk_size
        default_strategy = type(self)._default_strategy
        self.chunking_strategy = (
            default_strategy(chunk_size)
            if chunking_strategy is None and default_strategy is not None
            else chunking_strategy
        )
        self.name = name
        self.description = description
        self.encoding = encoding

    def read(self, source: Path | IO[Any], /, name: str | None = None) -> list[Document]:
        """Read ``source`` and return its documents."""
        raise NotImplementedError

    def chunk_document(self, document: Document) -> list[Document]:
        """Apply ``self.chunking_strategy`` (defaulting to ``FixedSizeChunking``) to ``document``."""
        if self.chunking_strategy is None:
            self.chunking_strategy = FixedSizeChunking(chunk_size=self.chunk_size)
        return self.chunking_strategy.chunk(document)

    def _chunk_documents(self, documents: list[Document]) -> list[Document]:
        if not self.chunk:
            return documents
        return [chunk for document in documents for chunk in self.chunk_document(document)]

    def _read_plain_text(
        self,
        file: Path | IO[Any],
        *,
        name: str | None,
        default_stream_name: str,
    ) -> list[Document]:
        try:
            encoding = self.encoding or "utf-8"
            if isinstance(file, Path):
                file_name = name or _ensure_exists(file).stem
                file_contents = file.read_text(encoding=encoding)
            else:
                file_name = name or _stream_stem(file, default_stream_name)
                file_contents = _read_stream_text(file, encoding)
            return self._chunk_documents([Document(name=file_name, id=str(uuid.uuid4()), content=file_contents)])
        except Exception as exc:
            logger.error("Failed reading text file", file=str(file), error=str(exc))  # noqa: TRY400
            return []


class TextReader(Reader):
    """Reader for plain text files."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy] | None] = _fixed_size_default

    def read(self, file: Path | IO[Any], name: str | None = None) -> list[Document]:
        """Read a text file or stream into chunked documents."""
        return self._read_plain_text(file, name=name, default_stream_name="text_file")


class MarkdownReader(Reader):
    """Reader for Markdown files."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy] | None] = _fixed_size_default

    def read(self, file: Path | IO[Any], name: str | None = None) -> list[Document]:
        """Read a Markdown file or stream into chunked documents."""
        return self._read_plain_text(file, name=name, default_stream_name="file")


class JSONReader(Reader):
    """Reader for JSON files, emitting one document per top-level element."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy] | None] = _fixed_size_default

    def read(self, path: Path | IO[Any], name: str | None = None) -> list[Document]:
        """Read and parse JSON, letting decode errors propagate to callers."""
        if isinstance(path, Path):
            json_name = name or _ensure_exists(path).stem
            json_contents = json.loads(path.read_text(encoding=self.encoding or "utf-8"))
        elif hasattr(path, "seek") and hasattr(path, "read"):
            json_name = name or _stream_stem(path, "json_file")
            path.seek(0)
            json_contents = json.load(path)
        else:
            msg = "Unsupported file type. Must be Path or file-like object."
            raise ValueError(msg)

        items = json_contents if isinstance(json_contents, list) else [json_contents]
        documents = [
            Document(
                name=json_name,
                id=str(uuid.uuid4()),
                meta_data={"page": page_number},
                content=json.dumps(item, ensure_ascii=False),
            )
            for page_number, item in enumerate(items, start=1)
        ]
        return self._chunk_documents(documents)


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
                    id=str(uuid.uuid4()),
                    meta_data={"sheet_name": sheet_name, "sheet_index": sheet_index},
                    content="\n".join(lines),
                ),
            )
    return documents


class CSVReader(Reader):
    """Reader for CSV files using row-based chunking by default."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy] | None] = _row_default

    def read(
        self,
        file: Path | str | IO[Any],
        name: str | None = None,
        *,
        delimiter: str = ",",
        quotechar: str = '"',
    ) -> list[Document]:
        """Read a CSV file or stream and return row-chunked documents."""
        encoding = self.encoding or "utf-8"
        if isinstance(file, Path | str):
            file_path = _ensure_exists(Path(file))
            csv_name = name or file_path.stem
            stream: io.TextIOBase = file_path.open(newline="", mode="r", encoding=encoding)
        else:
            csv_name = name or _stream_stem(file, "csv_file")
            try:
                stream = io.StringIO(_read_stream_text(file, encoding), newline="")
            except Exception as exc:
                logger.error("Failed reading CSV file", file=str(file), error=str(exc))  # noqa: TRY400
                return []

        try:
            with stream as csvfile:
                csv_lines = [
                    ", ".join(_stringify_cell_value(cell) for cell in row)
                    for row in csv.reader(csvfile, delimiter=delimiter, quotechar=quotechar)
                ]
            return self._chunk_documents([Document(name=csv_name, id=str(uuid.uuid4()), content="\n".join(csv_lines))])
        except Exception as exc:
            logger.error("Failed reading CSV file", file=str(file), error=str(exc))  # noqa: TRY400
            return []


class ExcelReader(Reader):
    """Reader for Excel workbooks (``.xlsx`` and ``.xls``) using row-based chunking."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy] | None] = _row_default

    def __init__(
        self,
        sheets: list[str | int] | None = None,
        chunk: bool = True,
        chunk_size: int = 5000,
        chunking_strategy: ChunkingStrategy | None = None,
        name: str | None = None,
        description: str | None = None,
        encoding: str | None = None,
    ) -> None:
        super().__init__(
            chunk=chunk,
            chunk_size=chunk_size,
            chunking_strategy=chunking_strategy,
            name=name,
            description=description,
            encoding=encoding,
        )
        self.sheets = sheets

    def _should_include_sheet(self, sheet_name: str, sheet_index: int) -> bool:
        if not self.sheets:
            return True
        return any(
            (isinstance(entry, int) and not isinstance(entry, bool) and sheet_index == entry)
            or (isinstance(entry, str) and sheet_name.lower() == entry.lower())
            for entry in self.sheets
        )

    def _read_xlsx(self, file: Path | IO[Any], *, workbook_name: str) -> list[Document]:
        try:
            import openpyxl  # noqa: PLC0415
        except ImportError as exc:
            msg = "`openpyxl` not installed. Please install it via `pip install openpyxl`."
            raise ImportError(msg) from exc

        source = str(file) if isinstance(file, Path) else io.BytesIO(_read_stream_bytes(file))
        workbook = openpyxl.load_workbook(filename=source, read_only=True, data_only=True)
        try:
            sheets = [
                (worksheet.title, index, worksheet.iter_rows(values_only=True))
                for index, worksheet in enumerate(workbook.worksheets, start=1)
                if self._should_include_sheet(worksheet.title, index)
            ]
            return _excel_rows_to_documents(workbook_name=workbook_name, sheets=sheets)
        finally:
            workbook.close()

    def _read_xls(self, file: Path | IO[Any], *, workbook_name: str) -> list[Document]:
        try:
            import xlrd  # noqa: PLC0415
        except ImportError as exc:
            msg = "`xlrd` not installed. Please install it via `pip install xlrd`."
            raise ImportError(msg) from exc

        workbook = (
            xlrd.open_workbook(filename=str(file), encoding_override=self.encoding)
            if isinstance(file, Path)
            else xlrd.open_workbook(file_contents=_read_stream_bytes(file), encoding_override=self.encoding)
        )

        def _convert_cell(value: object, cell_type: int) -> object:
            if cell_type == xlrd.XL_CELL_DATE:
                with contextlib.suppress(Exception):
                    return datetime(*xlrd.xldate_as_tuple(value, workbook.datemode))  # noqa: DTZ001
                return value
            return bool(value) if cell_type == xlrd.XL_CELL_BOOLEAN else value

        sheets: list[tuple[str, int, Iterable[Sequence[object]]]] = []
        for index in range(workbook.nsheets):
            sheet = workbook.sheet_by_index(index)
            if not self._should_include_sheet(sheet.name, index + 1):
                continue
            rows = [
                [_convert_cell(sheet.cell_value(r, c), sheet.cell_type(r, c)) for c in range(sheet.ncols)]
                for r in range(sheet.nrows)
            ]
            sheets.append((sheet.name, index + 1, rows))
        return _excel_rows_to_documents(workbook_name=workbook_name, sheets=sheets)

    def read(self, file: Path | IO[Any], name: str | None = None) -> list[Document]:
        """Read an Excel workbook and return row-chunked sheet documents."""
        if isinstance(file, Path):
            _ensure_exists(file)
            extension = file.suffix.lower()
            workbook_name = Path(name).stem if name else file.stem
        else:
            file_name = getattr(file, "name", None)
            has_name = isinstance(file_name, str) and bool(file_name)
            extension = Path(file_name).suffix.lower() if has_name else (Path(name).suffix.lower() if name else "")
            workbook_name = Path(name).stem if name else (Path(file_name).stem if has_name else "workbook")

        if extension not in (".xlsx", ".xls"):
            msg = f"Unsupported file extension: '{extension}'. Expected .xlsx or .xls"
            raise ValueError(msg)

        try:
            documents = (
                self._read_xlsx(file, workbook_name=workbook_name)
                if extension == ".xlsx"
                else self._read_xls(file, workbook_name=workbook_name)
            )
            return self._chunk_documents(documents)
        except ImportError:
            raise
        except Exception as exc:
            logger.error("Failed reading Excel file", file=str(file), error=str(exc))  # noqa: TRY400
            return []


def _sanitize_pdf_text(text: str) -> str:
    text = re.sub(r"\n[ \t]+\n", " ", text)
    text = re.sub(r"(?<!\n)[ \t]*\n[ \t]*(?!\n)", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _clean_page_numbers(
    page_content_list: list[str],
    *,
    page_start_numbering_format: str = _PAGE_START_NUMBERING_FORMAT_DEFAULT,
    page_end_numbering_format: str = _PAGE_END_NUMBERING_FORMAT_DEFAULT,
) -> tuple[list[str], int | None]:
    page_numbers = [
        int(match.group(1) or match.group(2)) if (match := _PAGE_NUMBER_RE.search(content)) else None
        for content in page_content_list
    ]
    if all(number is None or number > 5 for number in page_numbers) or sum(n is not None for n in page_numbers) < 2:
        return page_content_list, None

    best_match: list[int] | None = None
    best_shift: int | None = None
    best_correct_count = 0
    for shift in (-2, -1, 0, 1, 2):
        expected_numbers = [index + shift for index in range(len(page_numbers))]
        correct_count = sum(
            1
            for actual, expected in zip(page_numbers, expected_numbers)
            if actual == expected or str(actual).startswith(str(expected)) or str(actual).endswith(str(expected))
        )
        if correct_count > best_correct_count:
            best_correct_count = correct_count
            best_match = expected_numbers
            best_shift = shift

    if not best_match or best_correct_count / len(page_numbers) < _PAGE_NUMBERING_CORRECTNESS_RATIO:
        return page_content_list, None

    cleaned_pages: list[str] = []
    for content, expected_number in zip(page_content_list, best_match):
        body = re.sub(rf"^\s*{expected_number}\s*|\s*{expected_number}\s*$", "", content)
        start_tag = (
            f"{page_start_numbering_format.format(page_nr=expected_number)}\n" if page_start_numbering_format else ""
        )
        end_tag = f"\n{page_end_numbering_format.format(page_nr=expected_number)}" if page_end_numbering_format else ""
        cleaned_pages.append(start_tag + body + end_tag)
    return cleaned_pages, best_shift


class PDFReader(Reader):
    """Reader for PDF files using paragraph-based chunking."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy] | None] = _document_default
    _required_package: ClassVar[tuple[str, str] | None] = (
        "pypdf",
        "`pypdf` not installed. Please install it via `pip install pypdf`.",
    )

    def __init__(
        self,
        split_on_pages: bool = True,
        page_start_numbering_format: str | None = None,
        page_end_numbering_format: str | None = None,
        password: str | None = None,
        sanitize_content: bool = True,
        chunk: bool = True,
        chunk_size: int = 5000,
        chunking_strategy: ChunkingStrategy | None = None,
        name: str | None = None,
        description: str | None = None,
        encoding: str | None = None,
    ) -> None:
        self.split_on_pages = split_on_pages
        self.page_start_numbering_format = (
            _PAGE_START_NUMBERING_FORMAT_DEFAULT if page_start_numbering_format is None else page_start_numbering_format
        )
        self.page_end_numbering_format = (
            _PAGE_END_NUMBERING_FORMAT_DEFAULT if page_end_numbering_format is None else page_end_numbering_format
        )
        self.password = password
        self.sanitize_content = sanitize_content
        super().__init__(
            chunk=chunk,
            chunk_size=chunk_size,
            chunking_strategy=chunking_strategy,
            name=name,
            description=description,
            encoding=encoding,
        )

    def _decrypt_pdf(self, doc_reader: PyPdfReader, doc_name: str, password: str | None = None) -> bool:
        if not doc_reader.is_encrypted:
            return True
        pdf_password = self.password if password is None else password
        if pdf_password is None:
            with contextlib.suppress(Exception):
                if doc_reader.decrypt(""):
                    return True
            logger.error("PDF file is password protected and no password was provided", document=doc_name)
            return False
        try:
            if doc_reader.decrypt(pdf_password):
                return True
            logger.error("Failed to decrypt PDF file: incorrect password", document=doc_name)
        except Exception as exc:
            logger.error("Error decrypting PDF file", document=doc_name, error=str(exc))  # noqa: TRY400
        return False

    def read(
        self,
        pdf: str | Path | IO[Any] | None = None,
        name: str | None = None,
        password: str | None = None,
    ) -> list[Document]:
        """Read a PDF file or stream and return chunked page documents."""
        if pdf is None:
            logger.error("No PDF source provided")
            return []

        from pypdf import PdfReader as PyPdfReader  # noqa: PLC0415
        from pypdf.errors import PdfStreamError  # noqa: PLC0415

        doc_name = name or (
            Path(pdf).stem.replace(" ", "_")
            if isinstance(pdf, str | Path)
            else _stream_stem(pdf, "pdf_file").replace(" ", "_")
        )
        try:
            pdf_reader = PyPdfReader(pdf)
        except PdfStreamError as exc:
            logger.error("Error reading PDF", document=doc_name, error=str(exc))  # noqa: TRY400
            return []

        if not self._decrypt_pdf(pdf_reader, doc_name, password):
            return []

        pdf_content = [page.extract_text() for page in pdf_reader.pages]
        if self.sanitize_content:
            pdf_content = [_sanitize_pdf_text(page) for page in pdf_content]
        pdf_content, shift = _clean_page_numbers(
            pdf_content,
            page_start_numbering_format=self.page_start_numbering_format,
            page_end_numbering_format=self.page_end_numbering_format,
        )

        if self.split_on_pages:
            start_page = shift if shift is not None else 1
            documents = [
                Document(name=doc_name, id=str(uuid.uuid4()), meta_data={"page": page_number}, content=page_content)
                for page_number, page_content in enumerate(pdf_content, start=start_page)
            ]
        else:
            documents = [Document(name=doc_name, id=str(uuid.uuid4()), meta_data={}, content="\n".join(pdf_content))]
        return self._chunk_documents(documents)


class DocxReader(Reader):
    """Reader for Microsoft Word ``.docx`` files."""

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy] | None] = _document_default
    _required_package: ClassVar[tuple[str, str] | None] = (
        "docx",
        "The `python-docx` package is not installed. Please install it via `pip install python-docx`.",
    )

    def read(self, file: Path | IO[Any], name: str | None = None) -> list[Document]:
        """Read a ``.docx`` file or stream into paragraph-chunked documents."""
        from docx import Document as DocxDocument  # noqa: PLC0415

        try:
            if isinstance(file, Path):
                docx_document = DocxDocument(str(_ensure_exists(file)))
                doc_name = name or file.stem
            else:
                docx_document = DocxDocument(file)
                doc_name = name or _stream_stem(file, "docx_file")

            doc_content = "\n\n".join(para.text for para in docx_document.paragraphs)
            return self._chunk_documents([Document(name=doc_name, id=str(uuid.uuid4()), content=doc_content)])
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

    _default_strategy: ClassVar[Callable[[int], ChunkingStrategy] | None] = _document_default
    _required_package: ClassVar[tuple[str, str] | None] = (
        "pptx",
        "The `python-pptx` package is not installed. Please install it via `pip install python-pptx`.",
    )

    def read(self, file: Path | IO[Any], name: str | None = None) -> list[Document]:
        """Read a ``.pptx`` file or stream into slide-ordered, paragraph-chunked documents."""
        from pptx import Presentation  # noqa: PLC0415  # ty: ignore[import-not-found]
        from pptx.shapes.group import GroupShape  # noqa: PLC0415  # ty: ignore[import-not-found]

        try:
            if isinstance(file, Path):
                presentation = Presentation(str(_ensure_exists(file)))
                doc_name = name or file.stem
            else:
                presentation = Presentation(file)
                doc_name = name or _stream_stem(file, "pptx_file")

            slide_texts = [
                f"Slide {slide_number}:\n"
                + ("\n".join(texts) if (texts := _shape_texts(slide.shapes, GroupShape)) else "(No text content)")
                for slide_number, slide in enumerate(presentation.slides, start=1)
            ]
            return self._chunk_documents(
                [Document(name=doc_name, id=str(uuid.uuid4()), content="\n\n".join(slide_texts))],
            )
        except Exception as exc:
            logger.error("Error reading PPTX file", file=str(file), error=str(exc))  # noqa: TRY400
            return []


def _build_xlsx_reader() -> ExcelReader:
    _require_package("openpyxl", "`openpyxl` not installed. Please install it via `pip install openpyxl`.")
    return ExcelReader(name="Excel Reader")


def _build_xls_reader() -> ExcelReader:
    _require_package("xlrd", "`xlrd` not installed. Please install it via `pip install xlrd`.")
    return ExcelReader(name="Excel Reader")


def _build_docx_reader() -> DocxReader:
    return DocxReader(name="Docx Reader")


class ReaderFactory:
    """Factory that builds a fresh format-specific ``Reader`` for each file extension."""

    _EXTENSION_READERS: ClassVar[dict[str, Callable[[], Reader]]] = {
        ".pdf": lambda: PDFReader(name="PDF Reader"),
        "application/pdf": lambda: PDFReader(name="PDF Reader"),
        ".csv": lambda: CSVReader(name="CSV Reader"),
        "text/csv": lambda: CSVReader(name="CSV Reader"),
        ".xlsx": _build_xlsx_reader,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": _build_xlsx_reader,
        ".xls": _build_xls_reader,
        "application/vnd.ms-excel": _build_xls_reader,
        ".docx": _build_docx_reader,
        ".doc": _build_docx_reader,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _build_docx_reader,
        ".pptx": lambda: PPTXReader(name="PPTX Reader"),
        ".json": lambda: JSONReader(name="JSON Reader"),
        ".md": lambda: MarkdownReader(name="Markdown Reader"),
        ".markdown": lambda: MarkdownReader(name="Markdown Reader"),
    }

    @classmethod
    def get_reader_for_extension(cls, extension: str) -> Reader:
        """Return a newly instantiated reader matching ``extension`` (or ``TextReader`` as fallback)."""
        builder = cls._EXTENSION_READERS.get(extension.lower())
        return builder() if builder is not None else TextReader(name="Text Reader")


__all__ = [
    "CSVReader",
    "ChunkingStrategy",
    "Document",
    "DocumentChunking",
    "DocxReader",
    "ExcelReader",
    "FixedSizeChunking",
    "JSONReader",
    "MarkdownReader",
    "PDFReader",
    "PPTXReader",
    "Reader",
    "ReaderFactory",
    "RowChunking",
    "TextReader",
]
