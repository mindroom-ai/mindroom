"""Excel range parsing, edit validation, and workbook operations against a fake Microsoft Graph."""

from __future__ import annotations

import asyncio
import json
import math
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.custom_tools import microsoft_graph_client
from mindroom.custom_tools.excel_workbooks import (
    MAX_EDIT_CELLS,
    MAX_EDITS,
    MAX_READ_CELLS,
    SheetRange,
    WorkbookArgumentError,
    apply_edits,
    cells_equal,
    changed_cell_count,
    excel_would_convert,
    parse_edits,
    parse_sheet_range,
    read_range,
    validate_grid,
    workbook_outline,
)
from mindroom.custom_tools.microsoft_graph_client import (
    DocumentRef,
    GraphAccessRejectedError,
    GraphError,
    graph_json,
    graph_path,
    share_id,
)
from tests.microsoft_graph_test_support import (
    ALICE_TOKEN,
    DRIVE_ID,
    ITEM_ID,
    FakeGraph,
    FakeSheet,
    graph_error,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

REF = DocumentRef(drive_id=DRIVE_ID, item_id=ITEM_ID)


def _run[T](coroutine: Callable[[], Awaitable[T]]) -> T:
    return asyncio.run(coroutine())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Sheet1!B4", SheetRange("Sheet1", "B4", 4, 2, 1, 1)),
        ("Sheet1!b4:c9", SheetRange("Sheet1", "B4:C9", 4, 2, 6, 2)),
        ("Sheet1!$B$4:$B$8", SheetRange("Sheet1", "B4:B8", 4, 2, 5, 1)),
        ("'Summary Sheet'!A1:B2", SheetRange("Summary Sheet", "A1:B2", 1, 1, 2, 2)),
        ("'O''Brien'!A1", SheetRange("O'Brien", "A1", 1, 1, 1, 1)),
        ("  Data!XFD1048576  ", SheetRange("Data", "XFD1048576", 1_048_576, 16_384, 1, 1)),
    ],
)
def test_parse_sheet_range_canonicalizes_a1_ranges(value: str, expected: SheetRange) -> None:
    """Sheet-qualified cell and range references parse to canonical addresses and sizes."""
    assert parse_sheet_range(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "B4",
        "Sheet1!B",
        "Sheet1!4:5",
        "Sheet1!A:C",
        "Sheet1!C9:B4",
        "Sheet1!XFE1",
        "Sheet1!A1048577",
        "'Unterminated!A1",
        "Bad/Name!A1",
        "!A1",
        "Sheet1!A1:B2:C3",
        "Sheet1!A1')/x",
        "",
        None,
        42,
    ],
)
def test_parse_sheet_range_rejects_everything_but_bounded_cell_ranges(value: object) -> None:
    """Unqualified, whole-row, whole-column, reversed, out-of-grid, and injected ranges are refused."""
    with pytest.raises(WorkbookArgumentError):
        parse_sheet_range(value)


def test_sheet_range_label_quotes_names_excel_would_quote() -> None:
    """Labels round-trip through the parser."""
    for value in ("Sheet1!B4", "'Summary Sheet'!A1:B2", "'O''Brien'!A1"):
        parsed = parse_sheet_range(value)
        assert parse_sheet_range(parsed.label) == parsed
    assert parse_sheet_range("'O''Brien'!A1").label == "'O''Brien'!A1"


def test_validate_grid_requires_the_range_shape_and_scalar_cells() -> None:
    """Grids must match the range; null (which Graph skips) and non-finite or nested cells are rejected."""
    assert validate_grid([[1, ""], ["x", True]], rows=2, columns=2, field_name="after") == [[1, ""], ["x", True]]
    for bad in ([[1]], [[1, 2]], [[1, 2], [3]], "x", [[1, [2]], [3, 4]], [[math.nan, 1], [1, 1]], [[None, 1], [1, 1]]):
        with pytest.raises(WorkbookArgumentError):
            validate_grid(bad, rows=2, columns=2, field_name="after")


