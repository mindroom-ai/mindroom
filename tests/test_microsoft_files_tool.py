"""Tests for the OneDrive and SharePoint files toolkit."""
# ruff: noqa: D103, S105, S106

from __future__ import annotations

import inspect
import json
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import httpx
import pytest

from mindroom.custom_tools import microsoft_files
from mindroom.custom_tools.microsoft_files import MicrosoftFilesTools
from mindroom.oauth.microsoft import microsoft_oauth_provider
from mindroom.tool_system.declarations import SetupType, ToolFileAccess
from mindroom.tool_system.registry import builtin_tool_registry
from tests.microsoft_graph_test_support import (
    FakeGraph,
    assert_refused_by_call_validation,
    model_description,
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
DOCUMENTS = "https://contoso-my.sharepoint.com/personal/alex/Documents"
WEB_URL = f"{DOCUMENTS}/report.docx"


def _item(*, name: str = "report.docx", size: int = 11, folder: bool = False) -> dict[str, object]:
    item: dict[str, object] = {
        "id": ITEM,
        "name": name,
        "size": size,
        "eTag": ETAG,
        "webUrl": f"{DOCUMENTS}/{name}",
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
        # The worksheet range every Excel request reads or writes, whatever its address.
        self.range_address = "Budget!A1"
        self.formulas: list[list[object]] = [[""]]
        self.formats: list[list[str]] = [["General"]]
        # How Excel stores a written value, such as text it parses as a number.
        self.store: Callable[[object], object] = lambda value: value

    def range_response(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            body = json.loads(request.content)
            if "numberFormat" in body:
                self.formats = [
                    [old if new is None else new for old, new in zip(old_row, new_row, strict=True)]
                    for old_row, new_row in zip(self.formats, body["numberFormat"], strict=True)
                ]
            if "values" in body:
                self.formulas = [
                    [old if new is None else self.store(new) for old, new in zip(old_row, new_row, strict=True)]
                    for old_row, new_row in zip(self.formulas, body["values"], strict=True)
                ]
        payload = {
            "address": self.range_address,
            "formulas": self.formulas,
            "numberFormat": self.formats,
            "values": self.formulas,
        }
        return httpx.Response(200, json=payload)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if (answer := self.answer_first(request)) is not None:
            return answer
        path = unquote(request.url.path)
        if "/workbook/worksheets/" in path and "/range(address=" in path:
            return self.range_response(request)
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
    connected: bool = True,
) -> tuple[MicrosoftFilesTools, FakeGraph]:
    graph = FakeGraph(handler).install(monkeypatch)
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    if connected:
        publish_grant(microsoft_oauth_provider("microsoft_files"), manager)
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


def _workspace_file(tmp_path: Path, relative: str, content: bytes) -> None:
    path = tmp_path / "workspace" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_registered_metadata_declares_a_primary_runtime_path_tool() -> None:
    metadata = builtin_tool_registry().entries["microsoft_files"]

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

    assert set(tools.functions) == set(builtin_tool_registry().entries["microsoft_files"].function_names)


def test_descriptions_keep_edits_safe_and_writes_legible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    download = model_description(tools, "microsoft_files_download_file")
    assert "save any edited version under a new name outside microsoft-files-downloads" in download
    assert "pass this result's etag to microsoft_files_replace_file" in download
    for name in ("microsoft_files_replace_file", "microsoft_files_upload_file", "microsoft_files_excel_update_range"):
        assert "fails with target_mismatch unless expected_web_url is the" in model_description(tools, name)


def test_item_paths_with_or_without_the_graph_prefix_need_no_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert json.loads(tools.microsoft_files_get_file(ITEM_PATH))["file"]["item_id"] == ITEM
    assert json.loads(tools.microsoft_files_get_file(f"https://graph.microsoft.com/v1.0{ITEM_PATH}"))["file"]["item_id"]
    assert graph.calls() == [f"GET graph.microsoft.com/v1.0{ITEM_PATH}"] * 2


def test_a_sharing_link_resolves_through_shares(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"id": ITEM, "parentReference": {"driveId": DRIVE}})
        if "/shares/u!" in request.url.path
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = json.loads(tools.microsoft_files_get_file(SHARE_LINK))

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

    assert json.loads(tools.microsoft_files_get_file(file))["code"] == "invalid_file"
    assert graph.requests == []


@pytest.mark.parametrize(
    ("call", "code"),
    [
        (lambda tools: tools.microsoft_files_list_folder(folder="report.docx"), "invalid_file"),
        (lambda tools: tools.microsoft_files_get_file("report.docx"), "invalid_file"),
        (lambda tools: tools.microsoft_files_download_file("report.docx"), "invalid_file"),
        (
            lambda tools: tools.microsoft_files_replace_file("report.docx", "report.docx", "etag", WEB_URL),
            "invalid_file",
        ),
        (
            lambda tools: tools.microsoft_files_upload_file(
                "report.docx",
                folder="report.docx",
                expected_web_url=WEB_URL,
            ),
            "invalid_file",
        ),
        (lambda tools: tools.microsoft_files_excel_list_worksheets("report.docx"), "invalid_file"),
        (lambda tools: tools.microsoft_files_excel_read_range("report.docx", "Budget"), "invalid_file"),
        (
            lambda tools: tools.microsoft_files_excel_update_range(
                "report.docx",
                "Budget",
                "A1",
                [[1]],
                [[""]],
                WEB_URL,
            ),
            "invalid_file",
        ),
        (lambda tools: tools.microsoft_files_search(" "), "invalid_argument"),
        (lambda tools: tools.microsoft_files_search("plan", limit=51), "invalid_argument"),
        # A sharing link is redeemed for lasting access, so a refused listing must not reach Graph.
        (lambda tools: tools.microsoft_files_list_folder(SHARE_LINK, limit=0), "invalid_argument"),
        (lambda tools: tools.microsoft_files_list_folder(SHARE_LINK, limit=201), "invalid_argument"),
        (lambda tools: tools.microsoft_files_excel_read_range(ITEM_PATH, "Budget", "A1:"), "invalid_argument"),
        (lambda tools: tools.microsoft_files_excel_read_range(ITEM_PATH, "Budget", "A1:CV101"), "too_large"),
        (
            lambda tools: tools.microsoft_files_excel_update_range(
                ITEM_PATH,
                "Budget",
                "A1:CV101",
                [[0] * 100] * 101,
                [[""] * 100] * 101,
                WEB_URL,
            ),
            "too_large",
        ),
    ],
)
def test_a_malformed_argument_is_refused_before_asking_for_a_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[MicrosoftFilesTools], str],
    code: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph(), connected=False)

    assert json.loads(call(tools))["code"] == code
    assert graph.requests == []


