"""OneDrive and SharePoint file tools that act as the requester through Microsoft Graph."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote, urlsplit

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.credentials.manager import CredentialsManager  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.microsoft_graph import (
    GRAPH_URL,
    GraphSession,
    MicrosoftGraphError,
    MicrosoftGraphToolkit,
    graph_call,
    graph_error_payload,
    graph_int,
    graph_object,
    graph_records,
    graph_upload_errors,
    invalid_action,
    next_page_token,
    page_offset,
    page_path,
    path_segment,
    require_arg,
    required_text,
    share_id,
)
from mindroom.file_access import checked_upload_source

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.config.models import FileAccess
    from mindroom.constants import RuntimePaths
    from mindroom.file_access import AuthorizedFile
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

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
# Characters OneDrive and SharePoint refuse in file names.
_INVALID_NAME_CHARACTERS = frozenset('"*:<>?/\\|')
_INVALID_WORKSHEET_CHARACTERS = frozenset("\\/?*[]:")
_INVALID_FILE = "file must be a Graph item path /drives/{drive-id}/items/{item-id} or a OneDrive or SharePoint link."
_ReadAction = Literal[
    "search",
    "list_folder",
    "get_file",
    "download_file",
    "excel_list_worksheets",
    "excel_read_range",
]
_WriteAction = Literal["replace_file", "upload_file", "excel_update_range"]


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


def _parse_file(file: str | None, field_name: str = "file") -> _Item | None:
    """Return the item a Graph path names, None for a link to resolve, or raise for anything else."""
    if not isinstance(file, str):
        raise MicrosoftGraphError(code="invalid_file", message=_INVALID_FILE.replace("file", field_name, 1))
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


def _resolve(file: str | None, session: GraphSession, field_name: str = "file") -> _Item:
    if (item := _parse_file(file, field_name)) is not None:
        return item
    assert isinstance(file, str)
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


def _etag(value: str | None) -> str:
    if value is None or not value.strip():
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


def _address(value: str | None) -> tuple[str, tuple[int, int]]:
    """Return a cell or range address and the rows and columns it covers."""
    address = value.strip() if isinstance(value, str) else ""
    if not (match := _ADDRESS.fullmatch(address)):
        msg = "address must be a cell or range such as A1 or A1:C10, without a worksheet name."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    first_column, first_row = match["first_column"], match["first_row"]
    last_column, last_row = match["last_column"] or first_column, match["last_row"] or first_row
    rows = abs(int(last_row) - int(first_row)) + 1
    columns = abs(_column_number(last_column) - _column_number(first_column)) + 1
    return address, (rows, columns)


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _checked_range_size(rows: int, columns: int) -> None:
    if rows * columns > _MAX_RANGE_CELLS:
        msg = f"The range covers more than {_MAX_RANGE_CELLS} cells; use a smaller address."
        raise MicrosoftGraphError(code="too_large", message=msg)


def _worksheet(value: str | None) -> str:
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


def _range_write(address: str | None, values: list[list[Any]] | None) -> str:
    """Return the checked address of a range write; the values must be equally long rows of the address's shape."""
    target, shape = _address(address)
    if not values or any(len(row) != len(values[0]) for row in values):
        raise MicrosoftGraphError(
            code="invalid_argument",
            message="values must be a non-empty list of equally long rows of text, numbers, booleans, or nulls.",
        )
    if (len(values), len(values[0])) != shape:
        msg = f"values have {len(values)} rows of {len(values[0])} cells, but {target} covers {shape[0]} by {shape[1]}."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    _checked_range_size(*shape)
    return target


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