@pytest.mark.parametrize(
    ("value", "converted"),
    [
        ("0042", True),
        ("12%", True),
        ("$1,200.50", True),
        ("(15)", True),
        ("1e3", True),
        ("2026-09-25", True),
        ("9/25/2026", True),
        ("1/2", True),
        ("Jan-15-2016", True),
        ("15 Sep", True),
        ("10:30 PM", True),
        ("1 1/2", True),
        ("TRUE", True),
        ("- bullet", True),
        ("+44 20 7946 0000", True),
        ("@mention", True),
        ("=SUM(A1:A3)", False),
        ("Revenue grows 12% YoY.", False),
        ("555-1234", False),
        ("v1.2", False),
        ("-", False),
        ("", False),
        (12, False),
    ],
)
def test_excel_would_convert_flags_text_excel_parses_as_something_else(value: object, converted: bool) -> None:
    """Numbers, percentages, dates, times, booleans, and formula-like text are flagged; prose and formulas are not."""
    assert excel_would_convert(value) is converted


def test_cells_equal_compares_numbers_numerically_and_keeps_types_apart() -> None:
    """8 equals 8.0, but a boolean never equals a number and a numeric string never equals a number."""
    assert cells_equal(8, 8.0)
    assert cells_equal(0.1 + 0.2, 0.3)
    assert not cells_equal(True, 1)
    assert not cells_equal("8", 8)
    assert cells_equal("=A1*2", "=A1*2")
    assert changed_cell_count([[1, 2]], [[1, 3]]) == 1


def test_parse_edits_bounds_count_cells_and_duplicates() -> None:
    """Edits are bounded, must not repeat a range, and carry grids matching their ranges."""
    edit = {"range": "Assumptions!B4", "before": [[0.08]], "after": [[0.12]]}
    [plan] = parse_edits([edit])
    assert plan.target.address == "B4"
    assert plan.after == [[0.12]]
    with pytest.raises(WorkbookArgumentError, match="non-empty"):
        parse_edits([])
    with pytest.raises(WorkbookArgumentError, match="at most"):
        parse_edits([{**edit, "range": f"Assumptions!A{row}"} for row in range(1, MAX_EDITS + 2)])
    with pytest.raises(WorkbookArgumentError, match="overlaps"):
        parse_edits([edit, {**edit, "range": "assumptions!$B$4"}])
    square = {"range": "Assumptions!A1:B2", "before": [[1, 1], [1, 1]], "after": [[2, 2], [2, 2]]}
    with pytest.raises(WorkbookArgumentError, match="overlaps"):
        parse_edits([square, {**square, "range": "Assumptions!B2:C3"}])
    assert (
        len(parse_edits([square, {**square, "range": "Summary!B2:C3"}, {**square, "range": "Assumptions!C1:D2"}])) == 3
    )
    with pytest.raises(WorkbookArgumentError, match="Excel would convert"):
        parse_edits([{**edit, "after": [["0042"]]}])
    [text_plan] = parse_edits([{**edit, "after": [["0042"]], "number_format": [["@"]]}])
    assert text_plan.number_format == [["@"]]
    wide = {"range": f"Assumptions!A1:A{MAX_EDIT_CELLS + 1}"}
    rows = [[0]] * (MAX_EDIT_CELLS + 1)
    with pytest.raises(WorkbookArgumentError, match=f"at most {MAX_EDIT_CELLS} cells"):
        parse_edits([{**wide, "before": rows, "after": rows}])
    with pytest.raises(WorkbookArgumentError, match="number format"):
        parse_edits([{**edit, "number_format": [[5]]}])


def test_document_ref_rejects_path_characters() -> None:
    """Document IDs cannot smuggle separators, queries, or encodings into a Graph path."""
    assert DocumentRef.parse("b!abc-_.:01X!2").path("workbook") == "/drives/b!abc-_./items/01X!2/workbook"
    for value in ("nocolon", "a/b:c", "a:b/c", "a:b?c", "a%2F:b", ":b", "a:", "a:" + "b" * 257, None):
        with pytest.raises(GraphError) as raised:
            DocumentRef.parse(value)
        assert raised.value.code == "invalid_document_id"


def test_graph_path_encodes_each_segment() -> None:
    """Spaces, slashes, and hashes in a segment cannot change the request path."""
    assert graph_path("me", "drive", "root:", "a b#c/d") == "/me/drive/root%3A/a%20b%23c%2Fd"