@pytest.mark.parametrize("file", [ITEM_PATH, f" {ITEM_PATH} "])
def test_get_file_summarizes_the_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file: str) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    assert json.loads(tools.microsoft_files_get_file(file))["file"] == {
        "drive_id": DRIVE,
        "item_id": ITEM,
        "path": ITEM_PATH,
        "name": "report.docx",
        "size": 11,
        "etag": ETAG,
        "web_url": WEB_URL,
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

    result = json.loads(tools.microsoft_files_search("quarterly plan", limit=5))

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

    result = json.loads(tools.microsoft_files_list_folder())

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

    result = json.loads(tools.microsoft_files_search("quarterly plan", limit=5, page_token="5"))

    assert result["next_page_token"] is None
    assert json.loads(graph.requests[0].content)["requests"][0]["from"] == 5


def test_search_continues_past_the_first_thousand_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(
            200,
            json={"value": [{"hitsContainers": [{"hits": [{"resource": _item()}], "moreResultsAvailable": True}]}]},
        )
        if request.url.path == "/v1.0/search/query"
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = json.loads(tools.microsoft_files_search("plan", limit=50, page_token="1050"))

    assert json.loads(graph.requests[0].content)["requests"][0]["from"] == 1050
    assert result["next_page_token"] == "1051"


@pytest.mark.parametrize("folder_path", ["/me/drive/root", ITEM_PATH])
def test_list_folder_follows_its_own_page_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    folder_path: str,
) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"value": [_item()]}) if request.url.path == f"/v1.0{folder_path}/children" else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = json.loads(tools.microsoft_files_list_folder(page_token=f"{folder_path}/children?$top=50&$skiptoken=abc"))

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
        "/../beta/me/drive/root/children?x=1",
        "//evil.example.com/me/drive/root/children?x=1",
        "/me/drive/root/children?x=1#fragment",
        "/me/drive/root/children?x=1 2",
        "/me/drive/root/children?x=\x01",
    ],
)
def test_list_folder_refuses_a_page_token_for_another_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_token: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert json.loads(tools.microsoft_files_list_folder(page_token=page_token))["code"] == "invalid_page_token"
    assert graph.requests == []


