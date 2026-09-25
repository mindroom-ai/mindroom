"""In-memory Microsoft Graph fake for Microsoft 365 toolkit tests: shares, drive items, and workbooks."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from urllib.parse import unquote

import httpx

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager
from mindroom.custom_tools import microsoft_graph_client
from mindroom.message_target import MessageTarget
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
ALICE_TOKEN = "alice-graph-token"  # noqa: S105
BOB_TOKEN = "bob-graph-token"  # noqa: S105
DRIVE_ID = "b!drive-1"
ITEM_ID = "01ITEM"
DOCUMENT_ID = f"{DRIVE_ID}:{ITEM_ID}"
SHARE_URL = "https://contoso.sharepoint.com/:x:/s/finance/EabcDEF?e=xyz"
WEB_URL = "https://contoso.sharepoint.com/sites/finance/_layouts/15/Doc.aspx?sourcedoc=%7BA1B2%7D&file=Forecast.xlsx&action=default"
ROOM_ID = "!room:example.org"
THREAD_ID = "$thread"

_CELL = re.compile(r"\$?([A-Z]{1,3})\$?([0-9]{1,7})")


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _column_letters(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _bounds(address: str) -> tuple[int, int, int, int]:
    start, _, end = address.partition(":")
    start_match = _CELL.fullmatch(start)
    end_match = _CELL.fullmatch(end or start)
    assert start_match is not None
    assert end_match is not None
    return (
        int(start_match.group(2)),
        _column_number(start_match.group(1)),
        int(end_match.group(2)),
        _column_number(end_match.group(1)),
    )


@dataclass
class FakeSheet:
    """One worksheet: formulas and number formats keyed by (row, column)."""

    id: str
    name: str
    visibility: str = "Visible"
    formulas: dict[tuple[int, int], Any] = field(default_factory=dict)
    formats: dict[tuple[int, int], str] = field(default_factory=dict)

    def set(self, address: str, rows: list[list[Any]]) -> None:
        """Write a grid of formulas starting at the address's top-left cell."""
        top, left, _bottom, _right = _bounds(address)
        for row_offset, row in enumerate(rows):
            for column_offset, value in enumerate(row):
                self.formulas[(top + row_offset, left + column_offset)] = value

    def grid(self, address: str, source: str = "formulas") -> list[list[Any]]:
        """Return one rectangular grid of formulas, values, or number formats."""
        top, left, bottom, right = _bounds(address)
        result = []
        for row in range(top, bottom + 1):
            cells = []
            for column in range(left, right + 1):
                if source == "numberFormat":
                    cells.append(self.formats.get((row, column), "General"))
                    continue
                value = self.formulas.get((row, column), "")
                if source == "values" and isinstance(value, str) and value.startswith("="):
                    value = 0
                cells.append(value)
            result.append(cells)
        return result

    def used_address(self) -> str:
        """Return the used range as Graph reports it."""
        if not self.formulas:
            return f"{self.name}!A1"
        rows = [row for row, _ in self.formulas]
        columns = [column for _, column in self.formulas]
        start = f"{_column_letters(min(columns))}{min(rows)}"
        end = f"{_column_letters(max(columns))}{max(rows)}"
        return f"{self.name}!{start}:{end}"


@dataclass
class FakeWorkbook:
    """Worksheets, tables, and names for one drive item."""

    sheets: list[FakeSheet] = field(default_factory=list)
    tables: list[dict[str, str]] = field(default_factory=list)
    names: list[dict[str, Any]] = field(default_factory=list)

    def sheet(self, name: str) -> FakeSheet:
        """Return a worksheet by exact name."""
        return next(sheet for sheet in self.sheets if sheet.name == name)


