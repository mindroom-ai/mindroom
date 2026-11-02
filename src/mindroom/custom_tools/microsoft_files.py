"""OneDrive and SharePoint file tools that act as the requester through Microsoft Graph."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.microsoft_graph import (
    GRAPH_URL,
    GraphSession,
    MicrosoftGraphError,
    graph_call,
    graph_int,
    graph_object,
    graph_records,
    graph_session,
    graph_upload_errors,
    next_page_token,
    page_offset,
    page_path,
    path_segment,
    required_text,
    share_id,
)
from mindroom.file_access import checked_upload_source
from mindroom.oauth.microsoft import microsoft_oauth_provider
from mindroom.redaction import MAX_CANONICAL_JSON_INTEGER
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.file_access import AuthorizedFile
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_WRITE_FUNCTIONS = frozenset(
    {"microsoft_files_replace_file", "microsoft_files_upload_file", "microsoft_files_excel_update_range"},
)
# Downloads land on the primary's storage volume, so one file must not fill it.
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
# Graph's single-request upload limit.
_MAX_UPLOAD_BYTES = 250 * 1024 * 1024
# Range values come back into the conversation, so one read or write stays small.
_MAX_RANGE_CELLS = 10_000
# Microsoft Search takes the result offset as a 32-bit integer.
_MAX_SEARCH_OFFSET = 2**31 - 1
_DOWNLOAD_DIR = "microsoft-files-downloads"
_TOOL_NAME = "microsoft_files"
_ITEM_SELECT = "id,name,size,eTag,webUrl,lastModifiedDateTime,lastModifiedBy,parentReference,file,folder"
_ITEM_PATH = re.compile(rf"^(?:{re.escape(GRAPH_URL)})?/?drives/(?P<drive>[^/]+)/items/(?P<item>[^/]+)/?$")
_LINK_HOSTS = frozenset({"onedrive.live.com", "1drv.ms"})
_CHILDREN_PAGES = r"/(me/drive|drives/[^/]+)/(root|items/[^/]+)/children"
_ADDRESS = re.compile(
    r"\$?(?P<first_column>[A-Za-z]{1,3})\$?(?P<first_row>\d{1,7})"
    r"(?::\$?(?P<last_column>[A-Za-z]{1,3})\$?(?P<last_row>\d{1,7}))?",
)
_RANGE_SELECT = "address,values,formulas,numberFormat,rowCount,columnCount"
_TEXT_NUMBER_FORMAT = "@"
_NUMBER = re.compile(r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]*)?|[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_TIME = (
    r"[0-9]{1,2}(?::[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?)?\s*[ap]\.?m\.?|[0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?"
)
_NUMERIC_DATE = r"[0-9]{1,4}\s*[-/.]\s*[0-9]{1,2}(?:\s*[-/.]\s*[0-9]{1,4})?|[0-9]{1,2}\s*[-/.]\s*[0-9]{4}"
# Text Excel stores as a date, time, fraction, boolean, or error value, as it does when the text is typed.
_CONVERTED_TEXT = (
    re.compile(rf"(?:{_NUMERIC_DATE})(?:\s+(?:{_TIME}))?", re.IGNORECASE),
    re.compile(rf"[0-9]{{1,2}}[-\s/]{_MONTH}(?:[-\s,/]+[0-9]{{1,4}})?", re.IGNORECASE),
    re.compile(rf"{_MONTH}[-\s,/.]+[0-9]{{1,4}}(?:[-\s,]+[0-9]{{1,4}})?", re.IGNORECASE),
    re.compile(_TIME, re.IGNORECASE),
    re.compile(r"[0-9]+\s+[0-9]+/[0-9]+"),
    re.compile(r"true|false", re.IGNORECASE),
    re.compile(r"#(?:N/A|DIV/0!|VALUE!|REF!|NAME\?|NUM!|NULL!|SPILL!|CALC!|GETTING_DATA)", re.IGNORECASE),
)
# Excel stores an ISO date, optionally with a time, as that date, which is how a date is written on purpose.
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?: [0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?")
# Day zero of Excel's 1900 date system, from which a date's serial number counts days.
_EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=UTC)
# Excel reads text starting with these characters as a formula or a signed number.
_FORMULA_PREFIXES = ("+", "-", "@")
_FORMULA_STRING_LITERAL = re.compile(r'("(?:[^"]|"")*")')
# Characters OneDrive and SharePoint refuse in file names.
_INVALID_NAME_CHARACTERS = frozenset('"*:<>?/\\|')
_INVALID_WORKSHEET_CHARACTERS = frozenset("\\/?*[]:")
_INVALID_FILE = "file must be a Graph item path /drives/{drive-id}/items/{item-id} or a OneDrive or SharePoint link."


@dataclass(frozen=True)
class _Item:
    drive_id: str
    item_id: str

    @property
    def path(self) -> str:
        return f"/drives/{path_segment(self.drive_id, 'drive_id')}/items/{path_segment(self.item_id, 'item_id')}"


def _is_link(value: str) -> bool:
    parts = urlsplit(value)
    host = (parts.hostname or "").casefold()
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and (host.endswith(".sharepoint.com") or host in _LINK_HOSTS)
    )


def _parse_file(file: str, field_name: str = "file") -> _Item | None:
    """Return the item a Graph path names, None for a link to resolve, or raise for anything else."""
    value = file.strip()
    if match := _ITEM_PATH.match(value):
        item = _Item(match["drive"], match["item"])
        try:
            _ = item.path
        except MicrosoftGraphError:
            raise MicrosoftGraphError(
                code="invalid_file",
                message=_INVALID_FILE.replace("file", field_name, 1),
            ) from None
        return item
    if _is_link(value):
        return None
    raise MicrosoftGraphError(code="invalid_file", message=_INVALID_FILE.replace("file", field_name, 1))


def _resolve(file: str, session: GraphSession, field_name: str = "file") -> _Item:
    if (item := _parse_file(file, field_name)) is not None:
        return item
    # Redeeming the link is the same as opening it in a browser; without it, access lasts only for this request.
    shared = session.json(
        "GET",
        f"/shares/{share_id(file.strip())}/driveItem",
        params={"$select": "id,parentReference"},
        headers={"Prefer": "redeemSharingLink"},
    )
    parent = shared.get("parentReference")
    drive_id = parent.get("driveId") if isinstance(parent, dict) else None
    item_id = shared.get("id")
    if not isinstance(drive_id, str) or not isinstance(item_id, str):
        raise MicrosoftGraphError(code="graph_invalid_response", message="Microsoft Graph returned an unexpected item.")
    return _Item(drive_id, item_id)


def _summary(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return the fields a caller needs to identify, check, and replace one item."""
    parent = graph_object(item.get("parentReference"))
    user = graph_object(graph_object(item.get("lastModifiedBy")).get("user"))
    drive_id = parent.get("driveId")
    return {
        "drive_id": drive_id,
        "item_id": item.get("id"),
        "path": f"/drives/{drive_id}/items/{item.get('id')}" if drive_id and item.get("id") else None,
        "name": item.get("name"),
        "size": item.get("size"),
        "etag": item.get("eTag"),
        "web_url": item.get("webUrl"),
        "last_modified": item.get("lastModifiedDateTime"),
        "last_modified_by": user.get("displayName") or user.get("email"),
        "parent": f"/drives/{drive_id}/items/{parent['id']}" if drive_id and parent.get("id") else None,
        "is_folder": "folder" in item,
    }