@pytest.mark.parametrize(
    "page_token",
    ["-1", "abc", "1.5", "99999999999", "\u00b2", pytest.param("9" * 5000, id="5000-digits")],
)
def test_search_refuses_a_malformed_page_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_token: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert json.loads(tools.microsoft_files_search("plan", page_token=page_token))["code"] == "invalid_page_token"
    assert graph.requests == []


def test_download_writes_into_the_download_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, _graph = _tools(tmp_path, monkeypatch, _Graph())

    result = json.loads(tools.microsoft_files_download_file(ITEM_PATH))

    target = tmp_path / "workspace" / "microsoft-files-downloads" / "report.docx"
    assert target.read_bytes() == b"old content"
    assert result["path"] == str(target)
    assert result["file"]["etag"] == ETAG


def test_download_refuses_a_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph(item=_item(name="Reports", folder=True)))

    assert json.loads(tools.microsoft_files_download_file(ITEM_PATH))["code"] == "is_folder"
    assert len(graph.requests) == 1


def test_download_checks_the_size_before_fetching_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph(item=_item(size=microsoft_files._MAX_DOWNLOAD_BYTES + 1)))

    assert json.loads(tools.microsoft_files_download_file(ITEM_PATH))["code"] == "too_large"
    assert len(graph.requests) == 1


@pytest.mark.parametrize("etag", [ETAG, f" {ETAG} "])
def test_replace_sends_the_etag_and_streams_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    etag: str,
) -> None:
    _workspace_file(tmp_path, "edited/report.docx", b"new content")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    result = json.loads(tools.microsoft_files_replace_file(ITEM_PATH, "edited/report.docx", etag, WEB_URL))

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
    assert (
        json.loads(tools.microsoft_files_replace_file(ITEM_PATH, "report.docx", " ", WEB_URL))["code"]
        == "invalid_argument"
    )
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

    assert json.loads(tools.microsoft_files_replace_file(ITEM_PATH, "report.docx", ETAG, WEB_URL))["code"] == code


def test_replace_refuses_a_folder_before_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph(item=_item(name="Reports", folder=True)))

    assert (
        json.loads(tools.microsoft_files_replace_file(ITEM_PATH, "report.docx", ETAG, f"{DOCUMENTS}/Reports"))["code"]
        == "is_folder"
    )
    assert [request.method for request in graph.requests] == ["GET"]


def test_replace_refuses_a_local_path_outside_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "secret.txt").write_bytes(b"secret")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert (
        json.loads(tools.microsoft_files_replace_file(ITEM_PATH, "../secret.txt", ETAG, WEB_URL))["code"]
        == "invalid_local_path"
    )
    assert graph.requests == []


def test_upload_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    files = _Graph(item=_item(name="Reports", folder=True))
    files.write = httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = json.loads(
        tools.microsoft_files_upload_file("report.docx", ITEM_PATH, expected_web_url=f"{DOCUMENTS}/Reports"),
    )

    assert result["code"] == "name_conflict"
    put = graph.requests[-1]
    assert put.url.path == f"/v1.0{ITEM_PATH}:/report.docx:/content"
    assert put.url.params["@microsoft.graph.conflictBehavior"] == "fail"


def test_upload_to_the_onedrive_root_quotes_the_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    files = _Graph()
    files.write = httpx.Response(201, json=_item(name="Q3 plan #1.docx"))
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = json.loads(tools.microsoft_files_upload_file("report.docx", name="Q3 plan #1.docx"))

    assert graph.requests[-1].url.raw_path.split(b"?")[0] == b"/v1.0/me/drive/root:/Q3%20plan%20%231.docx:/content"
    assert result["file"]["name"] == "Q3 plan #1.docx"


