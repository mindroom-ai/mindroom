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
    from collections.abc import Awaitable, Callable, Sequence

    from mindroom.custom_tools.microsoft_graph_client import DocumentRef

type CellValue = str | int | float | bool
type CellGrid = list[list[CellValue]]
type EditOutcomeName = Literal["applied", "conflict", "failed", "not_attempted"]

MAX_READ_CELLS = 2_000
MAX_EDITS = 25
MAX_EDIT_CELLS = 500
_MAX_COLUMNS = 16_384
_MAX_ROWS = 1_048_576
_MAX_CELL_TEXT_CHARS = 32_767
_MAX_OUTLINE_SHEETS = 100
_MAX_USED_RANGE_SHEETS = 20
_MAX_OUTLINE_TABLES = 20
_MAX_OUTLINE_NAMES = 100
_OUTLINE_CONCURRENCY = 4
_CELL_PATTERN = re.compile(r"\$?([A-Za-z]{1,3})\$?([0-9]{1,7})")
_SHEET_NAME_FORBIDDEN = frozenset("[]:*?/\\")


class WorkbookArgumentError(GraphError):
    """A model-supplied argument was rejected before any network access."""

    def __init__(self, message: str, **details: object) -> None:
        super().__init__(code="invalid_argument", message=message, **details)


@dataclass(frozen=True, slots=True)
class SheetRange:
    """One sheet-qualified rectangular cell range in canonical A1 form."""

    sheet: str
    address: str
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
        rows=end_row - start_row + 1,
        columns=end_column - start_column + 1,
    )


def _cell(value: object, field_name: str) -> CellValue:
    if value is None:
        return ""
    if isinstance(value, bool | int | str):
        if isinstance(value, str) and len(value) > _MAX_CELL_TEXT_CHARS:
            msg = f"{field_name} contains a cell longer than {_MAX_CELL_TEXT_CHARS} characters."
            raise WorkbookArgumentError(msg)
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    msg = f"{field_name} cells must be strings, finite numbers, booleans, or null."
    raise WorkbookArgumentError(msg)


def validate_grid(value: object, *, rows: int, columns: int, field_name: str) -> CellGrid:
    """Return a rectangular cell grid matching the range, with null cells as empty strings."""
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


async def _worksheet_id(token: str, ref: DocumentRef, sheet: str) -> str:
    worksheets = await _worksheets(token, ref)
    for worksheet in worksheets:
        if cast("str", worksheet["name"]).casefold() == sheet.casefold():
            return cast("str", worksheet["id"])
    raise GraphError(
        code="sheet_not_found",
        message=f"The workbook has no worksheet named '{sheet}'.",
        available_sheets=[worksheet["name"] for worksheet in worksheets[:_MAX_OUTLINE_SHEETS]],
    )


def _range_path(ref: DocumentRef, worksheet_id: str, address: str) -> str:
    # The address is canonical A1 text, so it cannot close the quoted function argument.
    return ref.path("workbook", "worksheets", quote(worksheet_id, safe=""), f"range(address='{address}')")


async def _bounded_gather[T](calls: Sequence[Callable[[], Awaitable[T]]]) -> list[T]:
    semaphore = asyncio.Semaphore(_OUTLINE_CONCURRENCY)

    async def run(call: Callable[[], Awaitable[T]]) -> T:
        async with semaphore:
            return await call()

    return list(await asyncio.gather(*(run(call) for call in calls)))