def _etag(value: str) -> str:
    if not value.strip():
        msg = "expected_etag must be the etag of the download or details this edit started from."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return value.strip()


def _web_url(value: str | None) -> str:
    if value is None or not value.strip():
        msg = "expected_web_url must be the web_url of the file or folder this call writes to."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return value.strip()


def _checked_target(metadata: Mapping[str, Any], expected_web_url: str | None) -> dict[str, Any]:
    """Return the write target's summary, refusing a target that is not the one expected_web_url names."""
    summary = _summary(metadata)
    if summary["web_url"] != _web_url(expected_web_url):
        msg = "The target is not at expected_web_url; tell the requester its real web_url before trying again."
        raise MicrosoftGraphError(code="target_mismatch", message=msg, web_url=summary["web_url"])
    return summary


def _upload_name(name: str) -> str:
    if name in {"", ".", ".."} or name != name.strip():
        msg = "File names must not be empty, '.', '..', or start or end with a space."
        raise MicrosoftGraphError(code="invalid_name", message=msg)
    if any(character in _INVALID_NAME_CHARACTERS or ord(character) < 32 for character in name):
        msg = 'File names must not contain control characters or any of " * : < > ? / \\ |.'
        raise MicrosoftGraphError(code="invalid_name", message=msg)
    return name