def test_upload_into_a_folder_requires_its_web_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace_file(tmp_path, "new.docx", b"new content")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert json.loads(tools.microsoft_files_upload_file("new.docx", ITEM_PATH))["code"] == "invalid_argument"
    assert graph.requests == []


@pytest.mark.parametrize("name", ["a/b.docx", "a\\b.docx", "..", " lead.docx", "trail.docx ", "x?.docx", "", "a\nb"])
def test_upload_refuses_names_onedrive_rejects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert json.loads(tools.microsoft_files_upload_file("report.docx", name=name))["code"] == "invalid_name"
    assert graph.requests == []


_BOUND_WRITES: dict[str, Callable[[MicrosoftFilesTools, str], str]] = {
    "replace": lambda tools, url: tools.microsoft_files_replace_file(ITEM_PATH, "report.docx", ETAG, url),
    "upload": lambda tools, url: tools.microsoft_files_upload_file("new.docx", ITEM_PATH, expected_web_url=url),
    "excel": lambda tools, url: tools.microsoft_files_excel_update_range(
        ITEM_PATH,
        "Budget",
        "A1",
        [["x"]],
        [[""]],
        url,
    ),
}


@pytest.mark.parametrize("write", list(_BOUND_WRITES))
def test_writes_run_only_on_the_target_whose_web_url_was_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: str,
) -> None:
    _workspace_file(tmp_path, "report.docx", b"new content")
    _workspace_file(tmp_path, "new.docx", b"new content")
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())
    call = _BOUND_WRITES[write]

    assert json.loads(call(tools, " "))["code"] == "invalid_argument"
    assert graph.requests == []
    refused = json.loads(call(tools, f"{DOCUMENTS}/mine.docx"))
    assert (refused["code"], refused["web_url"]) == ("target_mismatch", WEB_URL)
    assert [request.method for request in graph.requests] == ["GET"]
    assert json.loads(call(tools, WEB_URL))["status"] == "ok"
    assert {"PUT", "PATCH"} & {request.method for request in graph.requests}


def test_excel_list_worksheets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"value": [{"id": "{1}", "name": "Budget", "position": 0, "visibility": "Visible"}]})
        if request.url.path == f"/v1.0{ITEM_PATH}/workbook/worksheets"
        else None
    )
    tools, _graph = _tools(tmp_path, monkeypatch, files)

    result = json.loads(tools.microsoft_files_excel_list_worksheets(ITEM_PATH))

    assert result["worksheets"] == [{"name": "Budget", "position": 0, "visibility": "Visible"}]


def test_excel_read_range_escapes_the_worksheet_and_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(
            200,
            json={
                "address": "'Q3 Budget'!A1:B2",
                "values": [[1, 2], [3, 3]],
                "formulas": [[1, 2], [3, "=A2"]],
                "rowCount": 2,
            },
        )
        if "/workbook/worksheets/" in request.url.path
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = json.loads(tools.microsoft_files_excel_read_range(ITEM_PATH, "Q3 Budget", "A1:B2"))

    assert result["range"]["values"] == [[1, 2], [3, 3]]
    assert result["range"]["formulas"] == [[1, 2], [3, "=A2"]]
    assert "formulas" in graph.requests[0].url.params["$select"].split(",")
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

    result = json.loads(tools.microsoft_files_excel_read_range(ITEM_PATH, "Budget"))

    assert (result["code"], result["address"]) == ("too_large", "A1:GR100")


def _workbook(formulas: list[list[object]], formats: list[list[str]] | None = None) -> _Graph:
    files = _Graph()
    files.range_address = "Budget!A1:B1"
    files.formulas = formulas
    files.formats = formats or [["General"] * len(row) for row in formulas]
    return files


def _update(
    tools: MicrosoftFilesTools,
    values: list[list[str | int | float | bool | None]],
    before: list[list[str | int | float | bool]],
    number_formats: list[list[str | None]] | None = None,
) -> dict[str, Any]:
    return json.loads(
        tools.microsoft_files_excel_update_range(
            ITEM_PATH,
            "Budget",
            "A1:B1",
            values,
            before,
            WEB_URL,
            number_formats,
        ),
    )


