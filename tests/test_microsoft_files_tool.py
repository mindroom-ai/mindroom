"""Tests for the OneDrive and SharePoint files toolkit."""
# ruff: noqa: D103, S105, S106

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx
import pytest
from agno.tools.function import Function

from mindroom.custom_tools import microsoft_files
from mindroom.custom_tools.microsoft_files import MicrosoftFilesTools
from mindroom.oauth.microsoft import microsoft_files_oauth_provider
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.declarations import SetupType, ToolFileAccess
from tests.microsoft_graph_test_support import (
    FakeGraph,
    publish_grant,
    runtime_paths,
    save_client_config,
    worker_target,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DRIVE = "b!Drive-1_x"
ITEM = "01ITEMABC"
PARENT = "01PARENT"
ITEM_PATH = f"/drives/{DRIVE}/items/{ITEM}"
ETAG = '"{0A1B},2"'
SHARE_LINK = "https://contoso-my.sharepoint.com/:w:/r/personal/alex/Documents/report.docx?d=w1&web=1"


def _item(*, name: str = "report.docx", size: int = 11, folder: bool = False) -> dict[str, object]:
    item: dict[str, object] = {
        "id": ITEM,
        "name": name,
        "size": size,
        "eTag": ETAG,
        "webUrl": "https://contoso-my.sharepoint.com/personal/alex/Documents/report.docx",
        "lastModifiedDateTime": "2026-10-20T10:00:00Z",
        "lastModifiedBy": {"user": {"displayName": "Alex Example"}},
        "parentReference": {"driveId": DRIVE, "id": PARENT},
    }
    item["folder" if folder else "file"] = {"childCount": 1} if folder else {"mimeType": "application/octet-stream"}
    return item


class _Graph:
    """Answer item metadata, content, children, and writes; record every request."""

    def __init__(self, *, item: dict[str, object] | None = None, content: bytes = b"old content") -> None:
        self.item = item or _item()
        self.content = content
        self.write = httpx.Response(200, json=_item())
        self.answer_first: Callable[[httpx.Request], httpx.Response | None] = lambda _request: None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (answer := self.answer_first(request)) is not None:
            return answer
        path = unquote(request.url.path)
        if request.method == "GET" and path == f"/v1.0{ITEM_PATH}":
            return httpx.Response(200, json=self.item)
        if request.method == "GET" and path == f"/v1.0{ITEM_PATH}/content":
            return httpx.Response(200, content=self.content)
        if request.method in {"PUT", "PATCH"}:
            return self.write
        return httpx.Response(404, json={"error": {"code": "itemNotFound"}})


def _tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    write: bool = True,
) -> tuple[MicrosoftFilesTools, FakeGraph]:
    graph = FakeGraph(handler).install(monkeypatch)
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    publish_grant(microsoft_files_oauth_provider(), manager)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    tools = MicrosoftFilesTools(
        runtime_paths=paths,
        credentials_manager=manager,
        worker_target=worker_target(),
        runtime_config=None,
        tool_output_workspace_root=workspace,
        file_access="workspace",
        write=write,
    )
    return tools, graph


def _payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def _workspace_file(tmp_path: Path, relative: str, content: bytes) -> None:
    path = tmp_path / "workspace" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _description(tools: MicrosoftFilesTools, name: str) -> str:
    """Return the description the model sees; deferred toolkits do not show toolkit instructions."""
    return " ".join(Function.from_callable(getattr(tools, name)).to_dict()["description"].split())


def test_registered_metadata_declares_a_primary_runtime_path_tool() -> None:
    metadata = TOOL_METADATA["microsoft_files"]

    assert metadata.auth_provider == "microsoft_files"
    assert metadata.setup_type == SetupType.OAUTH
    assert metadata.requires_primary_runtime is True
    assert metadata.consumes_workspace_paths is True
    assert metadata.file_access == ToolFileAccess.AGENT
    assert len(metadata.function_names) == 9


def test_write_false_registers_only_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph(), write=False)

    assert set(tools.functions) == {
        "microsoft_files_search",
        "microsoft_files_list_folder",
        "microsoft_files_get_file",
        "microsoft_files_download_file",
        "microsoft_files_excel_list_worksheets",
        "microsoft_files_excel_read_range",
    }