def forecast_workbook() -> FakeWorkbook:
    """Return the forecast workbook used across the toolkit tests."""
    assumptions = FakeSheet(id="{00000000-0001-0000-0000-000000000000}", name="Assumptions")
    assumptions.set(
        "A1",
        [
            ["Assumption", "Value", "Source"],
            ["Price increase", 0.03, "Pricing memo"],
            ["Churn", 0.065, "Q2 actuals"],
            ["Growth", 0.08, "Sam"],
            ["Headcount", 142, "HR plan"],
        ],
    )
    assumptions.formats[(4, 2)] = "0%"
    summary = FakeSheet(id="{00000000-0002-0000-0000-000000000000}", name="Summary Sheet")
    summary.set("A1", [["Revenue", "=Model!H40"], ["Narrative", "Revenue grows 8% YoY."]])
    hidden = FakeSheet(id="{00000000-0003-0000-0000-000000000000}", name="Lookups", visibility="Hidden")
    return FakeWorkbook(
        sheets=[assumptions, summary, hidden],
        tables=[{"id": "{T1}", "name": "tbl_Inputs", "address": "Assumptions!A1:C5"}],
        names=[
            {"name": "GrowthRate", "value": "=Assumptions!$B$4", "visible": True},
            {"name": "Secret", "value": "=Lookups!$A$1", "visible": False},
        ],
    )