def _patches(graph: FakeGraph) -> list[dict[str, object]]:
    return [json.loads(request.content) for request in graph.requests if request.method == "PATCH"]


def test_excel_update_writes_cells_that_still_hold_what_was_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _workbook([["old", "=SUM(C1:C2)"]])
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [["x", 2]], [["old", "=SUM(C1:C2)"]])

    assert _patches(graph) == [{"values": [["x", 2]]}]
    assert (result["status"], result["verified"]) == ("ok", True)
    assert result["range"]["formulas"] == [["x", 2]]
    assert result["range"]["address"] == "Budget!A1:B1"


def test_excel_update_refuses_cells_someone_changed_since_they_were_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _workbook([["edited by Alex", "=SUM(C1:C2)"]])
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [["x", 2]], [["old", "=SUM(C1:C2)"]])

    assert result["code"] == "cells_changed"
    assert result["current_formulas"] == [["edited by Alex", "=SUM(C1:C2)"]]
    assert _patches(graph) == []


def test_excel_update_verifies_formulas_as_excel_stores_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _workbook([["=SUM(C1:C2)", 1.5]])
    files.store = lambda value: value.upper() if isinstance(value, str) else value
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [["=sum(c1:c3)", 0.3]], [["=SUM(C1:C2)", 1.5]])

    assert _patches(graph) == [{"values": [["=sum(c1:c3)", 0.3]]}]
    assert result["verified"] is True


def test_excel_update_tolerates_only_last_digit_rounding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _workbook([["", ""]])
    files.store = lambda value: {0.3: 0.30000000000000004, 123456789012345: 123456789012346}.get(value, value)
    tools, _graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [[0.3, 123456789012345]], [["", ""]])

    assert result["differences"] == [{"cell": "B1", "written": 123456789012345, "stored": 123456789012346}]


@pytest.mark.parametrize(
    ("current", "expected"),
    [([["=ABC", 1]], [["=abc", 1]]), ([["x", True]], [["x", 1]]), ([["x", 1]], [["x", True]])],
)
def test_excel_update_compares_what_was_read_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    current: list[list[str | int | float | bool]],
    expected: list[list[str | int | float | bool]],
) -> None:
    """A case-only edit to text and a TRUE where 1 was read are both edits someone made."""
    tools, graph = _tools(tmp_path, monkeypatch, _workbook(current, [["@", "General"]]))

    assert _update(tools, [["y", 2]], expected)["code"] == "cells_changed"
    assert _patches(graph) == []


def test_excel_update_refuses_a_formula_a_text_cell_would_keep_as_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _workbook([["", ""]], [["General", "@"]]))

    result = _update(tools, [["=1+1", "=1+1"]], [["", ""]])

    assert (result["code"], result["cell"]) == ("formula_as_text", "B1")
    assert _patches(graph) == []
    assert _update(tools, [["=1+1", "=1+1"]], [["", ""]], number_formats=[[None, "General"]])["verified"] is True


def test_excel_update_never_takes_text_for_a_formula_already_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text in a text-formatted cell is literal, so `=abc` does not already hold the formula `=ABC`."""
    tools, graph = _tools(tmp_path, monkeypatch, _workbook([["=abc", ""]], [["@", "General"]]))

    result = _update(tools, [["=ABC", None]], [["=abc", ""]])

    assert result["code"] == "formula_as_text"
    assert _patches(graph) == []


def test_excel_update_writes_iso_dates_as_dates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _workbook([["", ""]])
    files.store = lambda value: {"2026-11-02": 46328, "2026-11-02 09:30": 46328.395833}.get(value, value)
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [["2026-11-02", "2026-11-02 09:30"]], [["", ""]])

    assert (result["verified"], files.formulas) == (True, [[46328, 46328.395833]])
    assert _update(tools, [["2026-11-02", None]], [["", ""]])["already_applied"] is True
    assert len(_patches(graph)) == 1


def test_excel_update_verifies_by_reading_the_range_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PATCH response without the written properties does not decide verification."""
    files = _workbook([["", ""]])
    partial = httpx.Response(200, json={"address": "Budget!A1:B1"})
    files.answer_first = lambda request: (
        (files.range_response(request), partial)[1] if request.method == "PATCH" else None
    )
    tools, _graph = _tools(tmp_path, monkeypatch, files)

    assert _update(tools, [["x", 2]], [["", ""]])["verified"] is True