def _address(value: str) -> tuple[str, tuple[int, int], tuple[int, int]]:
    """Return a cell or range address, the rows and columns it covers, and its top-left row and column."""
    address = value.strip()
    if not (match := _ADDRESS.fullmatch(address)):
        msg = "address must be a cell or range such as A1 or A1:C10, without a worksheet name."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    first_row, first_column = int(match["first_row"]), _column_number(match["first_column"])
    last_row = int(match["last_row"] or first_row)
    last_column = _column_number(match["last_column"]) if match["last_column"] else first_column
    shape = (abs(last_row - first_row) + 1, abs(last_column - first_column) + 1)
    return address, shape, (min(first_row, last_row), min(first_column, last_column))


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


def _checked_range_size(rows: int, columns: int) -> None:
    if rows * columns > _MAX_RANGE_CELLS:
        msg = f"The range covers more than {_MAX_RANGE_CELLS} cells; use a smaller address."
        raise MicrosoftGraphError(code="too_large", message=msg)


def _worksheet(value: str) -> str:
    """Return a worksheet name as an escaped path segment; Excel allows `#` but not these characters."""
    if (
        not 1 <= len(value) <= 31
        # A name of only dots would be read as a relative path segment and reach another endpoint.
        or not value.strip(".")
        or any(character in _INVALID_WORKSHEET_CHARACTERS or ord(character) < 32 for character in value)
    ):
        msg = r"worksheet must be a worksheet name of 1 to 31 characters without \ / ? * [ ] or :."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return quote(value, safe="")


def _cell_count(values: list[Any]) -> int:
    return len(values) * max((len(row) for row in values if isinstance(row, list)), default=0)


def _checked_grid(field_name: str, grid: list[list[Any]], target: str, shape: tuple[int, int]) -> None:
    """Refuse a grid that is not equally long rows of the address's shape."""
    if not grid or any(len(row) != len(grid[0]) for row in grid):
        msg = f"{field_name} must be a non-empty list of equally long rows."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    if (len(grid), len(grid[0])) != shape:
        msg = (
            f"{field_name} has {len(grid)} rows of {len(grid[0])} cells, but {target} covers {shape[0]} by {shape[1]}."
        )
        raise MicrosoftGraphError(code="invalid_argument", message=msg)


def _at(grid: object, row: int, column: int) -> object:
    """Return one cell of a Graph grid, or None when Graph left it out."""
    cells = grid[row] if isinstance(grid, list) and row < len(grid) else None
    return cells[column] if isinstance(cells, list) and column < len(cells) else None


def _formula_key(formula: str) -> str:
    """Return a formula with case folded outside string literals, since Excel uppercases names and references."""
    return "".join(
        part if index % 2 else part.upper() for index, part in enumerate(_FORMULA_STRING_LITERAL.split(formula))
    )


def _same(left: object, right: object) -> bool:
    """Return whether two cells read from Graph are equal, keeping booleans apart from the numbers 1 and 0."""
    return isinstance(left, bool) == isinstance(right, bool) and left == right