def drive_item(
    item_id: str = ITEM_ID,
    *,
    name: str = "Forecast.xlsx",
    drive_id: str = DRIVE_ID,
    etag: str = '"{ETAG},1"',
) -> dict[str, Any]:
    """Return a driveItem resource as Graph returns it."""
    return {
        "id": item_id,
        "name": name,
        "webUrl": WEB_URL,
        "webDavUrl": f"https://contoso.sharepoint.com/sites/finance/Shared%20Documents/FY27/{name}",
        "eTag": etag,
        "size": 2048,
        "file": {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        "lastModifiedDateTime": "2026-09-25T10:05:40Z",
        "lastModifiedBy": {"user": {"displayName": "Sam Kim"}},
        "parentReference": {"driveId": drive_id, "path": f"/drives/{drive_id}/root:/FY27"},
    }


def folder_item(item_id: str, *, drive_id: str = DRIVE_ID) -> dict[str, Any]:
    """Return a folder driveItem."""
    return {"id": item_id, "name": item_id, "folder": {"childCount": 0}, "parentReference": {"driveId": drive_id}}


type Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class FakeGraph:
    """Answer the Graph calls the Microsoft 365 toolkit makes, from in-memory state."""

    tokens: set[str] = field(default_factory=lambda: {ALICE_TOKEN, BOB_TOKEN})
    items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    workbooks: dict[tuple[str, str], FakeWorkbook] = field(default_factory=dict)
    shares: dict[str, tuple[str, str]] = field(default_factory=dict)
    me_folders: dict[str, dict[str, Any]] = field(default_factory=dict)
    folder_children: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    overrides: dict[tuple[str, str], Handler] = field(default_factory=dict)
    before_patch: Callable[[FakeSheet, str], None] | None = None
    patch_transform: Callable[[list[list[Any]]], list[list[Any]]] | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    @classmethod
    def with_forecast(cls) -> FakeGraph:
        """Return a fake holding the forecast workbook reachable through SHARE_URL."""
        graph = cls()
        graph.items[(DRIVE_ID, ITEM_ID)] = drive_item()
        graph.workbooks[(DRIVE_ID, ITEM_ID)] = forecast_workbook()
        graph.shares[microsoft_graph_client.share_id(SHARE_URL)] = (DRIVE_ID, ITEM_ID)
        return graph

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeGraph:
        """Route every Graph client request through this fake, keeping the production client settings."""
        monkeypatch.setattr(
            microsoft_graph_client,
            "_new_http_client",
            partial(microsoft_graph_client._new_http_client, transport=httpx.MockTransport(self.handle)),
        )
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Record and answer one request."""
        self.requests.append(request)
        assert request.url.host == "graph.microsoft.com"
        path = unquote(request.url.path.removeprefix("/v1.0"))
        if (override := self.overrides.get((request.method, path))) is not None:
            return override(request)
        if bearer(request) not in self.tokens:
            return graph_error(401, "InvalidAuthenticationToken", "Access token is empty.")
        return self._route(request, path)

    def _route(self, request: httpx.Request, path: str) -> httpx.Response:  # noqa: PLR0911
        if match := re.fullmatch(r"/shares/([^/]+)/driveItem", path):
            key = self.shares.get(match.group(1))
            return httpx.Response(200, json=self.items[key]) if key else graph_error(404, "itemNotFound", "Not found")
        if match := re.fullmatch(r"/drives/([^/]+)/items/([^/]+)", path):
            item = self.items.get((match.group(1), match.group(2)))
            return httpx.Response(200, json=item) if item else graph_error(404, "itemNotFound", "Not found")
        if match := re.fullmatch(r"/drives/([^/]+)/items/([^/]+)/workbook/(.+)", path):
            workbook = self.workbooks.get((match.group(1), match.group(2)))
            if workbook is None:
                return graph_error(404, "itemNotFound", "Not found")
            return self._workbook(request, workbook, match.group(3))
        if match := re.fullmatch(r"/drives/([^/]+)/items/([^/:]+):/(.+):/content", path):
            return self._upload(request, match.group(1), match.group(2), match.group(3))
        if match := re.fullmatch(r"/drives/([^/]+)/items/([^/:]+):/([^/]+)", path):
            existing = self.folder_children.get(match.group(2), {}).get(match.group(3))
            return httpx.Response(200, json=existing) if existing else graph_error(404, "itemNotFound", "Not found")
        if match := re.fullmatch(r"/me/drive/root:/([^/]+)", path):
            folder = self.me_folders.get(match.group(1))
            return httpx.Response(200, json=folder) if folder else graph_error(404, "itemNotFound", "Not found")
        if path == "/me/drive/root/children" and request.method == "POST":
            body = json.loads(request.content)
            if body["name"] in self.me_folders:
                return graph_error(409, "nameAlreadyExists", "The specified item name already exists.")
            self.me_folders[body["name"]] = folder_item(f"FOLDER-{body['name']}")
            return httpx.Response(201, json=self.me_folders[body["name"]])
        return graph_error(404, "invalidRequest", f"no fake route for {request.method} {path}")

    def _workbook(self, request: httpx.Request, workbook: FakeWorkbook, rest: str) -> httpx.Response:  # noqa: C901, PLR0911, PLR0912
        if rest == "worksheets":
            return httpx.Response(
                200,
                json={"value": [{"id": s.id, "name": s.name, "visibility": s.visibility} for s in workbook.sheets]},
            )
        if rest == "tables":
            return httpx.Response(200, json={"value": [{"id": t["id"], "name": t["name"]} for t in workbook.tables]})
        if match := re.fullmatch(r"tables/([^/]+)/range", rest):
            table = next(t for t in workbook.tables if t["id"] == match.group(1))
            return httpx.Response(200, json={"address": table["address"]})
        if rest == "names":
            return httpx.Response(200, json={"value": workbook.names})
        if match := re.fullmatch(r"worksheets/([^/]+)/usedRange\(valuesOnly=true\)", rest):
            sheet = next(s for s in workbook.sheets if s.id == match.group(1))
            address = sheet.used_address()
            top, left, bottom, right = _bounds(address.split("!", 1)[1])
            return httpx.Response(
                200,
                json={"address": address, "rowCount": bottom - top + 1, "columnCount": right - left + 1},
            )
        if match := re.fullmatch(r"worksheets/([^/]+)/range\(address='([A-Z0-9:]+)'\)", rest):
            sheet = next((s for s in workbook.sheets if s.id == match.group(1)), None)
            if sheet is None:
                return graph_error(404, "itemNotFound", "Worksheet not found")
            address = match.group(2)
            if request.method == "PATCH":
                body = json.loads(request.content)
                if self.before_patch is not None:
                    self.before_patch(sheet, address)
                if "formulas" in body:
                    formulas = body["formulas"]
                    sheet.set(address, self.patch_transform(formulas) if self.patch_transform else formulas)
                if "numberFormat" in body:
                    top, left, _bottom, _right = _bounds(address)
                    for row_offset, row in enumerate(body["numberFormat"]):
                        for column_offset, value in enumerate(row):
                            sheet.formats[(top + row_offset, left + column_offset)] = value
            return httpx.Response(
                200,
                json={
                    "address": f"{sheet.name}!{address}",
                    "formulas": sheet.grid(address),
                    "values": sheet.grid(address, "values"),
                    "numberFormat": sheet.grid(address, "numberFormat"),
                },
            )
        return graph_error(404, "invalidRequest", f"no fake workbook route for {rest}")

    def _upload(self, request: httpx.Request, drive_id: str, folder_id: str, name: str) -> httpx.Response:
        children = self.folder_children.setdefault(folder_id, {})
        if name in children:
            if request.url.params.get("@microsoft.graph.conflictBehavior") != "rename":
                return graph_error(409, "nameAlreadyExists", "The specified item name already exists.")
            stem, _, suffix = name.rpartition(".")
            name = f"{stem} 1.{suffix}"
        item_id = f"01NEW{len(self.items)}"
        item = drive_item(item_id, name=name, drive_id=drive_id)
        item["size"] = len(request.content)
        children[name] = item
        self.items[(drive_id, item_id)] = item
        self.workbooks[(drive_id, item_id)] = FakeWorkbook(sheets=[FakeSheet(id="{S1}", name="Sheet1")])
        return httpx.Response(201, json=item)

    def paths(self, method: str | None = None) -> list[str]:
        """Return the decoded paths of recorded requests, optionally for one method."""
        return [
            unquote(request.url.path.removeprefix("/v1.0"))
            for request in self.requests
            if method is None or request.method == method
        ]


def bearer(request: httpx.Request) -> str | None:
    """Return the bearer token a request carried, if any."""
    authorization = request.headers.get("authorization")
    return authorization.removeprefix("Bearer ") if authorization else None


def graph_error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> httpx.Response:
    """Return a Graph error response."""
    return httpx.Response(status, json={"error": {"code": code, "message": message}}, headers=headers)


def runtime_paths(tmp_path: Path, extra_env: dict[str, str] | None = None) -> RuntimePaths:
    """Return runtime paths with a public callback origin."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://chat.example.com", **(extra_env or {})},
    )