def test_write_true_registers_every_function(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    assert set(tools.functions) == set(TOOL_METADATA["microsoft_files"].function_names)


def test_descriptions_keep_edits_safe_and_writes_legible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    download = _description(tools, "microsoft_files_download_file")
    assert "save any edited version under a new name outside microsoft-files-downloads" in download
    assert "pass this result's etag to microsoft_files_replace_file" in download
    for name in ("microsoft_files_replace_file", "microsoft_files_upload_file", "microsoft_files_excel_update_range"):
        assert "Before calling, tell the requester" in _description(tools, name)


def test_item_paths_with_or_without_the_graph_prefix_need_no_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.microsoft_files_get_file(ITEM_PATH))["file"]["item_id"] == ITEM
    assert _payload(tools.microsoft_files_get_file(f"https://graph.microsoft.com/v1.0{ITEM_PATH}"))["file"]["item_id"]
    assert graph.calls() == [f"GET graph.microsoft.com/v1.0{ITEM_PATH}"] * 2


def test_a_sharing_link_resolves_through_shares(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"id": ITEM, "parentReference": {"driveId": DRIVE}})
        if "/shares/u!" in request.url.path
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_get_file(SHARE_LINK))

    assert (result["file"]["drive_id"], result["file"]["item_id"]) == (DRIVE, ITEM)
    assert graph.calls()[0].endswith("/driveItem")
    # Redeeming the link grants the lasting access the direct item requests that follow need.
    assert graph.requests[0].headers["prefer"] == "redeemSharingLink"


@pytest.mark.parametrize(
    "file",
    [
        "https://evil.example.com/report.docx",
        "http://contoso.sharepoint.com/sites/x/report.docx",
        "https://user@contoso.sharepoint.com/sites/x/report.docx",
        "/drives/a/b/items/c",
        "/drives/../items/1",
        "report.docx",
        "",
    ],
)
def test_a_file_that_is_not_a_graph_item_or_link_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.microsoft_files_get_file(file))["code"] == "invalid_file"
    assert graph.requests == []


def test_get_file_summarizes_the_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.microsoft_files_get_file(ITEM_PATH))["file"] == {
        "drive_id": DRIVE,
        "item_id": ITEM,
        "path": ITEM_PATH,
        "name": "report.docx",
        "size": 11,
        "etag": ETAG,
        "web_url": "https://contoso-my.sharepoint.com/personal/alex/Documents/report.docx",
        "last_modified": "2026-10-20T10:00:00Z",
        "last_modified_by": "Alex Example",
        "parent": f"/drives/{DRIVE}/items/{PARENT}",
        "is_folder": False,
    }


def test_search_asks_microsoft_search_for_drive_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(
            200,
            json={
                "value": [
                    {
                        "hitsContainers": [
                            {"hits": [{"resource": _item()}], "moreResultsAvailable": True},
                        ],
                    },
                ],
            },
        )
        if request.url.path == "/v1.0/search/query"
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_search("quarterly plan", limit=5))

    assert [file["path"] for file in result["files"]] == [ITEM_PATH]
    assert result["next_page_token"] == "1"
    [request] = graph.requests
    assert json.loads(request.content) == {
        "requests": [
            {"entityTypes": ["driveItem"], "query": {"queryString": "quarterly plan"}, "from": 0, "size": 5},
        ],
    }


def test_list_folder_defaults_to_the_requester_onedrive_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(
            200,
            json={
                "value": [_item(name="Reports", folder=True)],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/drive/root/children?$top=50&$skiptoken=abc",
            },
        )
        if request.url.path == "/v1.0/me/drive/root/children"
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_list_folder())

    assert [(file["name"], file["is_folder"]) for file in result["files"]] == [("Reports", True)]
    assert result["next_page_token"] == "/me/drive/root/children?$top=50&$skiptoken=abc"
    assert graph.requests[0].url.params["$top"] == "50"


def test_search_continues_from_the_page_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"value": [{"hitsContainers": [{"hits": [], "moreResultsAvailable": False}]}]})
        if request.url.path == "/v1.0/search/query"
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_search("quarterly plan", limit=5, page_token="5"))

    assert result["next_page_token"] is None
    assert json.loads(graph.requests[0].content)["requests"][0]["from"] == 5


def test_list_folder_follows_its_own_page_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"value": [_item()]}) if request.url.path == "/v1.0/me/drive/root/children" else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_list_folder(page_token="/me/drive/root/children?$top=50&$skiptoken=abc"))

    assert result["next_page_token"] is None
    assert graph.requests[0].url.params["$skiptoken"] == "abc"


