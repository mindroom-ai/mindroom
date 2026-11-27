"""Stateless file extraction and chunking for knowledge indexing."""

from __future__ import annotations

import contextlib
import csv
import json
import re
import sys
from datetime import date, datetime
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.knowledge.chunking import KnowledgeChunk, chunk_fixed, chunk_paragraphs, chunk_rows, chunk_text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

_REQUIRED_PACKAGES: dict[str, tuple[str, str]] = {
    ".pdf": ("pypdf", "pypdf"),
    ".docx": ("docx", "python-docx"),
    ".doc": ("docx", "python-docx"),
    ".xlsx": ("openpyxl", "openpyxl"),
    ".xls": ("xlrd", "xlrd"),
    ".pptx": ("pptx", "python-pptx"),
}
_PAGE_NUMBERING_CORRECTNESS_RATIO = 0.4
_PAGE_NUMBER_RE = re.compile(r"^\s*(\d+)\s*|\s*(\d+)\s*$")
_LOCKED_PDF_MESSAGE = "PDF file is password protected and no password was provided"


class KnowledgeReadError(Exception):
    """Raised when a knowledge file cannot be decoded into indexable content."""


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


def ensure_reader_available(extension: str) -> None:
    """Raise ``ImportError`` when the optional package for ``extension`` is not installed."""
    if (required := _REQUIRED_PACKAGES.get(extension.lower())) is not None:
        _require_package(*required)


def _ensure_exists(path: Path) -> Path:
    if not path.exists():
        msg = f"Could not find file: {path}"
        raise FileNotFoundError(msg)
    return path


def _stringify_cell_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _excel_rows_to_chunks(
    workbook_name: str,
    sheets: Iterable[tuple[str, int, Iterable[Sequence[object]]]],
) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    for sheet_name, sheet_index, rows in sheets:
        lines: list[str] = []
        for row in rows:
            values = [_stringify_cell_value(value) for value in row]
            while values and values[-1] == "":
                values.pop()
            if line := ", ".join(values):
                lines.append(line)
        if lines:
            chunks.extend(
                chunk_rows(
                    "\n".join(lines),
                    name=workbook_name,
                    metadata={"sheet_name": sheet_name, "sheet_index": sheet_index},
                ),
            )
    return chunks


def _read_json(
    file: Path,
    *,
    name: str | None,
    chunk_size: int,
    chunk_overlap: int,
    chunk_strategy: Literal["fixed", "memory_entries"],
    encoding: str,
    on_malformed_json: Callable[[int, int], None] | None,
) -> list[KnowledgeChunk]:
    doc_name = name or file.stem
    source_text = file.read_text(encoding=encoding)
    try:
        contents = json.loads(source_text)
    except json.JSONDecodeError as exc:
        if on_malformed_json is not None:
            on_malformed_json(exc.lineno, exc.colno)
        return chunk_text(
            source_text,
            name=doc_name,
            chunk_size=chunk_size,
            overlap=chunk_overlap,
            strategy=chunk_strategy,
        )
    items = contents if isinstance(contents, list) else [contents]
    return [
        chunk
        for page, item in enumerate(items, start=1)
        for chunk in chunk_fixed(
            json.dumps(item, ensure_ascii=False),
            name=doc_name,
            metadata={"page": page},
            chunk_size=5000,
            overlap=0,
            min_chunk_fill_ratio=None,
        )
    ]


def _read_csv(file: Path, name: str | None, encoding: str) -> list[KnowledgeChunk]:
    with file.open(newline="", mode="r", encoding=encoding) as csvfile:
        csv_lines = [", ".join(_stringify_cell_value(cell) for cell in row) for row in csv.reader(csvfile)]
    return chunk_rows("\n".join(csv_lines), name=name or file.stem)


def _read_xlsx(file: Path, name: str | None) -> list[KnowledgeChunk]:
    import openpyxl  # noqa: PLC0415

    workbook = openpyxl.load_workbook(filename=str(file), read_only=True, data_only=True)
    try:
        sheets = (
            (worksheet.title, index, worksheet.iter_rows(values_only=True))
            for index, worksheet in enumerate(workbook.worksheets, start=1)
        )
        return _excel_rows_to_chunks(Path(name).stem if name else file.stem, sheets)
    finally:
        workbook.close()