def test_share_id_matches_the_documented_encoding_and_rejects_unsafe_urls() -> None:
    """Share IDs use unpadded base64url, and only plain HTTPS links are accepted."""
    assert (
        share_id("https://onedrive.live.com/redir?resid=1231244193912!12&authKey=1201919!12921!1")
        == "u!aHR0cHM6Ly9vbmVkcml2ZS5saXZlLmNvbS9yZWRpcj9yZXNpZD0xMjMxMjQ0MTkzOTEyITEyJmF1dGhLZXk9MTIwMTkxOSExMjkyMSEx"
    )
    for bad in ("http://x.sharepoint.com/a", "https://u:p@x.sharepoint.com/a", "https:///a", "https://x/a b", "", 3):
        with pytest.raises(GraphError) as raised:
            share_id(bad)
        assert raised.value.code == "invalid_url"


def test_graph_errors_carry_codes_and_scrubbed_messages_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Status codes map to safe codes; Graph's code and message survive with URLs scrubbed."""
    graph = FakeGraph().install(monkeypatch)
    graph.overrides[("GET", "/me")] = lambda _request: graph_error(
        429,
        "TooManyRequests",
        "Slow down, see https://secret.example/token=abc",
        headers={"Retry-After": "7"},
    )
    with pytest.raises(GraphError) as raised:
        _run(lambda: graph_json(ALICE_TOKEN, "GET", "/me"))
    assert raised.value.code == "rate_limited"
    assert raised.value.details == {
        "status_code": 429,
        "graph_code": "TooManyRequests",
        "graph_message": "Slow down, see <url>",
        "retry_after_seconds": 7,
    }
    with pytest.raises(GraphAccessRejectedError):
        _run(lambda: graph_json("unknown-token", "GET", "/me/drive"))


def test_graph_client_never_follows_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirect is reported, not followed, so the bearer never reaches another host."""
    graph = FakeGraph().install(monkeypatch)
    graph.overrides[("GET", "/me")] = lambda _request: httpx.Response(302, headers={"Location": "https://evil.test/"})
    with pytest.raises(GraphError) as raised:
        _run(lambda: graph_json(ALICE_TOKEN, "GET", "/me"))
    assert raised.value.code == "redirect_rejected"
    assert len(graph.requests) == 1


def test_graph_client_bounds_response_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body declared larger than the JSON bound is refused without being read."""
    graph = FakeGraph().install(monkeypatch)
    graph.overrides[("GET", "/me")] = lambda _request: httpx.Response(
        200,
        headers={"Content-Length": str(64 * 1024 * 1024)},
        content=b"{}",
    )
    monkeypatch.setattr(microsoft_graph_client, "_MAX_JSON_RESPONSE_BYTES", 1)
    with pytest.raises(GraphError) as raised:
        _run(lambda: graph_json(ALICE_TOKEN, "GET", "/me"))
    assert raised.value.code == "response_too_large"