async def workbook_outline(token: str, ref: DocumentRef) -> dict[str, object]:
    """Return worksheet names with used ranges, tables with their ranges, and workbook names."""
    worksheets = await _worksheets(token, ref)
    listed = worksheets[:_MAX_OUTLINE_SHEETS]
    detailed = [sheet for sheet in listed if sheet.get("visibility") in (None, "Visible")][:_MAX_USED_RANGE_SHEETS]

    def used_range(sheet: dict[str, object]) -> Callable[[], Awaitable[object]]:
        path = ref.path(
            "workbook",
            "worksheets",
            quote(cast("str", sheet["id"]), safe=""),
            "usedRange(valuesOnly=true)",
        )
        return lambda: graph_json(token, "GET", path, params={"$select": "address,rowCount,columnCount"})

    used_ranges = dict(
        zip(
            (cast("str", sheet["id"]) for sheet in detailed),
            await _bounded_gather([used_range(sheet) for sheet in detailed]),
            strict=True,
        ),
    )
    tables_payload, names_payload = await asyncio.gather(
        graph_json(token, "GET", ref.path("workbook", "tables"), params={"$select": "id,name"}),
        graph_json(token, "GET", ref.path("workbook", "names"), params={"$select": "name,value,visible"}),
    )
    tables = [table for table in _items(tables_payload) if isinstance(table.get("id"), str)]

    def table_range(table: dict[str, object]) -> Callable[[], Awaitable[object]]:
        path = ref.path("workbook", "tables", quote(cast("str", table["id"]), safe=""), "range")
        return lambda: graph_json(token, "GET", path, params={"$select": "address"})

    table_ranges = await _bounded_gather([table_range(table) for table in tables[:_MAX_OUTLINE_TABLES]])
    names = [name for name in _items(names_payload) if name.get("visible") is not False]
    outline_sheets: list[dict[str, object]] = []
    for sheet in listed:
        used = _mapping(used_ranges.get(cast("str", sheet["id"])))
        outline_sheets.append(
            {
                "name": sheet["name"],
                "visibility": sheet.get("visibility"),
                "used_range": used.get("address"),
                "rows": used.get("rowCount"),
                "columns": used.get("columnCount"),
            },
        )
    return {
        "worksheets": outline_sheets,
        "worksheets_truncated": len(worksheets) > len(listed),
        "tables": [
            {"name": table.get("name"), "range": _mapping(table_range_payload).get("address")}
            for table, table_range_payload in zip(tables, table_ranges, strict=False)
        ],
        "tables_truncated": len(tables) > _MAX_OUTLINE_TABLES,
        "names": [{"name": name.get("name"), "value": name.get("value")} for name in names[:_MAX_OUTLINE_NAMES]],
        "names_truncated": len(names) > _MAX_OUTLINE_NAMES,
    }