@pytest.mark.parametrize(
    "page_token",
    [
        "/me/messages?$skiptoken=abc",
        "https://evil.example.com/me/drive/root/children?$skiptoken=abc",
        "/me/drive/root/children/../../../me/messages?x=1",
        "/me/drive/root/childrenX?$skiptoken=abc",
        "",
    ],
)
def test_list_folder_refuses_a_page_token_for_another_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_token: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.microsoft_files_list_folder(page_token=page_token))["code"] == "invalid_page_token"
    assert graph.requests == []


@pytest.mark.parametrize("page_token", ["-1", "abc", "1.5", "100000"])
def test_search_refuses_a_malformed_page_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_token: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.microsoft_files_search("plan", page_token=page_token))["code"] == "invalid_page_token"
    assert graph.requests == []


def test_download_writes_into_the_download_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.microsoft_files_download_file(ITEM_PATH))

    target = tmp_path / "workspace" / "microsoft-files-downloads" / "report.docx"
    assert target.read_bytes() == b"old content"
    assert result["path"] == str(target)
    assert result["file"]["etag"] == ETAG


def test_download_refuses_a_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph(item=_item(name="Reports", folder=True)))

    assert _payload(tools.microsoft_files_download_file(ITEM_PATH))["code"] == "is_folder"
    assert len(graph.requests) == 1


def test_download_checks_the_size_before_fetching_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph(item=_item(size=microsoft_files._MAX_DOWNLOAD_BYTES + 1)))

    assert _payload(tools.microsoft_files_download_file(ITEM_PATH))["code"] == "too_large"
    assert len(graph.requests) == 1


def test_replace_sends_the_etag_and_streams_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "edited/report.docx", b"new content")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.microsoft_files_replace_file(ITEM_PATH, "edited/report.docx", expected_etag=ETAG))

    put = graph.requests[-1]
    assert (put.method, put.url.path) == ("PUT", f"/v1.0{ITEM_PATH}/content")
    assert put.headers["if-match"] == ETAG
    assert put.content == b"new content"
    assert result["file"]["item_id"] == ITEM


def test_replace_requires_the_etag_the_edit_started_from(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    parameter = inspect.signature(tools.microsoft_files_replace_file).parameters["expected_etag"]
    assert parameter.default is inspect.Parameter.empty
    assert _payload(tools.microsoft_files_replace_file(ITEM_PATH, "report.docx", " "))["code"] == "invalid_argument"
    assert graph.requests == []


@pytest.mark.parametrize(("status", "code"), [(412, "etag_mismatch"), (423, "locked")])
def test_replace_failures_keep_their_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    files = _Graph()
    files.write = httpx.Response(status, json={"error": {"code": "x"}})
    tools, _graph = _tools(tmp_path, monkeypatch, files)

    assert _payload(tools.microsoft_files_replace_file(ITEM_PATH, "report.docx", expected_etag=ETAG))["code"] == code


def test_replace_refuses_a_folder_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph(item=_item(name="Reports", folder=True)))

    assert _payload(tools.microsoft_files_replace_file(ITEM_PATH, "report.docx", ETAG))["code"] == "is_folder"
    assert [request.method for request in graph.requests] == ["GET"]


def test_replace_refuses_a_local_path_outside_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "secret.txt").write_bytes(b"secret")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert (
        _payload(tools.microsoft_files_replace_file(ITEM_PATH, "../secret.txt", ETAG))["code"] == "invalid_local_path"
    )
    assert graph.requests == []


def test_upload_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    files = _Graph()
    files.write = httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_upload_file("report.docx", folder=f"/drives/{DRIVE}/items/{PARENT}"))

    assert result["code"] == "name_conflict"
    put = graph.requests[-1]
    assert put.url.path == f"/v1.0/drives/{DRIVE}/items/{PARENT}:/report.docx:/content"
    assert put.url.params["@microsoft.graph.conflictBehavior"] == "fail"


def test_upload_to_the_onedrive_root_quotes_the_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    files = _Graph()
    files.write = httpx.Response(201, json=_item(name="Q3 plan #1.docx"))
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_upload_file("report.docx", name="Q3 plan #1.docx"))

    assert graph.requests[-1].url.raw_path.split(b"?")[0] == b"/v1.0/me/drive/root:/Q3%20plan%20%231.docx:/content"
    assert result["file"]["name"] == "Q3 plan #1.docx"


@pytest.mark.parametrize("name", ["a/b.docx", "a\\b.docx", "..", " lead.docx", "trail.docx ", "x?.docx", "", "a\nb"])
def test_upload_refuses_names_onedrive_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.microsoft_files_upload_file("report.docx", name=name))["code"] == "invalid_name"
    assert graph.requests == []