def test_excel_update_sets_only_formats_when_no_value_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _workbook([[0.25, 3]])
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [[None, None]], [[0.25, 3]], number_formats=[["0%", None]])

    assert _patches(graph) == [{"numberFormat": [["0%", None]]}]
    assert (result["verified"], files.formats) == (True, [["0%", "General"]])


def test_excel_update_skips_a_write_the_cells_already_hold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A replayed approval finds its own write and reports it instead of a conflict."""
    files = _workbook([["x", 2]])
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [["x", None]], [["old", 1]])

    assert (result["status"], result["verified"], result["already_applied"]) == ("ok", True, True)
    assert _patches(graph) == []


@pytest.mark.parametrize("text", ["0042", "1/2", "3-4", "Jan 5", "12:30", "TRUE", "1e5", "45%", "$1,000", "+A1", "@x"])
def test_excel_update_refuses_text_excel_would_convert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    files = _workbook([["", ""]])
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [["label", text]], [["", ""]])

    assert (result["code"], result["cell"]) == ("converted_text", "B1")
    assert _patches(graph) == []


@pytest.mark.parametrize("text", ["Q3 plan", "SKU-0042", '=A1&"x"', "", "-", "1/2/3/4", "v1.2.3"])
def test_excel_update_writes_text_excel_keeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _workbook([["", ""]]))

    assert _update(tools, [["label", text]], [["", ""]])["status"] == "ok"
    assert _patches(graph) == [{"values": [["label", text]]}]


def test_excel_update_keeps_text_in_a_text_formatted_cell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _workbook([["", ""]], [["General", "@"]]))

    assert _update(tools, [["label", "0042"]], [["", ""]])["verified"] is True
    assert _patches(graph) == [{"values": [["label", "0042"]]}]


def test_excel_update_sets_number_formats_before_the_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _workbook([["", ""]])
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [[0.25, "0042"]], [["", ""]], number_formats=[["0%", "@"]])

    assert _patches(graph) == [{"numberFormat": [["0%", "@"]]}, {"values": [[0.25, "0042"]]}]
    assert result["verified"] is True
    assert files.formats == [["0%", "@"]]


def test_excel_update_reports_cells_excel_stored_differently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _workbook([["", ""]])
    files.store = lambda value: 45000 if value == "5 mars" else value
    tools, _graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [["label", "5 mars"]], [["", ""]])

    assert (result["status"], result["verified"]) == ("ok", False)
    assert result["differences"] == [{"cell": "B1", "written": "5 mars", "stored": 45000}]


def test_excel_update_reports_a_number_format_excel_did_not_keep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _workbook([["", ""]])
    files.answer_first = lambda request: (
        httpx.Response(200, json={"address": "Budget!A1:B1"})
        if request.method == "PATCH" and b"numberFormat" in request.content
        else None
    )
    tools, _graph = _tools(tmp_path, monkeypatch, files)

    result = _update(tools, [[1, 2]], [["", ""]], number_formats=[[None, "0.0"]])

    assert result["verified"] is False
    assert result["differences"] == [{"cell": "B1", "number_format": "0.0", "stored_number_format": "General"}]


@pytest.mark.parametrize(
    ("before", "number_formats"),
    [([["", ""], ["", ""]], None), ([[""]], None), ([["", ""]], [["@"]]), ([["", ""]], [["@", ""]])],
)
def test_excel_update_refuses_grids_that_do_not_match_the_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before: list[list[str | int | float | bool]],
    number_formats: list[list[str | None]] | None,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _workbook([["", ""]]))

    assert _update(tools, [["x", "y"]], before, number_formats=number_formats)["code"] == "invalid_argument"
    assert graph.requests == []


@pytest.mark.parametrize(
    ("address", "values"),
    [
        ("A1:B1", [["x"], ["y", "z"]]),
        ("A1:B1", []),
        ("A1:B1", [[]]),
        ("A1;DROP", [["x"]]),
        ("Sheet2!A1", [["x"]]),
        ("A1:A20000", [[0]]),
        ("A1:B2", [["x", "y"]]),
        ("A1:B2", [["x", "y"], ["z"]]),
        ("A1:B1:C1", [["x", "y", "z"]]),
    ],
)
def test_excel_update_refuses_malformed_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    address: str,
    values: list[list[str | int | float | bool | None]],
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    before = [[""] * len(values[0])] * len(values) if values and values[0] else [[""]]
    result = json.loads(
        tools.microsoft_files_excel_update_range(ITEM_PATH, "Budget", address, values, before, WEB_URL),
    )

    assert result["code"] == "invalid_argument"
    assert graph.requests == []


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        (
            "microsoft_files_excel_update_range",
            {"file": ITEM_PATH, "worksheet": "Budget", "address": "A1", "values": "x", "expected_web_url": WEB_URL},
        ),
        (
            "microsoft_files_excel_update_range",
            {
                "file": ITEM_PATH,
                "worksheet": "Budget",
                "address": "A1",
                "values": [[{"x": 1}]],
                "expected_web_url": WEB_URL,
            },
        ),
        ("microsoft_files_excel_read_range", {"file": ITEM_PATH, "worksheet": 5}),
        ("microsoft_files_search", {"query": 5}),
        ("microsoft_files_get_file", {"file": None}),
    ],
)
def test_malformed_arguments_fail_call_validation_before_any_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, object],
) -> None:
    """Arguments that disagree with a function's signature fail call validation before its body runs."""
    tools, graph = _tools(tmp_path, monkeypatch, _Graph())

    assert_refused_by_call_validation(tools, graph, name, arguments)