async def read_range(token: str, ref: DocumentRef, target: SheetRange) -> dict[str, object]:
    """Return one bounded range's formulas, computed values, and number formats."""
    if target.cells > MAX_READ_CELLS:
        msg = f"Read at most {MAX_READ_CELLS} cells at a time; {target.label} has {target.cells}."
        raise WorkbookArgumentError(msg)
    worksheet_id = await _worksheet_id(token, ref, target.sheet)
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
    current: list[list[object]] | None = None
    written: list[list[object]] | None = None
    error: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the outcome without empty fields."""
        fields: dict[str, object] = {"range": self.range, "outcome": self.outcome, "cells_changed": self.cells_changed}
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


def parse_edits(edits: object) -> list[EditPlan]:
    """Validate model-supplied edits before approval execution touches the workbook."""
    items = cast("list[object]", edits) if isinstance(edits, list) else None
    if not items:
        msg = "edits must be a non-empty list of {range, before, after} objects."
        raise WorkbookArgumentError(msg)
    if len(items) > MAX_EDITS:
        msg = f"Apply at most {MAX_EDITS} edits per call."
        raise WorkbookArgumentError(msg)
    plans: list[EditPlan] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        edit = _mapping(item)
        target = parse_sheet_range(edit.get("range"))
        key = (target.sheet.casefold(), target.address)
        if key in seen:
            msg = f"edits[{index}] repeats range {target.label}; combine edits to one range into one entry."
            raise WorkbookArgumentError(msg)
        seen.add(key)
        before = validate_grid(edit.get("before"), rows=target.rows, columns=target.columns, field_name="before")
        after = validate_grid(edit.get("after"), rows=target.rows, columns=target.columns, field_name="after")
        raw_format = edit.get("number_format")
        number_format = None
        if raw_format is not None:
            grid = validate_grid(raw_format, rows=target.rows, columns=target.columns, field_name="number_format")
            if not all(isinstance(cell, str) for row in grid for cell in row):
                msg = "number_format cells must be Excel number format strings such as '0.0%'."
                raise WorkbookArgumentError(msg)
            number_format = cast("list[list[str]]", grid)
        plans.append(EditPlan(target=target, before=before, after=after, number_format=number_format))
    total_cells = sum(plan.target.cells for plan in plans)
    if total_cells > MAX_EDIT_CELLS:
        msg = f"Edit at most {MAX_EDIT_CELLS} cells per call; these edits cover {total_cells}."
        raise WorkbookArgumentError(msg)
    return plans


def _error_fields(exc: GraphError) -> dict[str, object]:
    return {"code": exc.code, "message": exc.message, **exc.details}


async def apply_edits(token: str, ref: DocumentRef, plans: Sequence[EditPlan], *, skip_conflicts: bool) -> EditReceipt:
    """Write every edit whose range still holds its ``before`` formulas, verifying each write.

    All ranges are checked before any write. A conflict aborts the call unless ``skip_conflicts``.
    Writes run in order and stop at the first failure; Graph has no conditional range write,
    so the check narrows but cannot close the window for a concurrent edit.
    """
    worksheet_ids = {worksheet["name"]: worksheet["id"] for worksheet in await _worksheets(token, ref)}
    folded_ids = {cast("str", name).casefold(): cast("str", sheet_id) for name, sheet_id in worksheet_ids.items()}
    missing = sorted({plan.target.sheet for plan in plans if plan.target.sheet.casefold() not in folded_ids})
    if missing:
        raise GraphError(
            code="sheet_not_found",
            message=f"The workbook has no worksheet named '{missing[0]}'.",
            available_sheets=list(worksheet_ids)[:_MAX_OUTLINE_SHEETS],
        )
    paths = [_range_path(ref, folded_ids[plan.target.sheet.casefold()], plan.target.address) for plan in plans]
    current_payloads = await _bounded_gather(
        [lambda path=path: graph_json(token, "GET", path, params={"$select": "formulas"}) for path in paths],
    )
    receipt = EditReceipt()
    writable: list[tuple[EditPlan, str, EditOutcome]] = []
    for plan, path, payload in zip(plans, paths, current_payloads, strict=True):
        current = _grid(_mapping(payload).get("formulas"))
        outcome = EditOutcome(range=plan.target.label, outcome="not_attempted")
        receipt.edits.append(outcome)
        if grids_equal(current, plan.before):
            writable.append((plan, path, outcome))
        else:
            outcome.outcome = "conflict"
            outcome.current = current
    if not skip_conflicts and any(edit.outcome == "conflict" for edit in receipt.edits):
        return receipt
    for plan, path, outcome in writable:
        body: dict[str, object] = {"formulas": plan.after}
        if plan.number_format is not None:
            body["numberFormat"] = plan.number_format
        try:
            written = _mapping(await graph_json(token, "PATCH", path, json_body=body))
        except GraphError as exc:
            outcome.outcome = "failed"
            outcome.error = _error_fields(exc)
            break
        written_formulas = _grid(written.get("formulas"))
        outcome.outcome = "applied"
        outcome.cells_changed = changed_cell_count(plan.before, plan.after)
        formats_match = plan.number_format is None or grids_equal(
            _grid(written.get("numberFormat")),
            plan.number_format,
        )
        outcome.verified = grids_equal(written_formulas, plan.after) and formats_match
        if not outcome.verified:
            outcome.written = written_formulas
    return receipt