def test_excel_list_worksheets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"value": [{"id": "{1}", "name": "Budget", "position": 0, "visibility": "Visible"}]})
        if request.url.path == f"/v1.0{ITEM_PATH}/workbook/worksheets"
        else None
    )
    tools, _graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_excel_list_worksheets(ITEM_PATH))

    assert result["worksheets"] == [{"name": "Budget", "position": 0, "visibility": "Visible"}]


def test_excel_read_range_escapes_the_worksheet_and_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"address": "'Q3 Budget'!A1:B2", "values": [[1, 2], [3, 4]], "rowCount": 2})
        if "/workbook/worksheets/" in request.url.path
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_excel_read_range(ITEM_PATH, "Q3 Budget", "A1:B2"))

    assert result["range"]["values"] == [[1, 2], [3, 4]]
    assert (
        unquote(graph.requests[0].url.path) == f"/v1.0{ITEM_PATH}/workbook/worksheets/Q3 Budget/range(address='A1:B2')"
    )


def test_excel_read_without_an_address_reads_the_used_range(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"address": "Budget!A1:A1", "values": [["x"]], "rowCount": 1})
        if "/workbook/worksheets/" in request.url.path
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    tools.microsoft_files_excel_read_range(ITEM_PATH, "Budget")

    assert graph.requests[0].url.path.endswith("/usedRange(valuesOnly=true)")


def test_excel_read_refuses_a_range_over_the_cell_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    big = [[0] * 200 for _ in range(100)]
    files.answer_first = lambda request: (
        httpx.Response(200, json={"address": "A1:GR100", "values": big, "rowCount": 100, "columnCount": 200})
        if "/workbook/worksheets/" in request.url.path
        else None
    )
    tools, _graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_excel_read_range(ITEM_PATH, "Budget"))

    assert (result["code"], result["address"]) == ("too_large", "A1:GR100")


def test_excel_update_range_patches_the_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.write = httpx.Response(200, json={"address": "Budget!A1:B1", "values": [["x", 2]]})
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_excel_update_range(ITEM_PATH, "Budget", "A1:B1", [["x", 2]]))

    patch = graph.requests[-1]
    assert patch.method == "PATCH"
    assert json.loads(patch.content) == {"values": [["x", 2]]}
    assert result["range"]["address"] == "Budget!A1:B1"


@pytest.mark.parametrize(
    ("address", "values"),
    [
        ("A1:B1", [["x"], ["y", "z"]]),
        ("A1:B1", []),
        ("A1:B1", "x"),
        ("A1;DROP", [["x"]]),
        ("Sheet2!A1", [["x"]]),
        ("A1:A20000", [[0]]),
        ("A1:B2", [["x", "y"]]),
    ],
)
def test_excel_update_refuses_malformed_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
    values: object,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.microsoft_files_excel_update_range(ITEM_PATH, "Budget", address, values))  # type: ignore[arg-type]

    assert result["code"] == "invalid_argument"
    assert graph.requests == []


def test_excel_worksheet_names_may_contain_a_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"address": "'Budget #1'!A1", "values": [[1]], "rowCount": 1})
        if "/workbook/worksheets/" in request.url.path
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    assert _payload(tools.microsoft_files_excel_read_range(ITEM_PATH, "Budget #1", "A1"))["status"] == "ok"
    assert "/worksheets/Budget%20%231/" in graph.requests[0].url.raw_path.decode()


@pytest.mark.parametrize("worksheet", ["", ".", "..", "a/b", "a:b", "a?b", "a*b", "a[b", "a]b", "a\\b", "x" * 32])
def test_excel_refuses_impossible_worksheet_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worksheet: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert _payload(tools.microsoft_files_excel_read_range(ITEM_PATH, worksheet, "A1"))["code"] == "invalid_argument"
    assert graph.requests == []


def test_excel_update_accepts_values_matching_the_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.write = httpx.Response(200, json={"address": "Budget!B2:C3", "values": [[1, 2], [3, 4]]})
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _payload(tools.microsoft_files_excel_update_range(ITEM_PATH, "Budget", "$B$2:C3", [[1, 2], [3, 4]]))

    assert result["status"] == "ok"
    assert json.loads(graph.requests[-1].content) == {"values": [[1, 2], [3, 4]]}


def test_excel_update_refuses_a_range_over_the_cell_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = _payload(tools.microsoft_files_excel_update_range(ITEM_PATH, "Budget", "A1:CV101", [[0] * 100] * 101))

    assert result["code"] == "too_large"
    assert graph.requests == []
