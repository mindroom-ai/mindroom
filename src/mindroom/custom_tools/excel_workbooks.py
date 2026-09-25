"""Excel workbook reads and conflict-checked cell edits through the Microsoft Graph workbook API."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import quote

from mindroom.custom_tools.microsoft_graph_client import GraphError, InvalidArgumentError, graph_json, graph_object

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.custom_tools.microsoft_graph_client import DocumentRef

type _CellValue = str | int | float | bool
type _CellGrid = list[list[_CellValue]]
type _FormatGrid = list[list[str | None]]
type _EditOutcomeName = Literal["applied", "conflict", "failed", "not_attempted"]

_MAX_READ_CELLS = 2_000
_MAX_EDITS = 25
_MAX_EDIT_CELLS = 500
# Bounds the text one approved call can carry; the approval card only shows payloads up to 2 MB in full.
_MAX_EDIT_TEXT_CHARS = 200_000
_TEXT_NUMBER_FORMAT = "@"
# Excel stores numbers as doubles, which hold integers exactly only up to 2**53.
_MAX_EXACT_INTEGER = 2**53
_MAX_COLUMNS = 16_384
_MAX_ROWS = 1_048_576
_MAX_CELL_TEXT_CHARS = 32_767
_MAX_OUTLINE_SHEETS = 100
_MAX_USED_RANGE_SHEETS = 20
_MAX_OUTLINE_TABLES = 20
_MAX_OUTLINE_NAMES = 100
_CELL_PATTERN = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})")
_SHEET_NAME_FORBIDDEN = frozenset("[]:*?/\\")
_NUMBER_PATTERN = re.compile(r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_TIME = (
    r"[0-9]{1,2}(?::[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?)?\s*[ap]\.?m\.?|[0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
)
_NUMERIC_DATE = r"[0-9]{1,4}\s*[-/.]\s*[0-9]{1,2}(?:\s*[-/.]\s*[0-9]{1,4})?|[0-9]{1,2}\s*[-/.]\s*[0-9]{4}"
# Text Excel parses as a date, time, fraction, boolean, or error value when it is typed or written.
_CONVERTED_TEXT_PATTERNS = (
    re.compile(rf"(?:{_NUMERIC_DATE})(?:\s+(?:{_TIME}))?", re.IGNORECASE),
    re.compile(rf"[0-9]{{1,2}}[-\s/]{_MONTH}(?:[-\s,/]+[0-9]{{1,4}})?", re.IGNORECASE),
    re.compile(rf"{_MONTH}[-\s,/.]+[0-9]{{1,4}}(?:[-\s,]+[0-9]{{1,4}})?", re.IGNORECASE),
    re.compile(_TIME, re.IGNORECASE),
    re.compile(r"[0-9]+\s+[0-9]+/[0-9]+"),
    re.compile(r"true|false", re.IGNORECASE),
    re.compile(r"#(?:N/A|DIV/0!|VALUE!|REF!|NAME\?|NUM!|NULL!|SPILL!|CALC!|GETTING_DATA)", re.IGNORECASE),
)
# Excel reads input starting with these characters as a formula or signed number.
_FORMULA_PREFIXES = ("+", "-", "@")
_FORMULA_STRING_LITERAL = re.compile(r'("(?:[^"]|"")*")')
# Microsoft recommends one request at a time per workbook; this serializes edits within this process.
_DOCUMENT_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass(frozen=True, slots=True)
class _SheetRange:
    """One sheet-qualified rectangular cell range in canonical A1 form."""

    sheet: str
    address: str
    first_row: int
    first_column: int
    rows: int
    columns: int

    @property
    def _cells(self) -> int:
        """Return the number of cells the range covers."""
        return self.rows * self.columns

    @property
    def label(self) -> str:
        """Return the range as users write it, quoting the sheet name when Excel would."""
        needs_quotes = not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", self.sheet)
        sheet = "'" + self.sheet.replace("'", "''") + "'" if needs_quotes else self.sheet
        return f"{sheet}!{self.address}"

    def _overlaps(self, other: _SheetRange) -> bool:
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
        raise InvalidArgumentError(msg)
    column = _column_number(match.group(1))
    row = int(match.group(2))
    if not 1 <= column <= _MAX_COLUMNS or not 1 <= row <= _MAX_ROWS:
        msg = f"Range '{value}' is outside the worksheet grid."
        raise InvalidArgumentError(msg)
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
        raise InvalidArgumentError(msg)
    sheet, separator, address = value.rpartition("!")
    if not separator:
        msg = f"Range '{value}' must name its sheet, for example Sheet1!B4:B8."
        raise InvalidArgumentError(msg)
    return sheet, address


def parse_sheet_range(value: object) -> _SheetRange:
    """Return the canonical range for ``Sheet!A1`` or ``Sheet!A1:C9``, rejecting whole rows and columns."""
    if not isinstance(value, str) or not value.strip():
        msg = "Ranges must be non-empty strings such as Sheet1!B4:B8."
        raise InvalidArgumentError(msg)
    text = value.strip()
    sheet, address = _split_sheet(text)
    if not sheet or len(sheet) > 31 or any(char in _SHEET_NAME_FORBIDDEN for char in sheet):
        msg = f"Range '{text}' does not name a valid worksheet."
        raise InvalidArgumentError(msg)
    start_text, _, end_text = address.partition(":")
    start_row, start_column = _parse_cell(start_text, text)
    end_row, end_column = _parse_cell(end_text, text) if end_text else (start_row, start_column)
    if end_row < start_row or end_column < start_column:
        msg = f"Range '{text}' must go from its top-left cell to its bottom-right cell."
        raise InvalidArgumentError(msg)
    start = f"{_column_letters(start_column)}{start_row}"
    end = f"{_column_letters(end_column)}{end_row}"
    return _SheetRange(
        sheet=sheet,
        address=start if start == end else f"{start}:{end}",
        first_row=start_row,
        first_column=start_column,
        rows=end_row - start_row + 1,
        columns=end_column - start_column + 1,
    )


def _cell(value: object, field_name: str) -> _CellValue:
    if value is None:
        # Graph skips null cells instead of clearing them, so an empty string is the only way to clear.
        msg = f'{field_name} cells must not be null; use "" for an empty cell.'
        raise InvalidArgumentError(msg)
    if isinstance(value, bool | str):
        if isinstance(value, str) and len(value) > _MAX_CELL_TEXT_CHARS:
            msg = f"{field_name} contains a cell longer than {_MAX_CELL_TEXT_CHARS} characters."
            raise InvalidArgumentError(msg)
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_EXACT_INTEGER:
            msg = (
                f"{field_name} contains an integer Excel cannot store exactly; write long identifiers "
                f"as text with number_format '{_TEXT_NUMBER_FORMAT}'."
            )
            raise InvalidArgumentError(msg)
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    msg = f"{field_name} cells must be strings, finite numbers, or booleans."
    raise InvalidArgumentError(msg)


def _rows(value: object, *, rows: int, columns: int, field_name: str) -> list[list[object]]:
    grid = cast("list[object]", value) if isinstance(value, list) else None
    shaped = (
        grid is not None and len(grid) == rows and all(isinstance(row, list) and len(row) == columns for row in grid)
    )
    if not shaped:
        msg = f"{field_name} must be a list of {rows} row(s), each a list of {columns} cell(s)."
        raise InvalidArgumentError(msg)
    return cast("list[list[object]]", grid)


def _validate_grid(value: object, *, rows: int, columns: int, field_name: str) -> _CellGrid:
    """Return a rectangular grid of scalar cells matching the range."""
    return [
        [_cell(cell, field_name) for cell in row]
        for row in _rows(value, rows=rows, columns=columns, field_name=field_name)
    ]


def _looks_numeric(text: str) -> bool:
    """Return whether Excel would read the text as a number, percentage, or currency amount."""
    stripped = text.strip()
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    stripped = stripped.strip("$€£¥").strip().removesuffix("%").strip().strip("$€£¥").strip()
    return any(char in "0123456789" for char in stripped) and _NUMBER_PATTERN.fullmatch(stripped) is not None


def _excel_would_convert(value: _CellValue) -> bool:
    """Return whether Excel would store this text as something else, such as a number, date, or formula.

    Graph parses written cells as if typed, so text like ``0042`` loses its zeros and ``1/2``
    becomes a date. This is a conservative approximation of Excel's parser.
    """
    if not isinstance(value, str) or not value.strip() or value.startswith("="):
        return False
    text = value.strip()
    return (
        _looks_numeric(text)
        or any(pattern.fullmatch(text) for pattern in _CONVERTED_TEXT_PATTERNS)
        or (len(text) > 1 and text.startswith(_FORMULA_PREFIXES))
    )


def _formula_key(formula: str) -> str:
    """Return a formula with case folded outside string literals, since Excel uppercases names and references."""
    return "".join(
        part if index % 2 else part.upper() for index, part in enumerate(_FORMULA_STRING_LITERAL.split(formula))
    )


def _cells_equal(left: object, right: object) -> bool:
    """Compare two cells: numbers numerically, formulas ignoring case outside literals, everything else exactly."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, int | float) and isinstance(right, int | float):
        # Only rounding in the last of a double's digits is tolerated, so distinct 15-digit values differ.
        return left == right or math.isclose(left, right, rel_tol=1e-15, abs_tol=0.0)
    if isinstance(left, str) and isinstance(right, str) and left.startswith("=") and right.startswith("="):
        return _formula_key(left) == _formula_key(right)
    return left == right