def _read_xls(file: Path, name: str | None) -> list[KnowledgeChunk]:
    import xlrd  # noqa: PLC0415

    workbook = xlrd.open_workbook(filename=str(file))

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

    return _excel_rows_to_chunks(Path(name).stem if name else file.stem, _iter_sheets())


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


def _read_pdf(file: Path, name: str | None) -> list[KnowledgeChunk]:
    from pypdf import PdfReader as PyPdfReader  # noqa: PLC0415

    doc_name = name or file.stem.replace(" ", "_")
    pdf_reader = PyPdfReader(file)
    if pdf_reader.is_encrypted:
        try:
            decrypted = bool(pdf_reader.decrypt(""))
        except Exception as exc:
            raise KnowledgeReadError(_LOCKED_PDF_MESSAGE) from exc
        if not decrypted:
            raise KnowledgeReadError(_LOCKED_PDF_MESSAGE)

    pdf_content = [_sanitize_pdf_text(page.extract_text()) for page in pdf_reader.pages]
    pdf_content, shift = _clean_page_numbers(pdf_content)
    return [
        chunk
        for page_number, page_content in enumerate(pdf_content, start=shift if shift is not None else 1)
        for chunk in chunk_paragraphs(page_content, name=doc_name, metadata={"page": page_number}, chunk_size=5000)
    ]


def _read_docx(file: Path, name: str | None) -> list[KnowledgeChunk]:
    from docx import Document as DocxDocument  # noqa: PLC0415

    docx_document = DocxDocument(str(file))
    doc_content = "\n\n".join(para.text for para in docx_document.paragraphs)
    return chunk_paragraphs(doc_content, name=name or file.stem, chunk_size=5000)


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


def _read_pptx(file: Path, name: str | None) -> list[KnowledgeChunk]:
    from pptx import Presentation  # noqa: PLC0415  # ty: ignore[import-not-found]
    from pptx.shapes.group import GroupShape  # noqa: PLC0415  # ty: ignore[import-not-found]

    presentation = Presentation(str(file))
    slide_texts = [
        f"Slide {slide_number}:\n"
        + ("\n".join(texts) if (texts := _shape_texts(slide.shapes, GroupShape)) else "(No text content)")
        for slide_number, slide in enumerate(presentation.slides, start=1)
    ]
    return chunk_paragraphs("\n\n".join(slide_texts), name=name or file.stem, chunk_size=5000)


_DOCUMENT_READERS: dict[str, Callable[[Path, str | None], list[KnowledgeChunk]]] = {
    ".xlsx": _read_xlsx,
    ".xls": _read_xls,
    ".pdf": _read_pdf,
    ".docx": _read_docx,
    ".doc": _read_docx,
    ".pptx": _read_pptx,
}


def read_knowledge_file(
    file: Path,
    *,
    name: str | None = None,
    chunk_size: int = 5000,
    chunk_overlap: int = 0,
    chunk_strategy: Literal["fixed", "memory_entries"] = "fixed",
    encoding: str = "utf-8",
    on_malformed_json: Callable[[int, int], None] | None = None,
) -> list[KnowledgeChunk]:
    """Read and chunk one knowledge source file into native ``KnowledgeChunk`` values."""
    extension = file.suffix.lower()
    ensure_reader_available(extension)
    _ensure_exists(file)

    if extension == ".json":
        return _read_json(
            file,
            name=name,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            chunk_strategy=chunk_strategy,
            encoding=encoding,
            on_malformed_json=on_malformed_json,
        )
    if extension == ".csv":
        return _read_csv(file, name, encoding)
    if (reader := _DOCUMENT_READERS.get(extension)) is not None:
        return reader(file, name)
    return chunk_text(
        file.read_text(encoding=encoding),
        name=name or file.stem,
        chunk_size=chunk_size,
        overlap=chunk_overlap,
        strategy=chunk_strategy,
    )


__all__ = [
    "KnowledgeReadError",
    "ensure_reader_available",
    "read_knowledge_file",
]