def _iso_serial(value: object) -> float | None:
    """Return the serial number Excel stores for a written ISO date, or None for any other value."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value.strip()):
        return None
    try:
        moment = datetime.fromisoformat(value.strip()).replace(tzinfo=UTC)
    except ValueError:
        return None
    return (moment - _EXCEL_EPOCH) / timedelta(days=1)


def _shown_as_text(value: object) -> bool:
    """Return whether an approval card carries this number as text, as Matrix events hold no floats or huge integers."""
    if isinstance(value, bool):
        return False
    return isinstance(value, float) or (isinstance(value, int) and abs(value) > MAX_CANONICAL_JSON_INTEGER)


def _shown_alike(left: object, right: object) -> bool:
    """Return whether an approval card shows two different cells alike: a number it carries as text and that text."""
    return any(
        _shown_as_text(number) and isinstance(text, str) and str(number) == text
        for number, text in ((left, right), (right, left))
    )


def _stored_as_written(held: object, written: object, *, text_cell: bool) -> bool:
    """Return whether a cell holds what was written, as Excel stores it.

    Excel uppercases a formula's names and references, stores an ISO date as its serial number, and may round a
    number's last digit; a text-formatted cell stores written text exactly.
    """
    if isinstance(written, str) and not text_cell:
        if written.startswith("=") and isinstance(held, str):
            return _formula_key(held) == _formula_key(written)
        if (serial := _iso_serial(written)) is not None:
            # A workbook in the 1904 date system stores another serial, which is reported as a difference.
            if isinstance(held, bool) or not isinstance(held, int | float):
                return False
            return math.isclose(held, serial, rel_tol=0.0, abs_tol=1e-6)
    if isinstance(held, int | float) and isinstance(written, int | float) and not isinstance(held, bool):
        # Only rounding in the last of a double's digits is tolerated, so distinct 15-digit values differ.
        return not isinstance(written, bool) and math.isclose(held, written, rel_tol=1e-15, abs_tol=0.0)
    return _same(held, written)


def _excel_would_convert(value: object) -> bool:
    """Return whether Excel would store this text as a number, date, time, boolean, error, or formula.

    Graph parses a written cell as if it were typed, so `0042` loses its zeros and `1/2` becomes a date.
    This approximates Excel's English parser conservatively; text in a text-formatted cell stays text.
    """
    if not isinstance(value, str) or value.startswith("=") or _iso_serial(value) is not None:
        return False
    text = value.strip()
    unsigned = text[1:-1].strip() if text.startswith("(") and text.endswith(")") else text
    number = unsigned.strip("$€£¥").strip().removesuffix("%").strip().strip("$€£¥").strip()
    return (
        (_NUMBER.fullmatch(number) is not None and any(character.isdigit() for character in number))
        or any(pattern.fullmatch(text) for pattern in _CONVERTED_TEXT)
        or (len(text) > 1 and text.startswith(_FORMULA_PREFIXES))
    )


@dataclass(frozen=True)
class _RangeEdit:
    """One checked range write: the cells it writes, what they held when read, and the formats it sets."""

    worksheet: str
    address: str
    first_cell: tuple[int, int]
    values: list[list[Any]]
    expected_formulas: list[list[Any]]
    number_formats: list[list[str | None]] | None

    @property
    def writes(self) -> list[list[Any]]:
        """Return the values with null for every cell given what it held when read.

        Graph parses every written cell again, so an unchanged cell is left out rather than rewritten, where text
        such as an ISO date would become a date; the approval card shows those cells as unchanged.
        """
        return [
            [None if _same(value, before) else value for value, before in zip(row, read, strict=True)]
            for row, read in zip(self.values, self.expected_formulas, strict=True)
        ]

    def cell(self, row: int, column: int) -> str:
        return f"{_column_letters(self.first_cell[1] + column)}{self.first_cell[0] + row}"

    def _cells(self) -> list[tuple[int, int]]:
        return [(row, column) for row in range(len(self.values)) for column in range(len(self.values[0]))]

    def check_unchanged(self, current: Mapping[str, Any]) -> None:
        """Refuse a range that no longer holds what it held when read, such as after someone edited it."""
        formulas = current.get("formulas")
        if not all(
            _same(_at(formulas, row, column), self.expected_formulas[row][column]) for row, column in self._cells()
        ):
            msg = (
                "The range no longer holds expected_formulas, so nothing was written; someone may have edited it. "
                "Show the requester current_formulas and ask again."
            )
            raise MicrosoftGraphError(code="cells_changed", message=msg, current_formulas=formulas)

    def check_conversions(self, current: Mapping[str, Any]) -> None:
        """Refuse a written cell the approval card shows unchanged, or one Excel would store as something else."""
        writes = self.writes
        for row, column in self._cells():
            value = writes[row][column]
            requested = _at(self.number_formats, row, column)
            number_format = _at(current.get("numberFormat"), row, column) if requested is None else requested
            cell = self.cell(row, column)
            if value is not None and _shown_alike(value, self.expected_formulas[row][column]):
                msg = (
                    f"{cell} would change only between a number and the same text, {value!r}, which the approval "
                    "card shows as unchanged, so nothing was written. Write the type the cell holds, or null."
                )
                raise MicrosoftGraphError(code="ambiguous_change", message=msg, cell=cell)
            if number_format == _TEXT_NUMBER_FORMAT and isinstance(value, str) and value.startswith("="):
                msg = (
                    f"{cell} is formatted as text, so Excel would store the formula {value!r} as text and nothing "
                    "was written. Give that cell's number_formats entry another format, such as General."
                )
                raise MicrosoftGraphError(code="formula_as_text", message=msg, cell=cell)
            if number_format != _TEXT_NUMBER_FORMAT and _excel_would_convert(value):
                msg = (
                    f"Excel would store the text {value!r} in {cell} as a number, date, time, boolean, or formula, "
                    "so nothing was written. Write numbers as JSON numbers and dates as YYYY-MM-DD, or keep the text "
                    f"by setting that cell's number_formats entry to '{_TEXT_NUMBER_FORMAT}'."
                )
                raise MicrosoftGraphError(code="converted_text", message=msg, cell=cell)

    def differences(self, stored: Mapping[str, Any]) -> list[dict[str, object]]:
        """Return the written cells and number formats that the range does not hold as written."""
        differences: list[dict[str, object]] = []
        writes = self.writes
        for row, column in self._cells():
            value = writes[row][column]
            held = _at(stored.get("formulas"), row, column)
            held_format = _at(stored.get("numberFormat"), row, column)
            text_cell = held_format == _TEXT_NUMBER_FORMAT
            if value is not None and not _stored_as_written(held, value, text_cell=text_cell):
                differences.append({"cell": self.cell(row, column), "written": value, "stored": held})
            number_format = _at(self.number_formats, row, column)
            # Excel may change a format code's case, such as general to General.
            if isinstance(number_format, str) and str(held_format).casefold() != number_format.casefold():
                differences.append(
                    {
                        "cell": self.cell(row, column),
                        "number_format": number_format,
                        "stored_number_format": held_format,
                    },
                )
        return differences


def _range(payload: Mapping[str, Any]) -> dict[str, Any]:
    values = payload.get("values")
    if not isinstance(values, list):
        values = []
    if _cell_count(values) > _MAX_RANGE_CELLS:
        msg = f"The range has more than {_MAX_RANGE_CELLS} cells; read a smaller address."
        raise MicrosoftGraphError(code="too_large", message=msg, address=payload.get("address"))
    return {
        "address": payload.get("address"),
        "values": values,
        "formulas": payload.get("formulas"),
        "row_count": payload.get("rowCount", len(values)),
        "column_count": payload.get("columnCount", max((len(row) for row in values), default=0)),
    }


class MicrosoftFilesTools(Toolkit):
    """Search, read, and save OneDrive and SharePoint files, and Excel ranges, as the requester."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        write: bool = True,
    ) -> None:
        if credentials_manager is None:
            msg = "MicrosoftFilesTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._session = partial(
            graph_session,
            microsoft_oauth_provider(_TOOL_NAME),
            runtime_paths,
            credentials_manager,
            worker_target,
            runtime_config,
        )
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        functions: dict[str, Callable[..., str]] = {
            "microsoft_files_search": self.microsoft_files_search,
            "microsoft_files_list_folder": self.microsoft_files_list_folder,
            "microsoft_files_get_file": self.microsoft_files_get_file,
            "microsoft_files_download_file": self.microsoft_files_download_file,
            "microsoft_files_replace_file": self.microsoft_files_replace_file,
            "microsoft_files_upload_file": self.microsoft_files_upload_file,
            "microsoft_files_excel_list_worksheets": self.microsoft_files_excel_list_worksheets,
            "microsoft_files_excel_read_range": self.microsoft_files_excel_read_range,
            "microsoft_files_excel_update_range": self.microsoft_files_excel_update_range,
        }
        super().__init__(
            name=_TOOL_NAME,
            tools=[function for name, function in functions.items() if write or name not in _WRITE_FUNCTIONS],
        )

    @staticmethod
    def _metadata(item: _Item, session: GraphSession) -> dict[str, Any]:
        return session.json("GET", item.path, params={"$select": _ITEM_SELECT})

    def _upload_source(self, local_path: str) -> AuthorizedFile:
        with graph_upload_errors():
            return checked_upload_source(
                local_path,
                workspace_root=self._workspace_root,
                file_access=self._file_access,
                max_bytes=_MAX_UPLOAD_BYTES,
            )

    def microsoft_files_search(self, query: str, limit: int = 25, page_token: str | None = None) -> str:
        """Search the OneDrive and SharePoint files the requester can open, by name and content.

        Args:
            query: Search words, as typed into Microsoft 365 search.
            limit: Maximum number of files to return, from 1 to 50.
            page_token: The next_page_token of the previous call, to continue the same search.

        """

        def build() -> int:
            offset = page_offset(page_token, _MAX_SEARCH_OFFSET)
            required_text(query, "query")
            graph_int(limit, "limit", maximum=50)
            return offset

        def operation(session: GraphSession, offset: int) -> Mapping[str, object]:
            request = {"entityTypes": ["driveItem"], "query": {"queryString": query}, "from": offset, "size": limit}
            payload = session.json("POST", "/search/query", json={"requests": [request]})
            containers = [
                container for answer in graph_records(payload) for container in graph_records(answer, "hitsContainers")
            ]
            files = [
                _summary(hit["resource"])
                for container in containers
                for hit in container.get("hits") or []
                if isinstance(hit, dict) and isinstance(hit.get("resource"), dict)
            ]
            more = any(container.get("moreResultsAvailable") is True for container in containers)
            return {"files": files, "next_page_token": str(offset + len(files)) if more and files else None}

        return graph_call(_TOOL_NAME, self._session, build, operation)

    def microsoft_files_list_folder(
        self,
        folder: str | None = None,
        limit: int = 50,
        page_token: str | None = None,
    ) -> str:
        """List the files and folders in one folder, or in the requester's OneDrive root when no folder is given.

        Args:
            folder: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link of the folder.
            limit: Maximum number of entries to return, from 1 to 200.
            page_token: The next_page_token of the previous call, to continue the same listing; folder and limit
                are then taken from the token.

        """

        def build() -> str | None:
            if page_token is not None:
                return page_path(page_token, _CHILDREN_PAGES)
            if folder is not None:
                _parse_file(folder, "folder")
            graph_int(limit, "limit", maximum=200)
            return None

        def operation(session: GraphSession, next_page: str | None) -> Mapping[str, object]:
            if next_page is not None:
                payload = session.json("GET", next_page)
            else:
                parent = "/me/drive/root" if folder is None else _resolve(folder, session, "folder").path
                params = {"$top": str(limit), "$select": _ITEM_SELECT}
                payload = session.json("GET", f"{parent}/children", params=params)
            entries = [_summary(entry) for entry in graph_records(payload)]
            return {"files": entries, "next_page_token": next_page_token(payload)}

        return graph_call(_TOOL_NAME, self._session, build, operation)

    def microsoft_files_get_file(self, file: str) -> str:
        """Get one OneDrive or SharePoint file's details, including the etag microsoft_files_replace_file checks.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.

        """
        return graph_call(
            _TOOL_NAME,
            self._session,
            lambda: _parse_file(file),
            lambda session, _item: {"file": _summary(self._metadata(_resolve(file, session), session))},
        )

    def microsoft_files_download_file(self, file: str) -> str:
        """Download one OneDrive or SharePoint file, up to 100 MB, into the workspace's microsoft-files-downloads directory.

        A later download of the same name overwrites that copy, so save any edited version under a new name
        outside microsoft-files-downloads, and pass this result's etag to microsoft_files_replace_file.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.

        """

        def operation(session: GraphSession, _item: _Item | None) -> Mapping[str, object]:
            if self._workspace_root is None:
                raise MicrosoftGraphError(code="workspace_required", message="Downloads need an agent workspace.")
            item = _resolve(file, session)
            summary = _summary(self._metadata(item, session))
            if summary["is_folder"]:
                raise MicrosoftGraphError(code="is_folder", message="That item is a folder, not a file.")
            if isinstance(summary["size"], int) and summary["size"] > _MAX_DOWNLOAD_BYTES:
                msg = f"The file is {summary['size']} bytes; downloads are limited to {_MAX_DOWNLOAD_BYTES} bytes."
                raise MicrosoftGraphError(code="too_large", message=msg)
            path = session.save_download(
                f"{item.path}/content",
                workspace_root=self._workspace_root,
                directory=_DOWNLOAD_DIR,
                filename=str(summary["name"] or ""),
                max_bytes=_MAX_DOWNLOAD_BYTES,
            )
            return {"file": summary, "path": str(path)}

        return graph_call(_TOOL_NAME, self._session, lambda: _parse_file(file), operation)

    def microsoft_files_replace_file(
        self,
        file: str,
        local_path: str,
        expected_etag: str,
        expected_web_url: str,
    ) -> str:
        """Replace one OneDrive or SharePoint file's contents with a workspace file, up to 250 MB.

        The replace fails with target_mismatch unless expected_web_url is the file's web_url, and with
        etag_mismatch when someone changed the file after the download or details whose etag is passed, so a newer
        edit is never overwritten; the file's version history keeps the replaced version.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.
            local_path: The workspace file to upload.
            expected_etag: The etag from the download or details this edit started from; required.
            expected_web_url: The file's web_url from the same download or details; required.

        """

        def build() -> tuple[str, AuthorizedFile]:
            _parse_file(file)
            etag = _etag(expected_etag)
            _web_url(expected_web_url)
            return etag, self._upload_source(local_path)

        def operation(session: GraphSession, checked: tuple[str, AuthorizedFile]) -> Mapping[str, object]:
            etag, source = checked
            item = _resolve(file, session)
            if _checked_target(self._metadata(item, session), expected_web_url)["is_folder"]:
                raise MicrosoftGraphError(code="is_folder", message="That item is a folder, not a file.")
            return {
                "file": _summary(
                    session.upload(
                        "PUT",
                        f"{item.path}/content",
                        source,
                        max_bytes=_MAX_UPLOAD_BYTES,
                        headers={"If-Match": etag},
                    ),
                ),
            }

        return graph_call(_TOOL_NAME, self._session, build, operation)

    def microsoft_files_upload_file(
        self,
        local_path: str,
        folder: str | None = None,
        name: str | None = None,
        expected_web_url: str | None = None,
    ) -> str:
        """Upload a workspace file, up to 250 MB, as a new OneDrive or SharePoint file; it never overwrites a file.

        An upload into a folder fails with target_mismatch unless expected_web_url is the folder's web_url.

        Args:
            local_path: The workspace file to upload.
            folder: Graph item path or OneDrive or SharePoint link of the target folder; omit for the requester's
                OneDrive root.
            name: The new file's name; defaults to the local file's name.
            expected_web_url: The folder's web_url as microsoft_files_get_file or microsoft_files_list_folder
                returns it; search results show a different form of it. Required with folder.

        """

        def build() -> tuple[AuthorizedFile, str]:
            if folder is not None:
                _parse_file(folder, "folder")
                _web_url(expected_web_url)
            source = self._upload_source(local_path)
            return source, _upload_name(source.name if name is None else name)

        def operation(session: GraphSession, checked: tuple[AuthorizedFile, str]) -> Mapping[str, object]:
            source, file_name = checked
            parent = "/me/drive/root"
            if folder is not None:
                target = _resolve(folder, session, "folder")
                _checked_target(self._metadata(target, session), expected_web_url)
                parent = target.path
            path = f"{parent}:/{quote(file_name, safe='')}:/content?@microsoft.graph.conflictBehavior=fail"
            try:
                result = session.upload("PUT", path, source, max_bytes=_MAX_UPLOAD_BYTES)
            except MicrosoftGraphError as exc:
                if exc.code != "conflict":
                    raise
                msg = "A file with that name already exists there; use microsoft_files_replace_file or another name."
                raise MicrosoftGraphError(code="name_conflict", message=msg) from None
            return {"file": _summary(result)}

        return graph_call(_TOOL_NAME, self._session, build, operation)

    def microsoft_files_excel_list_worksheets(self, file: str) -> str:
        """List the worksheets in one Excel workbook stored in OneDrive or SharePoint.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.

        """

        def operation(session: GraphSession, _item: _Item | None) -> Mapping[str, object]:
            item = _resolve(file, session)
            payload = session.json(
                "GET",
                f"{item.path}/workbook/worksheets",
                params={"$select": "name,position,visibility"},
            )
            sheets = [
                {"name": sheet.get("name"), "position": sheet.get("position"), "visibility": sheet.get("visibility")}
                for sheet in graph_records(payload)
            ]
            return {"worksheets": sheets}

        return graph_call(_TOOL_NAME, self._session, lambda: _parse_file(file), operation)

    def microsoft_files_excel_read_range(self, file: str, worksheet: str, address: str | None = None) -> str:
        """Read cell values and formulas from one worksheet of an Excel workbook, up to 10,000 cells.

        values holds each cell's raw value, such as a date's serial number; formulas holds a computed cell's
        formula and any other cell's raw value, which microsoft_files_excel_update_range takes as expected_formulas.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.
            worksheet: The worksheet name.
            address: A cell or range such as A1:C10; omit to read the worksheet's used range.

        """

        def build() -> str:
            _parse_file(file)
            sheet = _worksheet(worksheet)
            if address is None:
                return f"{sheet}/usedRange(valuesOnly=true)"
            checked, shape, _first_cell = _address(address)
            _checked_range_size(*shape)
            return f"{sheet}/range(address='{checked}')"

        def operation(session: GraphSession, target: str) -> Mapping[str, object]:
            item = _resolve(file, session)
            payload = session.json(
                "GET",
                f"{item.path}/workbook/worksheets/{target}",
                params={"$select": "address,values,formulas,rowCount,columnCount"},
            )
            return {"range": _range(payload)}

        return graph_call(_TOOL_NAME, self._session, build, operation)

    def microsoft_files_excel_update_range(
        self,
        file: str,
        worksheet: str,
        address: str,
        values: list[list[str | int | float | bool | None]],
        expected_formulas: list[list[str | int | float | bool]],
        expected_web_url: str,
        number_formats: list[list[str | None]] | None = None,
    ) -> str:
        """Write cell values into one worksheet range of an Excel workbook, up to 10,000 cells.

        The write fails with target_mismatch unless expected_web_url is the workbook's web_url.
        It writes nothing and fails with cells_changed when the range no longer holds expected_formulas, such as
        after someone edited it in Excel; show the requester current_formulas and ask again.
        It writes nothing and fails with converted_text for text Excel would store as a number, date, time,
        boolean, or formula, such as 0042 or 1/2; write numbers as JSON numbers and dates as YYYY-MM-DD, or set
        number_formats to @ for cells that keep text. It fails with ambiguous_change for a cell that would change
        only between a number and the same text, which approval cards show alike. It fails with formula_as_text
        for a formula in a text-formatted cell unless number_formats gives that cell another format, such as
        General. The result reports verified false and lists differences when Excel stored a cell differently from
        what was written; tell the requester.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.
            worksheet: The worksheet name.
            address: The range to write, such as A1:C2, matching the shape of values.
            values: Rows of cell values; a string starting with = is written as a formula, "" empties a cell, and
                null leaves a cell unchanged.
            expected_formulas: The range's formulas exactly as microsoft_files_excel_read_range returned them for
                this address, in the same shape as values; cells whose value equals them are not written. Required.
            expected_web_url: The workbook's web_url as microsoft_files_get_file or microsoft_files_list_folder
                returns it; search results show a different form of it. Required.
            number_formats: Optional rows of Excel number formats, such as 0.0% or @ for text, set before the
                values; null keeps a cell's format.

        """

        def build() -> _RangeEdit:
            sheet = _worksheet(worksheet)
            target, shape, first_cell = _address(address)
            _checked_grid("values", values, target, shape)
            _checked_range_size(*shape)
            _checked_grid("expected_formulas", expected_formulas, target, shape)
            if number_formats is not None:
                _checked_grid("number_formats", number_formats, target, shape)
                if any(number_format == "" for row in number_formats for number_format in row):
                    msg = "number_formats entries must be Excel number formats, such as 0.0%, @, or General, or null."
                    raise MicrosoftGraphError(code="invalid_argument", message=msg)
            _parse_file(file)
            _web_url(expected_web_url)
            return _RangeEdit(sheet, target, first_cell, values, expected_formulas, number_formats)

        def operation(session: GraphSession, edit: _RangeEdit) -> Mapping[str, object]:
            item = _resolve(file, session)
            _checked_target(self._metadata(item, session), expected_web_url)
            path = f"{item.path}/workbook/worksheets/{edit.worksheet}/range(address='{edit.address}')"
            current = session.json("GET", path, params={"$select": _RANGE_SELECT})
            edit.check_unchanged(current)
            edit.check_conversions(current)
            if any(number_format is not None for row in edit.number_formats or [] for number_format in row):
                # Formats go first, so a text-formatted cell keeps text such as leading zeros.
                session.json("PATCH", path, json={"numberFormat": edit.number_formats})
            writes = edit.writes
            if any(value is not None for row in writes for value in row):
                session.json("PATCH", path, json={"values": writes})
            # The range is read again rather than trusting the PATCH response to carry every property.
            payload = session.json("GET", path, params={"$select": _RANGE_SELECT})
            differences = edit.differences(payload)
            result: dict[str, object] = {"range": _range(payload), "verified": not differences}
            if differences:
                result["differences"] = differences
            return result

        return graph_call(_TOOL_NAME, self._session, build, operation)
