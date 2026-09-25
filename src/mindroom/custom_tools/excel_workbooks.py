"""Excel workbook reads and conflict-checked cell edits through the Microsoft Graph workbook API."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import quote

from mindroom.custom_tools.microsoft_graph_client import GraphError, graph_json

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.custom_tools.microsoft_graph_client import DocumentRef

type CellValue = str | int | float | bool
type CellGrid = list[list[CellValue]]
type EditOutcomeName = Literal["applied", "conflict", "failed", "not_attempted"]

MAX_READ_CELLS = 2_000
MAX_EDITS = 25
MAX_EDIT_CELLS = 500
# Keeps the approval card's full arguments far below the approval payload limit.
MAX_EDIT_TEXT_CHARS = 200_000
TEXT_NUMBER_FORMAT = "@"
_MAX_COLUMNS = 16_384
_MAX_ROWS = 1_048_576
_MAX_CELL_TEXT_CHARS = 32_767
_MAX_OUTLINE_SHEETS = 100
_MAX_USED_RANGE_SHEETS = 20
_MAX_OUTLINE_TABLES = 20
_MAX_OUTLINE_NAMES = 100
_CELL_PATTERN = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})")
_SHEET_NAME_FORBIDDEN = frozenset("[]:*?/\\")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_COERCED_TEXT_PATTERNS = (
    re.compile(r"\d{1,4}\s*[-/.]\s*\d{1,2}(?:\s*[-/.]\s*\d{1,4})?"),
    re.compile(rf"(?:\d{{1,2}}[-\s])?{_MONTH}(?:[-\s,]+\d{{1,4}}){{0,2}}", re.IGNORECASE),
    re.compile(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:\s*[ap]\.?m\.?)?", re.IGNORECASE),
    re.compile(r"\d+\s+\d+/\d+"),
    re.compile(r"true|false", re.IGNORECASE),
)
# Excel reads input starting with these characters as a formula or signed number.
_FORMULA_PREFIXES = ("+", "-", "@")
# Microsoft recommends one request at a time per workbook; this serializes edits within this process.
_DOCUMENT_LOCKS: dict[str, asyncio.Lock] = {}


class WorkbookArgumentError(GraphError):
    """A model-supplied argument was rejected before any network access."""

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(code="invalid_argument", message=message, **details)


@dataclass(frozen=True, slots=True)
class SheetRange:
    """One sheet-qualified rectangular cell range in canonical A1 form."""

    sheet: str
    address: str
    first_row: int
    first_column: int
    rows: int
    columns: int

    @property
    def cells(self) -> int:
        """Return the number of cells the range covers."""
        return self.rows * self.columns

    @property
    def label(self) -> str:
        """Return the range as users write it, quoting the sheet name when Excel would."""
        needs_quotes = not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", self.sheet)
        sheet = "'" + self.sheet.replace("'", "''") + "'" if needs_quotes else self.sheet
        return f"{sheet}!{self.address}"

    def overlaps(self, other: SheetRange) -> bool:
        """Return whether both ranges share at least one cell of the same sheet."""
        return (
            self.sheet.casefold() == other.sheet.casefold()
            and self.first_row < other.first_row + other.rows
            and other.first_row < self.first_row + self.rows
            and self.first_column < other.first_column + other.columns
            and other.first_column < self.first_column + self.columns
        )


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _column_letters(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _parse_cell(text: str, value: str) -> tuple[int, int]:
    match = _CELL_PATTERN.fullmatch(text)
    if match is None:
        msg = f"Range '{value}' must use A1 cell references such as Sheet1!B4 or Sheet1!B4:C9."
        raise WorkbookArgumentError(msg)
    column = _column_number(match.group(1))
    row = int(match.group(2))
    if not 1 <= column <= _MAX_COLUMNS or not 1 <= row <= _MAX_ROWS:
        msg = f"Range '{value}' is outside the worksheet grid."
        raise WorkbookArgumentError(msg)
    return row, column


def _split_sheet(value: str) -> tuple[str, str]:
    """Split ``Sheet!A1`` or ``'My Sheet'!A1`` into the unquoted sheet name and address."""
    if value.startswith("'"):
        index = 1
        name_chars: list[str] = []
        while index < len(value):
            char = value[index]
            if char == "'":
                if value[index + 1 : index + 2] == "'":
                    name_chars.append("'")
                    index += 2
                    continue
                if value[index + 1 : index + 2] == "!":
                    return "".join(name_chars), value[index + 2 :]
                break
            name_chars.append(char)
            index += 1
        msg = f"Range '{value}' has an unterminated quoted sheet name."
        raise WorkbookArgumentError(msg)
    sheet, separator, address = value.rpartition("!")
    if not separator:
        msg = f"Range '{value}' must name its sheet, for example Sheet1!B4:B8."
        raise WorkbookArgumentError(msg)
    return sheet, address


def parse_sheet_range(value: object) -> SheetRange:
    """Return the canonical range for ``Sheet!A1`` or ``Sheet!A1:C9``, rejecting whole rows and columns."""
    if not isinstance(value, str) or not value.strip():
        msg = "Ranges must be non-empty strings such as Sheet1!B4:B8."
        raise WorkbookArgumentError(msg)
    text = value.strip()
    sheet, address = _split_sheet(text)
    if not sheet or len(sheet) > 31 or any(char in _SHEET_NAME_FORBIDDEN for char in sheet):
        msg = f"Range '{text}' does not name a valid worksheet."
        raise WorkbookArgumentError(msg)
    start_text, _, end_text = address.partition(":")
    start_row, start_column = _parse_cell(start_text, text)
    end_row, end_column = _parse_cell(end_text, text) if end_text else (start_row, start_column)
    if end_row < start_row or end_column < start_column:
        msg = f"Range '{text}' must go from its top-left cell to its bottom-right cell."
        raise WorkbookArgumentError(msg)
    start = f"{_column_letters(start_column)}{start_row}"
    end = f"{_column_letters(end_column)}{end_row}"
    return SheetRange(
        sheet=sheet,
        address=start if start == end else f"{start}:{end}",
        first_row=start_row,
        first_column=start_column,
        rows=end_row - start_row + 1,
        columns=end_column - start_column + 1,
    )


def _cell(value: object, field_name: str) -> CellValue:
    if value is None:
        # Graph skips null cells instead of clearing them, so an empty string is the only way to clear.
        msg = f'{field_name} cells must not be null; use "" for an empty cell.'
        raise WorkbookArgumentError(msg)
    if isinstance(value, bool | int | str):
        if isinstance(value, str) and len(value) > _MAX_CELL_TEXT_CHARS:
            msg = f"{field_name} contains a cell longer than {_MAX_CELL_TEXT_CHARS} characters."
            raise WorkbookArgumentError(msg)
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    msg = f"{field_name} cells must be strings, finite numbers, or booleans."
    raise WorkbookArgumentError(msg)


def validate_grid(value: object, *, rows: int, columns: int, field_name: str) -> CellGrid:
    """Return a rectangular grid of scalar cells matching the range."""
    grid = cast("list[object]", value) if isinstance(value, list) else None
    if grid is None or len(grid) != rows:
        msg = f"{field_name} must be a list of {rows} row(s), each a list of {columns} cell(s)."
        raise WorkbookArgumentError(msg)
    validated: CellGrid = []
    for row in grid:
        cells = cast("list[object]", row) if isinstance(row, list) else None
        if cells is None or len(cells) != columns:
            msg = f"{field_name} must be a list of {rows} row(s), each a list of {columns} cell(s)."
            raise WorkbookArgumentError(msg)
        validated.append([_cell(cell, field_name) for cell in cells])
    return validated


def _looks_numeric(text: str) -> bool:
    """Return whether Excel would read the text as a number, percentage, or currency amount."""
    stripped = text.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    stripped = stripped.strip("$€£¥").strip().removesuffix("%").strip().strip("$€£¥").strip()
    stripped = stripped.replace(",", "")
    if not any(char.isdigit() for char in stripped):
        return False
    try:
        float(stripped)
    except ValueError:
        return False
    return True


def excel_would_convert(value: CellValue) -> bool:
    """Return whether Excel would store this text as something else, such as a number, date, or formula.

    Graph parses written cells as if typed, so text like ``0042`` loses its zeros and ``1/2``
    becomes a date. This is a conservative approximation of Excel's parser.
    """
    if not isinstance(value, str) or not value.strip() or value.startswith("="):
        return False
    text = value.strip()
    return (
        _looks_numeric(text)
        or any(pattern.fullmatch(text) for pattern in _COERCED_TEXT_PATTERNS)
        or (len(text) > 1 and text.startswith(_FORMULA_PREFIXES))
    )


def cells_equal(left: object, right: object) -> bool:
    """Compare two cell values: numbers numerically, booleans only with booleans, strings exactly."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-15)
    return left == right


