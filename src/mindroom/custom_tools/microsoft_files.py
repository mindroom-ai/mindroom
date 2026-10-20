"""OneDrive and SharePoint file tools that act as the requester through Microsoft Graph."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote, urlsplit

from agno.tools import Toolkit

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.microsoft_graph import (
    GRAPH_URL,
    GraphSession,
    MicrosoftGraphError,
    checked_upload_source,
    graph_error_payload,
    graph_session,
    graph_tool_result,
    next_page_token,
    page_offset,
    page_path,
    path_segment,
    share_id,
)
from mindroom.oauth.microsoft import microsoft_files_oauth_provider

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
_ADDRESS = re.compile(r"\$?[A-Za-z]{1,3}\$?\d{1,7}(?::\$?[A-Za-z]{1,3}\$?\d{1,7})?")
# Characters OneDrive and SharePoint refuse in file names.
_INVALID_NAME_CHARACTERS = frozenset('"*:<>?/\\|')
_INVALID_WORKSHEET_CHARACTERS = frozenset("\\/?*[]:")
_CELL = re.compile(r"(?P<column>[A-Za-z]{1,3})(?P<row>\d{1,7})")
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


def _parse_file(file: object, field_name: str = "file") -> _Item | None:
    """Return the item a Graph path names, None for a link to resolve, or raise for anything else."""
    value = file.strip() if isinstance(file, str) else ""
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
    raw_parent = item.get("parentReference")
    parent: Mapping[str, Any] = raw_parent if isinstance(raw_parent, dict) else {}
    modified_by = item.get("lastModifiedBy")
    raw_user = modified_by.get("user") if isinstance(modified_by, dict) else None
    user: Mapping[str, Any] = raw_user if isinstance(raw_user, dict) else {}
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


def _etag(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = "expected_etag must be the etag of the download or details this edit started from."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return value.strip()


def _limit(value: object, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        msg = f"limit must be an integer from 1 to {maximum}."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return value


def _upload_name_error(name: str) -> str | None:
    if name in {"", ".", ".."} or name != name.strip():
        return "File names must not be empty, '.', '..', or start or end with a space."
    if any(character in _INVALID_NAME_CHARACTERS or ord(character) < 32 for character in name):
        return 'File names must not contain control characters or any of " * : < > ? / \\ |.'
    return None


def _address(value: object) -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value.strip()):
        msg = "address must be a cell or range such as A1 or A1:C10, without a worksheet name."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return value.strip()


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _address_shape(address: str) -> tuple[int, int]:
    """Return the rows and columns an address covers."""
    corners = [_CELL.fullmatch(corner) for corner in address.replace("$", "").split(":")]
    first, last = corners[0], corners[-1]
    if first is None or last is None:
        msg = "address must be a cell or range such as A1 or A1:C10."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    rows = abs(int(last["row"]) - int(first["row"])) + 1
    columns = abs(_column_number(last["column"]) - _column_number(first["column"])) + 1
    return rows, columns


def _checked_range_size(rows: int, columns: int) -> None:
    if rows * columns > _MAX_RANGE_CELLS:
        msg = f"The range covers more than {_MAX_RANGE_CELLS} cells; use a smaller address."
        raise MicrosoftGraphError(code="too_large", message=msg)


def _worksheet(value: object) -> str:
    """Return a worksheet name as an escaped path segment; Excel allows `#` but not these characters."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 31
        # A name of only dots would be read as a relative path segment and reach another endpoint.
        or not value.strip(".")
        or any(character in _INVALID_WORKSHEET_CHARACTERS or ord(character) < 32 for character in value)
    ):
        msg = r"worksheet must be a worksheet name of 1 to 31 characters without \ / ? * [ ] or :."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return quote(value, safe="")


def _cell_count(values: list[Any]) -> int:
    return len(values) * max((len(row) for row in values if isinstance(row, list)), default=0)


def _values(value: object) -> list[list[Any]]:
    invalid = MicrosoftGraphError(
        code="invalid_argument",
        message="values must be a non-empty list of equally long rows of text, numbers, booleans, or nulls.",
    )
    if not isinstance(value, list) or not value or not all(isinstance(row, list) for row in value):
        raise invalid
    rows = cast("list[list[Any]]", value)
    width = len(rows[0])
    if (
        width == 0
        or any(len(row) != width for row in rows)
        or any(cell is not None and not isinstance(cell, str | int | float | bool) for row in rows for cell in row)
    ):
        raise invalid
    return rows