def _grids_equal(left: Sequence[Sequence[object]], right: Sequence[Sequence[object]]) -> bool:
    """Return whether two grids have the same shape and equal cells."""
    return len(left) == len(right) and all(
        len(left_row) == len(right_row)
        and all(_cells_equal(left_cell, right_cell) for left_cell, right_cell in zip(left_row, right_row, strict=True))
        for left_row, right_row in zip(left, right, strict=True)
    )


def _formats_match(written: Sequence[Sequence[object]], requested: _FormatGrid) -> bool:
    """Return whether every requested (non-null) number format was stored."""
    return len(written) == len(requested) and all(
        len(written_row) == len(requested_row)
        and all(cell is None or cell == stored for stored, cell in zip(written_row, requested_row, strict=True))
        for written_row, requested_row in zip(written, requested, strict=True)
    )


def _changed_cell_count(before: _CellGrid, after: _CellGrid) -> int:
    """Return how many cells differ between two same-shaped grids."""
    return sum(
        not _cells_equal(old, new)
        for old_row, new_row in zip(before, after, strict=True)
        for old, new in zip(old_row, new_row, strict=True)
    )


def _grid(value: object) -> list[list[object]]:
    rows = value if isinstance(value, list) else []
    return [cast("list[object]", row) if isinstance(row, list) else [] for row in rows]