def save_client_config(paths: RuntimePaths) -> CredentialsManager:
    """Store one Entra app's client credentials."""
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials(
        "microsoft_365_oauth_client",
        {"client_id": "entra-client", "client_secret": "entra-secret"},
    )
    return manager


def execution_identity(requester_id: str = ALICE) -> ToolExecutionIdentity:
    """Return one requester's tool execution identity for the test agent."""
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="assistant",
        requester_id=requester_id,
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        resolved_thread_id=THREAD_ID,
        session_id=None,
    )


def worker_target(requester_id: str = ALICE) -> ResolvedWorkerTarget:
    """Return the requester-owned target that requester-scoped OAuth credentials resolve to."""
    return resolve_worker_target("user", "assistant", execution_identity=execution_identity(requester_id))


def publish_grant(
    provider: OAuthProvider,
    manager: CredentialsManager,
    token: str,
    *,
    requester_id: str = ALICE,
    **overrides: object,
) -> None:
    """Publish one requester's grant through the real OAuth credential store."""
    publish_oauth_credentials(
        provider,
        {
            "token": token,
            "refresh_token": f"{token}-refresh",
            "client_id": "entra-client",
            "scopes": list(provider.scopes),
            "expires_at": 4_102_444_800.0,
            "_source": "oauth",
            "_oauth_provider": provider.id,
            **overrides,
        },
        credentials_manager=manager,
        worker_target=worker_target(requester_id),
    )


def tool_context(
    paths: RuntimePaths,
    *,
    requester_id: str = ALICE,
    client: MagicMock | None = None,
    config: Config | None = None,
) -> ToolRuntimeContext:
    """Return a thread conversation context for the test agent."""
    return make_test_tool_runtime_context(
        agent_name="assistant",
        target=MessageTarget.resolve(room_id=ROOM_ID, thread_id=THREAD_ID, reply_to_event_id="$request"),
        requester_id=requester_id,
        client=client or MagicMock(),
        config=bind_runtime_paths(config or Config(), paths),
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