def test_workbook_outline_lists_sheets_tables_and_visible_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The outline gives used ranges for visible sheets, table ranges, and only visible names."""
    FakeGraph.with_forecast().install(monkeypatch)
    outline = _run(lambda: workbook_outline(ALICE_TOKEN, REF))
    assert outline["worksheets"] == [
        {"name": "Assumptions", "visibility": "Visible", "used_range": "Assumptions!A1:C5", "rows": 5, "columns": 3},
        {
            "name": "Summary Sheet",
            "visibility": "Visible",
            "used_range": "Summary Sheet!A1:B2",
            "rows": 2,
            "columns": 2,
        },
        {"name": "Lookups", "visibility": "Hidden", "used_range": None, "rows": None, "columns": None},
    ]
    assert outline["tables"] == [{"name": "tbl_Inputs", "range": "Assumptions!A1:C5"}]
    assert outline["names"] == [{"name": "GrowthRate", "value": "=Assumptions!$B$4"}]
    assert not outline["worksheets_truncated"]


def test_read_range_returns_formulas_values_and_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ranges resolve their sheet by name, case-insensitively, and return all three grids."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    result = _run(lambda: read_range(ALICE_TOKEN, REF, parse_sheet_range("'summary sheet'!B1:B2")))
    assert result == {
        "range": "'summary sheet'!B1:B2",
        "address": "Summary Sheet!B1:B2",
        "formulas": [["=Model!H40"], ["Revenue grows 8% YoY."]],
        "values": [[0], ["Revenue grows 8% YoY."]],
        "number_format": [["General"], ["General"]],
    }
    assert graph.paths()[-1] == (
        f"/drives/{DRIVE_ID}/items/{ITEM_ID}/workbook/worksheets/{{00000000-0002-0000-0000-000000000000}}"
        "/range(address='B1:B2')"
    )


def test_read_range_refuses_oversized_ranges_and_unknown_sheets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads are bounded before any request, and an unknown sheet lists the real ones."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    with pytest.raises(WorkbookArgumentError, match=str(MAX_READ_CELLS)):
        _run(lambda: read_range(ALICE_TOKEN, REF, parse_sheet_range("Assumptions!A1:Z100")))
    assert graph.requests == []
    with pytest.raises(GraphError) as raised:
        _run(lambda: read_range(ALICE_TOKEN, REF, parse_sheet_range("Nope!A1")))
    assert raised.value.code == "sheet_not_found"
    assert raised.value.details["available_sheets"] == ["Assumptions", "Summary Sheet", "Lookups"]


def _growth_edit(before: Any = 0.08, after: Any = 0.12) -> dict[str, Any]:  # noqa: ANN401
    return {"range": "Assumptions!B4", "before": [[before]], "after": [[after]]}


def test_apply_edits_writes_verifies_and_counts_changed_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matching ranges are written and verified from the write response."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    plans = parse_edits(
        [
            _growth_edit(),
            {
                "range": "'Summary Sheet'!B2",
                "before": [["Revenue grows 8% YoY."]],
                "after": [["Revenue grows 12% YoY."]],
                "number_format": [["@"]],
            },
        ],
    )
    receipt = _run(lambda: apply_edits(ALICE_TOKEN, REF, plans, skip_conflicts=False))
    assert receipt.status == "applied"
    assert receipt.verified
    assert receipt.cells_changed == 2
    workbook = graph.workbooks[(DRIVE_ID, ITEM_ID)]
    assert workbook.sheet("Assumptions").formulas[(4, 2)] == 0.12
    assert workbook.sheet("Summary Sheet").formats[(2, 2)] == "@"
    assert len(graph.paths("PATCH")) == 3  # the number format goes first, in its own write


def test_apply_edits_aborts_on_any_conflict_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A range edited since it was read stops the whole call before any write."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    graph.workbooks[(DRIVE_ID, ITEM_ID)].sheet("Assumptions").set("B4", [[0.11]])
    plans = parse_edits([_growth_edit(), {"range": "Assumptions!B5", "before": [[142]], "after": [[150]]}])
    receipt = _run(lambda: apply_edits(ALICE_TOKEN, REF, plans, skip_conflicts=False))
    assert receipt.status == "conflict"
    assert [edit.as_dict() for edit in receipt.edits] == [
        {"range": "Assumptions!B4", "outcome": "conflict", "cells_changed": 0, "current": [[0.11]]},
        {"range": "Assumptions!B5", "outcome": "not_attempted", "cells_changed": 0},
    ]
    assert graph.paths("PATCH") == []


def test_apply_edits_skip_conflicts_writes_only_unchanged_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checked undo: ranges a human changed afterwards are kept, the rest are written."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    graph.workbooks[(DRIVE_ID, ITEM_ID)].sheet("Assumptions").set("B4", [[0.125]])
    plans = parse_edits([_growth_edit(0.12, 0.08), {"range": "Assumptions!B5", "before": [[142]], "after": [[140]]}])
    receipt = _run(lambda: apply_edits(ALICE_TOKEN, REF, plans, skip_conflicts=True))
    assert receipt.status == "partial"
    assert [edit.outcome for edit in receipt.edits] == ["conflict", "applied"]
    sheet = graph.workbooks[(DRIVE_ID, ITEM_ID)].sheet("Assumptions")
    assert sheet.formulas[(4, 2)] == 0.125
    assert sheet.formulas[(5, 2)] == 140


def test_apply_edits_stops_at_the_first_failed_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed write is reported, and later edits are not attempted."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    locked_path = (
        f"/drives/{DRIVE_ID}/items/{ITEM_ID}/workbook/worksheets/{{00000000-0001-0000-0000-000000000000}}"
        "/range(address='B4')"
    )

    def locked(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return graph_error(423, "resourceLocked", "The resource is locked.")
        return graph._route(request, locked_path)

    graph.overrides[("GET", locked_path)] = locked
    graph.overrides[("PATCH", locked_path)] = locked
    plans = parse_edits([_growth_edit(), {"range": "Assumptions!B5", "before": [[142]], "after": [[150]]}])
    receipt = _run(lambda: apply_edits(ALICE_TOKEN, REF, plans, skip_conflicts=False))
    assert receipt.status == "failed"
    assert receipt.edits[0].outcome == "failed"
    assert receipt.edits[0].error == {
        "code": "locked",
        "message": "Microsoft Graph rejected the request.",
        "status_code": 423,
        "graph_code": "resourceLocked",
        "graph_message": "The resource is locked.",
    }
    assert receipt.edits[0].current == [[0.08]]
    assert receipt.edits[1].outcome == "not_attempted"


def test_apply_edits_treats_a_write_that_landed_despite_an_error_as_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out write is read back, and content equal to after counts as applied."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    path = (
        f"/drives/{DRIVE_ID}/items/{ITEM_ID}/workbook/worksheets/{{00000000-0001-0000-0000-000000000000}}"
        "/range(address='B4')"
    )

    def lands_then_fails(request: httpx.Request) -> httpx.Response:
        response = graph._route(request, path)
        return graph_error(504, "gatewayTimeout", "Timed out.") if request.method == "PATCH" else response

    graph.overrides[("GET", path)] = lands_then_fails
    graph.overrides[("PATCH", path)] = lands_then_fails
    receipt = _run(lambda: apply_edits(ALICE_TOKEN, REF, parse_edits([_growth_edit()]), skip_conflicts=False))
    assert receipt.status == "applied"
    assert receipt.edits[0].verified
    assert receipt.edits[0].error is not None


def test_apply_edits_replay_counts_already_written_ranges_as_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaying an approved call after a crash does not write twice or report a false conflict."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    graph.workbooks[(DRIVE_ID, ITEM_ID)].sheet("Assumptions").set("B4", [[0.12]])
    receipt = _run(lambda: apply_edits(ALICE_TOKEN, REF, parse_edits([_growth_edit()]), skip_conflicts=False))
    assert receipt.status == "applied"
    assert receipt.edits[0].as_dict() == {
        "range": "Assumptions!B4",
        "outcome": "applied",
        "cells_changed": 0,
        "already_applied": True,
        "verified": True,
    }
    assert graph.paths("PATCH") == []


def test_apply_edits_writes_number_formats_before_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Text formats land first so text such as leading zeros survives."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    plans = parse_edits(
        [{"range": "Assumptions!C5", "before": [["HR plan"]], "after": [["0042"]], "number_format": [["@"]]}],
    )
    receipt = _run(lambda: apply_edits(ALICE_TOKEN, REF, plans, skip_conflicts=False))
    assert receipt.status == "applied"
    bodies = [json.loads(request.content) for request in graph.requests if request.method == "PATCH"]
    assert bodies == [{"numberFormat": [["@"]]}, {"formulas": [["0042"]]}]


def test_apply_edits_reports_writes_excel_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Excel stores something other than what was sent, the edit is applied but unverified."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    graph.patch_transform = lambda rows: [[0.12 if cell == "twelve" else cell for cell in row] for row in rows]
    receipt = _run(
        lambda: apply_edits(ALICE_TOKEN, REF, parse_edits([_growth_edit(after="twelve")]), skip_conflicts=False),
    )
    assert receipt.status == "applied"
    assert not receipt.verified
    assert receipt.edits[0].written == [[0.12]]


def test_apply_edits_rejects_unknown_sheets_before_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit naming a missing sheet fails before any range read or write."""
    graph = FakeGraph.with_forecast().install(monkeypatch)
    plans = parse_edits([{"range": "Missing!A1", "before": [[""]], "after": [["x"]]}])
    with pytest.raises(GraphError) as raised:
        _run(lambda: apply_edits(ALICE_TOKEN, REF, plans, skip_conflicts=False))
    assert raised.value.code == "sheet_not_found"
    assert graph.paths() == [f"/drives/{DRIVE_ID}/items/{ITEM_ID}/workbook/worksheets"]


def test_fake_sheet_grid_reads_formats_and_values() -> None:
    """The fake reports formula results as values and defaults number formats."""
    sheet = FakeSheet(id="x", name="S")
    sheet.set("A1", [["=1+1", 5]])
    assert sheet.grid("A1:B1", "values") == [[0, 5]]
    assert sheet.grid("A1:B1", "numberFormat") == [["General", "General"]]