def _range_write(address: object, values: object) -> tuple[str, list[list[Any]]]:
    """Return the checked address and rows of a range write; the rows must have the address's shape."""
    target = _address(address)
    shape = _address_shape(target)
    rows = _values(values)
    if (len(rows), len(rows[0])) != shape:
        msg = f"values have {len(rows)} rows of {len(rows[0])} cells, but {target} covers {shape[0]} by {shape[1]}."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    _checked_range_size(*shape)
    return target, rows


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
        self._provider = microsoft_files_oauth_provider()
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config
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

    def _run(self, operation: Callable[[GraphSession], Mapping[str, object]]) -> str:
        def run() -> Mapping[str, object]:
            with graph_session(
                self._provider,
                self._runtime_paths,
                self._credentials_manager,
                self._worker_target,
                self._config,
            ) as session:
                return operation(session)

        return graph_tool_result(_TOOL_NAME, run)

    @staticmethod
    def _metadata(item: _Item, session: GraphSession) -> dict[str, Any]:
        return session.json("GET", item.path, params={"$select": _ITEM_SELECT})

    def _upload_source(self, local_path: str) -> AuthorizedFile:
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

        def operation(session: GraphSession) -> Mapping[str, object]:
            if not isinstance(query, str) or not query.strip():
                raise MicrosoftGraphError(code="invalid_argument", message="query must not be empty.")
            offset = page_offset(page_token, _MAX_SEARCH_OFFSET)
            request = {
                "entityTypes": ["driveItem"],
                "query": {"queryString": query},
                "from": offset,
                "size": _limit(limit, 50),
            }
            payload = session.json("POST", "/search/query", json={"requests": [request]})
            containers = [
                container
                for answer in payload.get("value") or []
                if isinstance(answer, dict)
                for container in answer.get("hitsContainers") or []
                if isinstance(container, dict)
            ]
            files = [
                _summary(hit["resource"])
                for container in containers
                for hit in container.get("hits") or []
                if isinstance(hit, dict) and isinstance(hit.get("resource"), dict)
            ]
            more = any(container.get("moreResultsAvailable") is True for container in containers)
            return {"files": files, "next_page_token": str(offset + len(files)) if more and files else None}

        return self._call(operation, validate=lambda: page_offset(page_token, _MAX_SEARCH_OFFSET))

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

        def operation(session: GraphSession) -> Mapping[str, object]:
            if page_token is not None:
                payload = session.json("GET", page_path(page_token, "/children"))
            else:
                parent = "/me/drive/root" if folder is None else _resolve(folder, session, "folder").path
                params = {"$top": str(_limit(limit, 200)), "$select": _ITEM_SELECT}
                payload = session.json("GET", f"{parent}/children", params=params)
            entries = [_summary(entry) for entry in payload.get("value") or [] if isinstance(entry, dict)]
            return {"files": entries, "next_page_token": next_page_token(payload)}

        def validate() -> None:
            if page_token is not None:
                page_path(page_token, "/children")
            elif folder is not None:
                _parse_file(folder, "folder")

        return self._call(operation, validate=validate)

    def microsoft_files_get_file(self, file: str) -> str:
        """Get one OneDrive or SharePoint file's details, including the etag microsoft_files_replace_file checks.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.

        """
        return self._call(
            lambda session: {"file": _summary(self._metadata(_resolve(file, session), session))},
            validate=lambda: _parse_file(file),
        )

    def microsoft_files_download_file(self, file: str) -> str:
        """Download one OneDrive or SharePoint file, up to 100 MB, into the workspace's microsoft-files-downloads directory.

        A later download of the same name overwrites that copy, so save any edited version under a new name
        outside microsoft-files-downloads, and pass this result's etag to microsoft_files_replace_file.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.

        """

        def operation(session: GraphSession) -> Mapping[str, object]:
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

        return self._call(operation, validate=lambda: _parse_file(file))

    def microsoft_files_replace_file(self, file: str, local_path: str, expected_etag: str) -> str:
        """Replace one OneDrive or SharePoint file's contents with a workspace file, up to 250 MB.

        Before calling, tell the requester the file's name and location, such as its web_url, because an approval
        prompt may show only IDs. The replace fails with etag_mismatch when someone changed the file after the
        download or details whose etag is passed, so a newer edit is never overwritten; the file's version history
        keeps the replaced version.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.
            local_path: The workspace file to upload.
            expected_etag: The etag from the download or details this edit started from; required.

        """

        def operation(session: GraphSession) -> Mapping[str, object]:
            source = self._upload_source(local_path)
            item = _resolve(file, session)
            if _summary(self._metadata(item, session))["is_folder"]:
                raise MicrosoftGraphError(code="is_folder", message="That item is a folder, not a file.")
            return {
                "file": _summary(
                    session.upload(
                        "PUT",
                        f"{item.path}/content",
                        source,
                        max_bytes=_MAX_UPLOAD_BYTES,
                        headers={"If-Match": expected_etag.strip()},
                    ),
                ),
            }

        return self._call(
            operation,
            validate=lambda: (_parse_file(file), _etag(expected_etag), self._upload_source(local_path)),
        )

    def microsoft_files_upload_file(self, local_path: str, folder: str | None = None, name: str | None = None) -> str:
        """Upload a workspace file, up to 250 MB, as a new OneDrive or SharePoint file; it never overwrites a file.

        Before calling, tell the requester the new file's name and target folder, because an approval prompt may
        show only IDs.

        Args:
            local_path: The workspace file to upload.
            folder: Graph item path or OneDrive or SharePoint link of the target folder; omit for the requester's
                OneDrive root.
            name: The new file's name; defaults to the local file's name.

        """
        try:
            if folder is not None:
                _parse_file(folder, "folder")
            source = self._upload_source(local_path)
        except MicrosoftGraphError as exc:
            return graph_error_payload(_TOOL_NAME, exc)
        file_name = source.name if name is None else name
        if error := _upload_name_error(file_name):
            return graph_error_payload(_TOOL_NAME, MicrosoftGraphError(code="invalid_name", message=error))

        def operation(session: GraphSession) -> Mapping[str, object]:
            parent = "/me/drive/root" if folder is None else _resolve(folder, session, "folder").path
            path = f"{parent}:/{quote(file_name, safe='')}:/content?@microsoft.graph.conflictBehavior=fail"
            try:
                result = session.upload("PUT", path, source, max_bytes=_MAX_UPLOAD_BYTES)
            except MicrosoftGraphError as exc:
                if exc.code != "conflict":
                    raise
                msg = "A file with that name already exists there; use microsoft_files_replace_file or another name."
                raise MicrosoftGraphError(code="name_conflict", message=msg) from None
            return {"file": _summary(result)}

        return self._run(operation)

    def microsoft_files_excel_list_worksheets(self, file: str) -> str:
        """List the worksheets in one Excel workbook stored in OneDrive or SharePoint.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.

        """

        def operation(session: GraphSession) -> Mapping[str, object]:
            item = _resolve(file, session)
            payload = session.json(
                "GET",
                f"{item.path}/workbook/worksheets",
                params={"$select": "name,position,visibility"},
            )
            sheets = [
                {"name": sheet.get("name"), "position": sheet.get("position"), "visibility": sheet.get("visibility")}
                for sheet in payload.get("value") or []
                if isinstance(sheet, dict)
            ]
            return {"worksheets": sheets}

        return self._call(operation, validate=lambda: _parse_file(file))

    def microsoft_files_excel_read_range(self, file: str, worksheet: str, address: str | None = None) -> str:
        """Read cell values from one worksheet of an Excel workbook, up to 10,000 cells.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.
            worksheet: The worksheet name.
            address: A cell or range such as A1:C10; omit to read the worksheet's used range.

        """

        def operation(session: GraphSession) -> Mapping[str, object]:
            sheet = _worksheet(worksheet)
            if address is None:
                target = "usedRange(valuesOnly=true)"
            else:
                checked = _address(address)
                _checked_range_size(*_address_shape(checked))
                target = f"range(address='{checked}')"
            item = _resolve(file, session)
            payload = session.json(
                "GET",
                f"{item.path}/workbook/worksheets/{sheet}/{target}",
                params={"$select": "address,values,rowCount,columnCount"},
            )
            return {"range": _range(payload)}

        return self._call(operation, validate=lambda: _parse_file(file))

    def microsoft_files_excel_update_range(
        self,
        file: str,
        worksheet: str,
        address: str,
        values: list[list[str | int | float | bool | None]],
    ) -> str:
        """Write cell values into one worksheet range of an Excel workbook, up to 10,000 cells.

        Before calling, tell the requester the workbook, worksheet, and range, because an approval prompt may show
        only IDs.

        Args:
            file: Graph item path `/drives/{drive-id}/items/{item-id}` or a OneDrive or SharePoint link.
            worksheet: The worksheet name.
            address: The range to write, such as A1:C2, matching the shape of values.
            values: Rows of cell values; a string starting with = is written as a formula.

        """
        try:
            sheet = _worksheet(worksheet)
            target, rows = _range_write(address, values)
        except MicrosoftGraphError as exc:
            return graph_error_payload(_TOOL_NAME, exc)

        def operation(session: GraphSession) -> Mapping[str, object]:
            item = _resolve(file, session)
            payload = session.json(
                "PATCH",
                f"{item.path}/workbook/worksheets/{sheet}/range(address='{target}')",
                json={"values": rows},
            )
            return {"range": _range(payload)}

        return self._call(operation, validate=lambda: _parse_file(file))

    def _call(
        self,
        operation: Callable[[GraphSession], Mapping[str, object]],
        *,
        validate: Callable[[], object] | None = None,
    ) -> str:
        """Refuse malformed arguments before any request, then run the operation."""
        if validate is not None:
            try:
                validate()
            except MicrosoftGraphError as exc:
                return graph_error_payload(_TOOL_NAME, exc)
        return self._run(operation)