class MicrosoftFilesTools(MicrosoftGraphToolkit):
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
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        tools: list[Callable[..., str]] = [self.microsoft_files_read]
        if write:
            tools.append(self.microsoft_files_write)
        super().__init__(
            tool_name=_TOOL_NAME,
            runtime_paths=runtime_paths,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
            runtime_config=runtime_config,
            tools=tools,
        )

    @staticmethod
    def _metadata(item: _Item, session: GraphSession) -> dict[str, Any]:
        return session.json("GET", item.path, params={"$select": _ITEM_SELECT})

    def _upload_source(self, local_path: str | None) -> AuthorizedFile:
        path = require_arg(local_path, "local_path")
        with graph_upload_errors():
            return checked_upload_source(
                path,
                workspace_root=self._workspace_root,
                file_access=self._file_access,
                max_bytes=_MAX_UPLOAD_BYTES,
            )

    def _search(self, query: str | None, limit: int, page_token: str | None) -> str:
        def build() -> tuple[int, str]:
            offset = page_offset(page_token, _MAX_SEARCH_OFFSET)
            text = required_text(query, "query")
            graph_int(limit, "limit", maximum=50)
            return offset, text

        def operation(session: GraphSession, checked: tuple[int, str]) -> Mapping[str, object]:
            offset, text = checked
            request = {"entityTypes": ["driveItem"], "query": {"queryString": text}, "from": offset, "size": limit}
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

    def _list_folder(self, folder: str | None, limit: int, page_token: str | None) -> str:
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
                payload = session.json(
                    "GET",
                    f"{parent}/children",
                    params={"$top": str(limit), "$select": _ITEM_SELECT},
                )
            return {
                "files": [_summary(entry) for entry in graph_records(payload)],
                "next_page_token": next_page_token(payload),
            }

        return graph_call(_TOOL_NAME, self._session, build, operation)

    def _download_file(self, file: str | None) -> str:
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

    def _excel_read_range(self, file: str | None, worksheet: str | None, address: str | None) -> str:
        def build() -> str:
            _parse_file(file)
            sheet = _worksheet(worksheet)
            if address is None:
                return f"{sheet}/usedRange(valuesOnly=true)"
            checked, shape = _address(address)
            _checked_range_size(*shape)
            return f"{sheet}/range(address='{checked}')"

        def operation(session: GraphSession, target: str) -> Mapping[str, object]:
            item = _resolve(file, session)
            payload = session.json(
                "GET",
                f"{item.path}/workbook/worksheets/{target}",
                params={"$select": "address,values,rowCount,columnCount"},
            )
            return {"range": _range(payload)}

        return graph_call(_TOOL_NAME, self._session, build, operation)

    def microsoft_files_read(
        self,
        action: _ReadAction,
        query: str | None = None,
        folder: str | None = None,
        file: str | None = None,
        worksheet: str | None = None,
        address: str | None = None,
        limit: int | None = None,
        page_token: str | None = None,
    ) -> str:
        """Search, browse, inspect, download, and read Excel ranges in OneDrive and SharePoint as the requester.

        `file` and `folder` take a Graph item path `/drives/{drive-id}/items/{item-id}` or an https OneDrive/SharePoint link.

        - `search`: search accessible files by name/content (`query`; optional `limit` 1-50 default 25, `page_token`)
        - `list_folder`: list entries in `folder` or OneDrive root if omitted (optional `limit` 1-200 default 50, `page_token`)
        - `get_file`: read details for `file`, including `etag` and `web_url` needed for writes
        - `download_file`: save `file` (<=100 MB) to workspace `microsoft-files-downloads/` (overwrites same name, so
          save edits under a new name outside `microsoft-files-downloads` and pass returned `etag` to `replace_file`)
        - `excel_list_worksheets`: list worksheets of Excel workbook `file`
        - `excel_read_range`: read up to 10,000 cells from `worksheet` of workbook `file` (optional `address` like
          `A1:C10`; omit for used range)
        """
        if action == "search":
            result = self._search(query, 25 if limit is None else limit, page_token)
        elif action == "list_folder":
            result = self._list_folder(folder, 50 if limit is None else limit, page_token)
        elif action == "get_file":
            result = graph_call(
                _TOOL_NAME,
                self._session,
                lambda: _parse_file(file),
                lambda session, _item: {"file": _summary(self._metadata(_resolve(file, session), session))},
            )
        elif action == "download_file":
            result = self._download_file(file)
        elif action == "excel_list_worksheets":
            result = graph_call(
                _TOOL_NAME,
                self._session,
                lambda: _parse_file(file),
                lambda session, _item: {
                    "worksheets": [
                        {
                            "name": sheet.get("name"),
                            "position": sheet.get("position"),
                            "visibility": sheet.get("visibility"),
                        }
                        for sheet in graph_records(
                            session.json(
                                "GET",
                                f"{_resolve(file, session).path}/workbook/worksheets",
                                params={"$select": "name,position,visibility"},
                            ),
                        )
                    ],
                },
            )
        elif action == "excel_read_range":
            result = self._excel_read_range(file, worksheet, address)
        else:
            result = graph_error_payload(_TOOL_NAME, invalid_action(action))
        return result

    def _replace_file(
        self,
        file: str | None,
        local_path: str | None,
        expected_etag: str | None,
        expected_web_url: str | None,
    ) -> str:
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

    def _upload_file(
        self,
        local_path: str | None,
        folder: str | None,
        name: str | None,
        expected_web_url: str | None,
    ) -> str:
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
                msg = (
                    "A file with that name already exists there; use microsoft_files_write with action "
                    "replace_file or another name."
                )
                raise MicrosoftGraphError(code="name_conflict", message=msg) from None
            return {"file": _summary(result)}

        return graph_call(_TOOL_NAME, self._session, build, operation)

    def _excel_update_range(
        self,
        file: str | None,
        worksheet: str | None,
        address: str | None,
        values: list[list[str | int | float | bool | None]] | None,
        expected_web_url: str | None,
    ) -> str:
        def build() -> tuple[str, str]:
            sheet = _worksheet(worksheet)
            target = _range_write(address, values)
            _parse_file(file)
            _web_url(expected_web_url)
            return sheet, target

        def operation(session: GraphSession, checked: tuple[str, str]) -> Mapping[str, object]:
            sheet, target = checked
            item = _resolve(file, session)
            _checked_target(self._metadata(item, session), expected_web_url)
            payload = session.json(
                "PATCH",
                f"{item.path}/workbook/worksheets/{sheet}/range(address='{target}')",
                json={"values": values},
            )
            return {"range": _range(payload)}

        return graph_call(_TOOL_NAME, self._session, build, operation)

    def microsoft_files_write(
        self,
        action: _WriteAction,
        file: str | None = None,
        local_path: str | None = None,
        expected_etag: str | None = None,
        expected_web_url: str | None = None,
        folder: str | None = None,
        name: str | None = None,
        worksheet: str | None = None,
        address: str | None = None,
        values: list[list[str | int | float | bool | None]] | None = None,
    ) -> str:
        """Replace/upload OneDrive and SharePoint files or update Excel ranges as the requester.

        Before calling, tell the requester the target file, folder, or workbook and what will change. Writes fail with
        `target_mismatch` unless `expected_web_url` is the target's `web_url` from `microsoft_files_read` (`get_file` or
        `list_folder`; search results return a different URL form).

        - `replace_file`: replace `file` with workspace `local_path` (<=250 MB; requires `expected_etag` and
          `expected_web_url`); fails with `etag_mismatch` if changed since read; version history keeps prior version
        - `upload_file`: upload workspace `local_path` (<=250 MB) as a new file—never overwrites (`folder` optional,
          omit for OneDrive root; `name` optional; `expected_web_url` required with `folder`)
        - `excel_update_range`: write up to 10,000 cells into `worksheet` `address` of workbook `file` (`values` rows
          matching `address` shape, `=` prefix writes formula; requires `expected_web_url`)
        """
        if action == "replace_file":
            return self._replace_file(file, local_path, expected_etag, expected_web_url)
        if action == "upload_file":
            return self._upload_file(local_path, folder, name, expected_web_url)
        if action == "excel_update_range":
            return self._excel_update_range(file, worksheet, address, values, expected_web_url)
        return graph_error_payload(_TOOL_NAME, invalid_action(action))