def _items(payload: object) -> list[dict[str, object]]:
    values = graph_object(payload).get("value")
    return [graph_object(item) for item in values] if isinstance(values, list) else []


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
        used_ranges[sheet_id] = graph_object(
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
        table_range = graph_object(
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


async def read_range(token: str, ref: DocumentRef, target: _SheetRange) -> dict[str, object]:
    """Return one bounded range's formulas, computed values, and number formats."""
    if target._cells > _MAX_READ_CELLS:
        msg = f"Read at most {_MAX_READ_CELLS} cells at a time; {target.label} has {target._cells}."
        raise InvalidArgumentError(msg)
    worksheet_id = (await _worksheet_ids(token, ref, [target.sheet]))[target.sheet]
    payload = graph_object(
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
class _EditPlan:
    """One validated edit: the range, the formulas it must still hold, and what to write."""

    target: _SheetRange
    before: _CellGrid
    after: _CellGrid
    number_format: _FormatGrid | None = None


@dataclass(slots=True)
class _EditOutcome:
    """What happened to one planned edit."""

    range: str
    outcome: _EditOutcomeName
    cells_changed: int = 0
    verified: bool | None = None
    already_applied: bool = False
    current: list[list[object]] | None = None
    written: list[list[object]] | None = None
    error: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the full outcome for the agent, without empty fields."""
        fields = self.summary()
        if self.current is not None:
            fields["current"] = self.current
        if self.written is not None:
            fields["written"] = self.written
        if self.error is not None:
            fields["error"] = self.error
        return fields

    def summary(self) -> dict[str, object]:
        """Return the outcome without cell contents, for cards posted to the room."""
        fields: dict[str, object] = {"range": self.range, "outcome": self.outcome, "cells_changed": self.cells_changed}
        if self.already_applied:
            fields["already_applied"] = True
        if self.verified is not None:
            fields["verified"] = self.verified
        return fields


@dataclass(slots=True)
class EditReceipt:
    """The outcome of one approved edit call."""

    edits: list[_EditOutcome] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        """Return whether any edit applied."""
        return any(edit.outcome == "applied" for edit in self.edits)

    @property
    def outcome_unknown(self) -> bool:
        """Return whether a failed write may still have landed."""
        return any(edit.error is not None and edit.error.get("outcome_unknown") is True for edit in self.edits)

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


def _validated_number_format(raw: object, target: _SheetRange) -> _FormatGrid | None:
    """Return per-cell formats, where null leaves a cell's format unchanged."""
    if raw is None:
        return None
    grid = _rows(raw, rows=target.rows, columns=target.columns, field_name="number_format")
    if not all(cell is None or (isinstance(cell, str) and cell) for row in grid for cell in row):
        msg = "number_format cells must be Excel number formats such as '0.0%' or '@', or null to keep a format."
        raise InvalidArgumentError(msg)
    return cast("_FormatGrid", grid)


def _reject_converted_text(target: _SheetRange, after: _CellGrid, number_format: _FormatGrid | None) -> None:
    for row_index, row in enumerate(after):
        for column_index, value in enumerate(row):
            text_format = number_format is not None and number_format[row_index][column_index] == _TEXT_NUMBER_FORMAT
            if not text_format and _excel_would_convert(value):
                cell = f"{_column_letters(target.first_column + column_index)}{target.first_row + row_index}"
                msg = (
                    f"after for {target.label} cell {cell} is text Excel would convert ({value!r}). "
                    "Write numbers as JSON numbers, dates as serial numbers with a date number_format, "
                    f"or set number_format '{_TEXT_NUMBER_FORMAT}' for that cell to keep it as text."
                )
                raise InvalidArgumentError(msg)


def _text_chars(grid: Sequence[Sequence[object]]) -> int:
    return sum(len(cell) for row in grid for cell in row if isinstance(cell, str))


def parse_edits(edits: object) -> list[_EditPlan]:
    """Validate model-supplied edits before execution touches the workbook."""
    items = cast("list[object]", edits) if isinstance(edits, list) else None
    if not items:
        msg = "edits must be a non-empty list of {range, before, after} objects."
        raise InvalidArgumentError(msg)
    if len(items) > _MAX_EDITS:
        msg = f"Apply at most {_MAX_EDITS} edits per call."
        raise InvalidArgumentError(msg)
    plans: list[_EditPlan] = []
    for index, item in enumerate(items):
        edit = graph_object(item)
        target = parse_sheet_range(edit.get("range"))
        if overlapping := next((plan.target for plan in plans if plan.target._overlaps(target)), None):
            msg = f"edits[{index}] range {target.label} overlaps {overlapping.label}; edits must not share cells."
            raise InvalidArgumentError(msg)
        before = _validate_grid(edit.get("before"), rows=target.rows, columns=target.columns, field_name="before")
        after = _validate_grid(edit.get("after"), rows=target.rows, columns=target.columns, field_name="after")
        number_format = _validated_number_format(edit.get("number_format"), target)
        _reject_converted_text(target, after, number_format)
        plans.append(_EditPlan(target=target, before=before, after=after, number_format=number_format))
    total_cells = sum(plan.target._cells for plan in plans)
    if total_cells > _MAX_EDIT_CELLS:
        msg = f"Edit at most {_MAX_EDIT_CELLS} cells per call; these edits cover {total_cells}."
        raise InvalidArgumentError(msg)
    text = sum(
        _text_chars(plan.before) + _text_chars(plan.after) + _text_chars(plan.number_format or []) for plan in plans
    )
    if text > _MAX_EDIT_TEXT_CHARS:
        msg = f"Edits may carry at most {_MAX_EDIT_TEXT_CHARS} characters of cell text per call; split them."
        raise InvalidArgumentError(msg)
    return plans


def _error_fields(exc: GraphError) -> dict[str, object]:
    return {"code": exc.code, "message": exc.message, **exc.details}


async def _current(token: str, path: str) -> dict[str, object]:
    return graph_object(await graph_json(token, "GET", path, params={"$select": "formulas,numberFormat"}))


def _record_write(plan: _EditPlan, outcome: _EditOutcome, stored: dict[str, object]) -> None:
    """Mark an edit applied and verify what Excel stored against what was requested."""
    formulas = _grid(stored.get("formulas"))
    outcome.outcome = "applied"
    outcome.cells_changed = _changed_cell_count(plan.before, plan.after)
    formats_match = plan.number_format is None or _formats_match(_grid(stored.get("numberFormat")), plan.number_format)
    outcome.verified = _grids_equal(formulas, plan.after) and formats_match
    if not outcome.verified:
        outcome.written = formulas


async def _write(token: str, path: str, plan: _EditPlan, outcome: _EditOutcome) -> bool:
    """Write one edit and record its outcome; return False when later edits must not run."""
    formats_written = False
    try:
        if plan.number_format is not None:
            # Formats go first so text-formatted cells keep text such as leading zeros.
            await graph_json(token, "PATCH", path, json_body={"numberFormat": plan.number_format})
            formats_written = True
        written = graph_object(await graph_json(token, "PATCH", path, json_body={"formulas": plan.after}))
    except GraphError as exc:
        outcome.error = _error_fields(exc)
        if formats_written:
            outcome.error["number_format_written"] = True
        # A timeout or server error can hide a write that landed, so read the range back.
        try:
            current = await _current(token, path)
        except GraphError:
            outcome.outcome = "failed"
            outcome.error["outcome_unknown"] = True
            return False
        if _grids_equal(_grid(current.get("formulas")), plan.after):
            _record_write(plan, outcome, current)
        else:
            outcome.outcome = "failed"
            outcome.current = _grid(current.get("formulas"))
        return False
    _record_write(plan, outcome, written)
    return True


async def apply_edits(token: str, ref: DocumentRef, plans: Sequence[_EditPlan], *, skip_conflicts: bool) -> EditReceipt:
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
        writable: list[tuple[_EditPlan, str, _EditOutcome]] = []
        for plan in plans:
            path = _range_path(ref, worksheet_ids[plan.target.sheet], plan.target.address)
            current = _grid((await _current(token, path)).get("formulas"))
            outcome = _EditOutcome(range=plan.target.label, outcome="not_attempted")
            receipt.edits.append(outcome)
            if _grids_equal(current, plan.before):
                writable.append((plan, path, outcome))
            elif _grids_equal(current, plan.after):
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