def test_excel_worksheet_names_may_contain_a_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"address": "'Budget #1'!A1", "values": [[1]], "rowCount": 1})
        if "/workbook/worksheets/" in request.url.path
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    assert json.loads(tools.microsoft_files_excel_read_range(ITEM_PATH, "Budget #1", "A1"))["status"] == "ok"
    assert "/worksheets/Budget%20%231/" in graph.requests[0].url.raw_path.decode()


def test_excel_read_sends_the_address_without_padding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _Graph()
    files.answer_first = lambda request: (
        httpx.Response(200, json={"address": "Budget!A1:B2", "values": [[1, 2], [3, 4]]})
        if "/workbook/worksheets/" in request.url.path
        else None
    )
    tools, graph = _tools(tmp_path, monkeypatch, files)

    assert json.loads(tools.microsoft_files_excel_read_range(ITEM_PATH, "Budget", " $A$1:B2 "))["status"] == "ok"
    assert unquote(graph.requests[-1].url.path).endswith("/worksheets/Budget/range(address='$A$1:B2')")


@pytest.mark.parametrize("worksheet", ["", ".", "..", "a/b", "a:b", "a?b", "a*b", "a[b", "a]b", "a\\b", "x" * 32])
def test_excel_refuses_impossible_worksheet_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worksheet: str,
) -> None:
    tools, graph = _tools(tmp_path, monkeypatch, _Graph(), connected=False)

    assert json.loads(tools.microsoft_files_excel_read_range(ITEM_PATH, worksheet, "A1"))["code"] == "invalid_argument"
    assert graph.requests == []


def test_excel_update_accepts_values_matching_the_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    files = _workbook([["", ""], ["", ""]])
    tools, graph = _tools(tmp_path, monkeypatch, files)

    result = json.loads(
        tools.microsoft_files_excel_update_range(
            ITEM_PATH,
            "Budget",
            "$B$2:C3",
            [[1, 2], [3, 4]],
            [["", ""], ["", ""]],
            WEB_URL,
        ),
    )

    assert result["status"] == "ok"
    assert unquote(graph.requests[-1].url.path).endswith("/worksheets/Budget/range(address='$B$2:C3')")
    assert _patches(graph) == [{"values": [[1, 2], [3, 4]]}]