def grids_equal(left: Sequence[Sequence[object]], right: Sequence[Sequence[object]]) -> bool:
    """Return whether two grids have the same shape and equal cells."""
    return len(left) == len(right) and all(
        len(left_row) == len(right_row)
        and all(cells_equal(left_cell, right_cell) for left_cell, right_cell in zip(left_row, right_row, strict=True))
        for left_row, right_row in zip(left, right, strict=True)
    )


def changed_cell_count(before: CellGrid, after: CellGrid) -> int:
    """Return how many cells differ between two same-shaped grids."""
    return sum(
        not cells_equal(old, new)
        for old_row, new_row in zip(before, after, strict=True)
        for old, new in zip(old_row, new_row, strict=True)
    )


def _mapping(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _grid(value: object) -> list[list[object]]:
    rows = value if isinstance(value, list) else []
    return [cast("list[object]", row) if isinstance(row, list) else [] for row in rows]


def _items(payload: object) -> list[dict[str, object]]:
    values = _mapping(payload).get("value")
    return [_mapping(item) for item in values] if isinstance(values, list) else []


async def _worksheets(token: str, ref: DocumentRef) -> list[dict[str, object]]:
    payload = await graph_json(
        token,
        "GET",
        ref.path("workbook", "worksheets"),
        params={"$select": "id,name,visibility"},
    )
    return [item for item in _items(payload) if isinstance(item.get("id"), str) and isinstance(item.get("name"), str)]


def _sheet_not_found(sheet: str, worksheets: list[dict[str, object]]) -> GraphError:
    return GraphError(
        code="sheet_not_found",
        message=f"The workbook has no worksheet named '{sheet}'.",
        available_sheets=[worksheet["name"] for worksheet in worksheets[:_MAX_OUTLINE_SHEETS]],
    )


async def _worksheet_ids(token: str, ref: DocumentRef, sheets: Sequence[str]) -> dict[str, str]:
    """Map each requested sheet name, case-insensitively, to its worksheet ID."""
    worksheets = await _worksheets(token, ref)
    by_name = {cast("str", worksheet["name"]).casefold(): cast("str", worksheet["id"]) for worksheet in worksheets}
    for sheet in sheets:
        if sheet.casefold() not in by_name:
            raise _sheet_not_found(sheet, worksheets)
    return {sheet: by_name[sheet.casefold()] for sheet in sheets}


def _worksheet_path(ref: DocumentRef, worksheet_id: str, *rest: str) -> str:
    return ref.path("workbook", "worksheets", quote(worksheet_id, safe=""), *rest)


def _range_path(ref: DocumentRef, worksheet_id: str, address: str) -> str:
    # The address is canonical A1 text, so it cannot close the quoted function argument.
    return _worksheet_path(ref, worksheet_id, f"range(address='{address}')")


async def workbook_outline(token: str, ref: DocumentRef) -> dict[str, object]:
    """Return worksheet names with used ranges, tables with their ranges, and visible workbook names.

    Calls run one at a time, as Microsoft recommends for requests to one workbook.
    """
    worksheets = await _worksheets(token, ref)
    listed = worksheets[:_MAX_OUTLINE_SHEETS]
    detailed = [sheet for sheet in listed if sheet.get("visibility") in (None, "Visible")][:_MAX_USED_RANGE_SHEETS]
    used_ranges: dict[str, dict[str, object]] = {}
    for sheet in detailed:
        sheet_id = cast("str", sheet["id"])
        used_ranges[sheet_id] = _mapping(
            await graph_json(
                token,
                "GET",
                _worksheet_path(ref, sheet_id, "usedRange(valuesOnly=true)"),
                params={"$select": "address,rowCount,columnCount"},
            ),
        )
    tables = [
        table
        for table in _items(
            await graph_json(token, "GET", ref.path("workbook", "tables"), params={"$select": "id,name"}),
        )
        if isinstance(table.get("id"), str)
    ]
    outline_tables: list[dict[str, object]] = []
    for table in tables[:_MAX_OUTLINE_TABLES]:
        table_range = _mapping(
            await graph_json(
                token,
                "GET",
                ref.path("workbook", "tables", quote(cast("str", table["id"]), safe=""), "range"),
                params={"$select": "address"},
            ),
        )
        outline_tables.append({"name": table.get("name"), "range": table_range.get("address")})
    names = [
        name
        for name in _items(
            await graph_json(token, "GET", ref.path("workbook", "names"), params={"$select": "name,value,visible"}),
        )
        if name.get("visible") is not False
    ]
    return {
        "worksheets": [
            {
                "name": sheet["name"],
                "visibility": sheet.get("visibility"),
                "used_range": used_ranges.get(cast("str", sheet["id"]), {}).get("address"),
                "rows": used_ranges.get(cast("str", sheet["id"]), {}).get("rowCount"),
                "columns": used_ranges.get(cast("str", sheet["id"]), {}).get("columnCount"),
            }
            for sheet in listed
        ],
        "worksheets_truncated": len(worksheets) > len(listed),
        "tables": outline_tables,
        "tables_truncated": len(tables) > _MAX_OUTLINE_TABLES,
        "names": [{"name": name.get("name"), "value": name.get("value")} for name in names[:_MAX_OUTLINE_NAMES]],
        "names_truncated": len(names) > _MAX_OUTLINE_NAMES,
    }


async def read_range(token: str, ref: DocumentRef, target: SheetRange) -> dict[str, object]:
    """Return one bounded range's formulas, computed values, and number formats."""
    if target.cells > MAX_READ_CELLS:
        msg = f"Read at most {MAX_READ_CELLS} cells at a time; {target.label} has {target.cells}."
        raise WorkbookArgumentError(msg)
    worksheet_id = (await _worksheet_ids(token, ref, [target.sheet]))[target.sheet]
    payload = _mapping(
        await graph_json(
            token,
            "GET",
            _range_path(ref, worksheet_id, target.address),
            params={"$select": "address,formulas,values,numberFormat"},
        ),
    )
    return {
        "range": target.label,
        "address": payload.get("address"),
        "formulas": _grid(payload.get("formulas")),
        "values": _grid(payload.get("values")),
        "number_format": _grid(payload.get("numberFormat")),
    }


@dataclass(frozen=True, slots=True)
class EditPlan:
    """One validated edit: the range, the formulas it must still hold, and what to write."""

    target: SheetRange
    before: CellGrid
    after: CellGrid
    number_format: list[list[str]] | None = None


@dataclass(slots=True)
class EditOutcome:
    """What happened to one planned edit."""

    range: str
    outcome: EditOutcomeName
    cells_changed: int = 0
    verified: bool | None = None
    already_applied: bool = False
    current: list[list[object]] | None = None
    written: list[list[object]] | None = None
    error: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the outcome without empty fields."""
        fields: dict[str, object] = {"range": self.range, "outcome": self.outcome, "cells_changed": self.cells_changed}
        if self.already_applied:
            fields["already_applied"] = True
        for name in ("verified", "current", "written", "error"):
            value = getattr(self, name)
            if value is not None:
                fields[name] = value
        return fields


@dataclass(slots=True)
class EditReceipt:
    """The outcome of one approved edit call."""

    edits: list[EditOutcome] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        """Return whether any edit applied."""
        return any(edit.outcome == "applied" for edit in self.edits)

    @property
    def cells_changed(self) -> int:
        """Return the number of cells changed by applied edits."""
        return sum(edit.cells_changed for edit in self.edits if edit.outcome == "applied")

    @property
    def verified(self) -> bool:
        """Return whether every applied edit read back exactly as written."""
        return all(edit.verified for edit in self.edits if edit.outcome == "applied")

    @property
    def status(self) -> Literal["applied", "partial", "conflict", "failed"]:
        """Summarize the call: everything applied, some applied, nothing applied due to conflicts, or failed."""
        outcomes = {edit.outcome for edit in self.edits}
        if outcomes == {"applied"}:
            return "applied"
        if "applied" in outcomes:
            return "partial"
        return "failed" if "failed" in outcomes else "conflict"


def _validated_number_format(raw: object, target: SheetRange) -> list[list[str]] | None:
    if raw is None:
        return None
    grid = validate_grid(raw, rows=target.rows, columns=target.columns, field_name="number_format")
    if not all(isinstance(cell, str) and cell for row in grid for cell in row):
        msg = "number_format cells must be non-empty Excel number format strings such as '0.0%' or '@'."
        raise WorkbookArgumentError(msg)
    return cast("list[list[str]]", grid)


def _reject_converted_text(target: SheetRange, after: CellGrid, number_format: list[list[str]] | None) -> None:
    for row_index, row in enumerate(after):
        for column_index, value in enumerate(row):
            text_format = number_format is not None and number_format[row_index][column_index] == TEXT_NUMBER_FORMAT
            if not text_format and excel_would_convert(value):
                cell = f"{_column_letters(target.first_column + column_index)}{target.first_row + row_index}"
                msg = (
                    f"after for {target.label} cell {cell} is text Excel would convert ({value!r}). "
                    "Write numbers as JSON numbers, dates as serial numbers with a date number_format, "
                    f"or set number_format '{TEXT_NUMBER_FORMAT}' for that cell to keep it as text."
                )
                raise WorkbookArgumentError(msg)


def _text_chars(grid: CellGrid) -> int:
    return sum(len(cell) for row in grid for cell in row if isinstance(cell, str))


def parse_edits(edits: object) -> list[EditPlan]:
    """Validate model-supplied edits before execution touches the workbook."""
    items = cast("list[object]", edits) if isinstance(edits, list) else None
    if not items:
        msg = "edits must be a non-empty list of {range, before, after} objects."
        raise WorkbookArgumentError(msg)
    if len(items) > MAX_EDITS:
        msg = f"Apply at most {MAX_EDITS} edits per call."
        raise WorkbookArgumentError(msg)
    plans: list[EditPlan] = []
    for index, item in enumerate(items):
        edit = _mapping(item)
        target = parse_sheet_range(edit.get("range"))
        if overlapping := next((plan.target for plan in plans if plan.target.overlaps(target)), None):
            msg = f"edits[{index}] range {target.label} overlaps {overlapping.label}; edits must not share cells."
            raise WorkbookArgumentError(msg)
        before = validate_grid(edit.get("before"), rows=target.rows, columns=target.columns, field_name="before")
        after = validate_grid(edit.get("after"), rows=target.rows, columns=target.columns, field_name="after")
        number_format = _validated_number_format(edit.get("number_format"), target)
        _reject_converted_text(target, after, number_format)
        plans.append(EditPlan(target=target, before=before, after=after, number_format=number_format))
    total_cells = sum(plan.target.cells for plan in plans)
    if total_cells > MAX_EDIT_CELLS:
        msg = f"Edit at most {MAX_EDIT_CELLS} cells per call; these edits cover {total_cells}."
        raise WorkbookArgumentError(msg)
    if sum(_text_chars(plan.before) + _text_chars(plan.after) for plan in plans) > MAX_EDIT_TEXT_CHARS:
        msg = f"Edits may carry at most {MAX_EDIT_TEXT_CHARS} characters of cell text per call; split them."
        raise WorkbookArgumentError(msg)
    return plans


def _error_fields(exc: GraphError) -> dict[str, object]:
    return {"code": exc.code, "message": exc.message, **exc.details}


async def _current_formulas(token: str, path: str) -> list[list[object]]:
    return _grid(_mapping(await graph_json(token, "GET", path, params={"$select": "formulas"})).get("formulas"))


async def _write(token: str, path: str, plan: EditPlan, outcome: EditOutcome) -> bool:
    """Write one edit and record its outcome; return False when later edits must not run."""
    try:
        if plan.number_format is not None:
            # Formats go first so text-formatted cells keep text such as leading zeros.
            await graph_json(token, "PATCH", path, json_body={"numberFormat": plan.number_format})
        written = _mapping(await graph_json(token, "PATCH", path, json_body={"formulas": plan.after}))
    except GraphError as exc:
        outcome.error = _error_fields(exc)
        # A timeout or server error can hide a write that landed, so read the range back.
        try:
            current = await _current_formulas(token, path)
        except GraphError:
            outcome.outcome = "failed"
            outcome.error["outcome_unknown"] = True
            return False
        if grids_equal(current, plan.after):
            outcome.outcome = "applied"
            outcome.cells_changed = changed_cell_count(plan.before, plan.after)
            outcome.verified = plan.number_format is None
            return False
        outcome.outcome = "failed"
        outcome.current = current
        return False
    written_formulas = _grid(written.get("formulas"))
    formats_match = plan.number_format is None or grids_equal(_grid(written.get("numberFormat")), plan.number_format)
    outcome.outcome = "applied"
    outcome.cells_changed = changed_cell_count(plan.before, plan.after)
    outcome.verified = grids_equal(written_formulas, plan.after) and formats_match
    if not outcome.verified:
        outcome.written = written_formulas
    return True


async def apply_edits(token: str, ref: DocumentRef, plans: Sequence[EditPlan], *, skip_conflicts: bool) -> EditReceipt:
    """Write every edit whose range still holds its ``before`` formulas, verifying each write.

    All ranges are checked before any write. A range already holding ``after`` counts as applied,
    so a replayed approval does not write twice. A conflict aborts the call unless ``skip_conflicts``.
    Writes run one at a time and stop at the first failure. Graph has no conditional range write,
    so the check narrows but cannot close the window for a concurrent edit.
    """
    lock = _DOCUMENT_LOCKS.setdefault(ref.document_id, asyncio.Lock())
    async with lock:
        worksheet_ids = await _worksheet_ids(token, ref, [plan.target.sheet for plan in plans])
        receipt = EditReceipt()
        writable: list[tuple[EditPlan, str, EditOutcome]] = []
        for plan in plans:
            path = _range_path(ref, worksheet_ids[plan.target.sheet], plan.target.address)
            current = await _current_formulas(token, path)
            outcome = EditOutcome(range=plan.target.label, outcome="not_attempted")
            receipt.edits.append(outcome)
            if grids_equal(current, plan.before):
                writable.append((plan, path, outcome))
            elif grids_equal(current, plan.after):
                outcome.outcome = "applied"
                outcome.already_applied = True
                outcome.verified = True
            else:
                outcome.outcome = "conflict"
                outcome.current = current
        if not skip_conflicts and any(edit.outcome == "conflict" for edit in receipt.edits):
            return receipt
        for plan, path, outcome in writable:
            if not await _write(token, path, plan, outcome):
                break
        return receipt
