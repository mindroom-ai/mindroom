"""Tests for the shared Microsoft Graph client used by the Microsoft 365 tools."""
# ruff: noqa: D103

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.custom_tools.microsoft_graph import (
    GRAPH_URL,
    MicrosoftGraphError,
    checked_upload_source,
    graph_session,
    graph_time,
    graph_tool_result,
    html_to_text,
    mailbox_next_page_token,
    mailbox_page_path,
    mailbox_path,
    mailbox_tool_result,
    next_page_token,
    page_path,
    path_segment,
    read_upload_source,
    share_id,
    text_to_html,
    time_zone_name,
)
from mindroom.oauth.microsoft import microsoft_files_oauth_provider
from mindroom.redaction import redact_sensitive_text
from tests.microsoft_graph_test_support import (
    TOKEN,
    FakeGraph,
    bearer,
    publish_grant,
    runtime_paths,
    save_client_config,
    worker_target,
)
from tests.oauth_test_utils import serve_token_endpoint

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.custom_tools.microsoft_graph import GraphSession

PROVIDER = microsoft_files_oauth_provider()
SHARE_LINK = "https://contoso-my.sharepoint.com/:w:/r/personal/alex/Documents/report.docx?d=w1&web=1"


def _result(
    tmp_path: Path,
    operation: Callable[[GraphSession], Mapping[str, object]],
    *,
    connected: bool = True,
    requester: bool = True,
    **grant_overrides: object,
) -> dict[str, object]:
    paths = runtime_paths(tmp_path)
    manager = save_client_config(paths)
    if connected:
        publish_grant(PROVIDER, manager, **grant_overrides)

    def run() -> Mapping[str, object]:
        with graph_session(PROVIDER, paths, manager, worker_target() if requester else None, None) as session:
            return operation(session)

    return json.loads(graph_tool_result("microsoft_files", run))


def _answer(status: int, **kwargs: object) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, **kwargs)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    return root


def test_requests_carry_the_requester_token_to_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(_answer(200, json={"id": "drive-1"})).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"))

    assert result == {"status": "ok", "tool": "microsoft_files", "id": "drive-1"}
    [request] = graph.requests
    assert str(request.url) == f"{GRAPH_URL}/me/drive"
    assert bearer(request) == TOKEN


def test_a_missing_connection_returns_a_connect_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(_answer(200, json={})).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"), connected=False)

    assert result["status"] == "error"
    assert result["oauth_connection_required"] is True
    assert result["provider"] == "microsoft_files"
    assert result["connect_url"]
    assert graph.requests == []


def test_a_call_without_a_requester_never_uses_a_stored_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(_answer(200, json={})).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"), requester=False)

    assert result["oauth_connection_required"] is True
    assert graph.requests == []


def test_a_rejected_refresh_asks_the_requester_to_reconnect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(_answer(200, json={})).install(monkeypatch)
    serve_token_endpoint(monkeypatch, [httpx.Response(400, json={"error": "invalid_grant"})])

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"), expires_at=1.0)

    assert result["oauth_connection_required"] is True
    assert result["reason"] == "refresh_rejected"
    assert graph.requests == []


def test_graph_rejecting_the_token_asks_the_requester_to_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeGraph(_answer(401, json={"error": {"code": "InvalidAuthenticationToken"}})).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"))

    assert result["oauth_connection_required"] is True
    assert result["reason"] == "access_rejected"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (412, "etag_mismatch"),
        (413, "too_large"),
        (423, "locked"),
    ],
)
def test_graph_statuses_map_to_stable_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
) -> None:
    FakeGraph(_answer(status, json={"error": {"code": "graphCode", "message": "details"}})).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"))

    assert (result["status"], result["code"], result["graph_code"]) == ("error", code, "graphCode")
    assert result["graph_message"] == "details"


def test_other_graph_failures_report_the_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGraph(_answer(502, text="bad gateway")).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"))

    assert (result["code"], result["status_code"], result["graph_code"]) == ("graph_error", 502, None)
    assert "graph_message" not in result


def test_a_rejected_request_reports_graphs_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reason = "Field 'Colour' is not recognized"
    FakeGraph(_answer(400, json={"error": {"code": "invalidRequest", "message": reason}})).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("POST", "/me/drive"))

    assert result == {
        "status": "error",
        "tool": "microsoft_files",
        "code": "graph_error",
        "message": "Microsoft Graph rejected the request.",
        "status_code": 400,
        "graph_code": "invalidRequest",
        "graph_message": reason,
    }


@pytest.mark.parametrize(
    ("graph_message", "shown"),
    [
        ("  Field\t'Colour'\r\n is  not recognized. ", "Field 'Colour' is not recognized."),
        ("Bad\x00 value\x1b[31m\u202e here\x7f", "Bad value [31m here"),
        ("See https://contoso.sharepoint.com/sites/team?token=abc for details.", "See <url> for details."),
        ("x" * 300, "x" * 300),
        ("y" * 301, "y" * 299 + "…"),
        ("", None),
        (" \n\t", None),
        (None, None),
        (42, None),
        ({"text": "nested"}, None),
        (["list"], None),
    ],
)
def test_graphs_reason_is_one_short_line_of_plain_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    graph_message: object,
    shown: str | None,
) -> None:
    error: dict[str, object] = {"code": "invalidRequest"}
    if graph_message is not None:
        error["message"] = graph_message
    FakeGraph(_answer(400, json={"error": error})).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"))

    assert (result["code"], result["graph_code"]) == ("graph_error", "invalidRequest")
    assert result.get("graph_message") == shown
    assert ("graph_message" in result) is (shown is not None)


@pytest.mark.parametrize(
    "body",
    [
        {"error": "invalid_request"},
        {"error": ["invalidRequest"]},
        {"message": "outside the error object"},
        ["invalidRequest"],
    ],
)
def test_a_body_without_a_graph_error_object_reports_no_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: object,
) -> None:
    FakeGraph(_answer(400, json=body)).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"))

    assert (result["code"], result["graph_code"]) == ("graph_error", None)
    assert "graph_message" not in result


def test_throttling_reports_when_to_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGraph(_answer(429, headers={"Retry-After": "7"}, json={"error": {"code": "TooManyRequests"}})).install(
        monkeypatch,
    )

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"))

    assert (result["code"], result["retry_after_seconds"]) == ("throttled", 7)


def test_transport_errors_report_only_their_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        msg = f"cannot reach {request.url} secret-detail"
        raise httpx.ConnectError(msg, request=request)

    FakeGraph(refuse).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"))

    assert (result["code"], result["error_type"]) == ("graph_unreachable", "ConnectError")
    assert "secret-detail" not in json.dumps(result)


def test_timeouts_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        msg = "slow"
        raise httpx.ReadTimeout(msg, request=request)

    FakeGraph(slow).install(monkeypatch)

    assert _result(tmp_path, lambda session: session.json("GET", "/me/drive"))["code"] == "graph_timeout"


def test_a_download_follows_the_redirect_without_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    download_url = "https://contoso-my.sharepoint.com/personal/alex/_layouts/15/download.aspx?UniqueId=1&tempauth=x"

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(302, headers={"Location": download_url})
        return httpx.Response(200, content=b"file bytes")

    graph = FakeGraph(answer).install(monkeypatch)
    workspace = _workspace(tmp_path)

    result = _result(
        tmp_path,
        lambda session: {
            "path": str(
                session.save_download(
                    "/drives/d/items/i/content",
                    workspace_root=workspace,
                    directory="downloads",
                    filename="report.docx",
                    max_bytes=100,
                ),
            ),
        },
    )

    assert (workspace / "downloads" / "report.docx").read_bytes() == b"file bytes"
    assert result["path"] == str(workspace / "downloads" / "report.docx")
    graph_request, sharepoint_request = graph.requests
    assert bearer(graph_request) == TOKEN
    assert bearer(sharepoint_request) is None


def test_a_download_refused_after_the_redirect_is_not_a_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(302, headers={"Location": "https://contoso-my.sharepoint.com/download"})
        return httpx.Response(401, json={"error": {"code": "unauthenticated", "message": "Link expired."}})

    FakeGraph(answer).install(monkeypatch)
    workspace = _workspace(tmp_path)

    result = _result(
        tmp_path,
        lambda session: {
            "path": str(
                session.save_download(
                    "/drives/d/items/i/content",
                    workspace_root=workspace,
                    directory="downloads",
                    filename="report.docx",
                    max_bytes=100,
                ),
            ),
        },
    )

    assert result["code"] == "download_rejected"
    assert "graph_message" not in result


def test_a_download_over_the_limit_leaves_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGraph(_answer(200, content=b"more than eight bytes")).install(monkeypatch)
    workspace = _workspace(tmp_path)

    result = _result(
        tmp_path,
        lambda session: {
            "path": str(
                session.save_download(
                    "/drives/d/items/i/content",
                    workspace_root=workspace,
                    directory="downloads",
                    filename="report.docx",
                    max_bytes=8,
                ),
            ),
        },
    )

    assert result["code"] == "too_large"
    assert list((workspace / "downloads").iterdir()) == []


@pytest.mark.parametrize("name", ["../x.docx", "a/b.docx", "a\\b.docx", "..", ".", " ", "C:evil.docx", "a\x00b"])
def test_a_download_with_an_unsafe_name_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    graph = FakeGraph(_answer(200, content=b"bytes")).install(monkeypatch)
    workspace = _workspace(tmp_path)

    result = _result(
        tmp_path,
        lambda session: {
            "path": str(
                session.save_download(
                    "/drives/d/items/i/content",
                    workspace_root=workspace,
                    directory="downloads",
                    filename=name,
                    max_bytes=100,
                ),
            ),
        },
    )

    assert result["code"] == "unsafe_name"
    assert graph.requests == []
    assert list(workspace.rglob("*")) == []


def test_an_upload_streams_the_workspace_file_with_its_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(_answer(200, json={"id": "item-1"})).install(monkeypatch)
    workspace = _workspace(tmp_path)
    (workspace / "out.docx").write_bytes(b"new content")
    source = checked_upload_source("out.docx", workspace_root=workspace, file_access="workspace", max_bytes=100)

    result = _result(
        tmp_path,
        lambda session: session.upload("PUT", "/drives/d/items/i/content", source, max_bytes=100),
    )

    assert result["id"] == "item-1"
    [request] = graph.requests
    assert request.headers["content-length"] == str(len(b"new content"))
    assert "transfer-encoding" not in request.headers
    assert request.content == b"new content"


def test_an_upload_source_over_the_limit_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "big.bin").write_bytes(b"0123456789")

    with pytest.raises(MicrosoftGraphError) as excinfo:
        checked_upload_source("big.bin", workspace_root=workspace, file_access="workspace", max_bytes=4)

    assert excinfo.value.code == "too_large"


def test_reading_an_upload_source_refuses_a_file_swapped_or_grown_after_the_check(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / "doc.bin").write_bytes(b"0123")
    (tmp_path / "secret.bin").write_bytes(b"secret")
    source = checked_upload_source("doc.bin", workspace_root=workspace, file_access="workspace", max_bytes=4)

    assert read_upload_source(source, 4) == b"0123"
    (workspace / "doc.bin").write_bytes(b"0123456789")
    with pytest.raises(MicrosoftGraphError) as grown:
        read_upload_source(source, 4)
    (workspace / "doc.bin").unlink()
    (workspace / "doc.bin").symlink_to(tmp_path / "secret.bin")
    with pytest.raises(MicrosoftGraphError) as swapped:
        read_upload_source(source, 100)

    assert grown.value.code == "too_large"
    assert swapped.value.code == "invalid_local_path"


def test_an_upload_source_outside_the_workspace_is_refused(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (tmp_path / "secret.txt").write_bytes(b"secret")

    with pytest.raises(MicrosoftGraphError) as excinfo:
        checked_upload_source("../secret.txt", workspace_root=workspace, file_access="workspace", max_bytes=100)

    assert excinfo.value.code == "invalid_local_path"


@pytest.mark.parametrize(
    ("value", "segment"),
    [
        ("b!Drive-1_x", "b!Drive-1_x"),
        ("19:abc@thread.tacv2", "19:abc@thread.tacv2"),
        ("AAMkAGI2==", "AAMkAGI2=="),
        ("Inbox name", "Inbox%20name"),
    ],
)
def test_path_segments_accept_graph_ids(value: str, segment: str) -> None:
    assert path_segment(value, "id") == segment


@pytest.mark.parametrize("value", ["", " ", ".", "..", "a/b", "a\\b", "a?b", "a#b", "a\nb", 7, None])
def test_path_segments_refuse_anything_that_changes_the_path(value: object) -> None:
    with pytest.raises(MicrosoftGraphError) as excinfo:
        path_segment(value, "id")

    assert excinfo.value.code == "invalid_argument"


@pytest.mark.parametrize(
    "value",
    [
        "W. Europe Standard Time",
        "Central Standard Time (Mexico)",
        "Pacific Standard Time (Mexico)",
        "UTC-11",
        "America/New_York",
        "Etc/GMT+5",
    ],
)
def test_time_zone_names_accept_windows_and_iana_names(value: str) -> None:
    assert time_zone_name(value) == value


@pytest.mark.parametrize("value", ["", " UTC", "UTC ", "Europe/Berlin;x", "(Mexico)", "Europe/Berlin\n", 7, None])
def test_time_zone_names_refuse_anything_else(value: object) -> None:
    with pytest.raises(MicrosoftGraphError) as excinfo:
        time_zone_name(value)

    assert excinfo.value.code == "invalid_argument"


def test_share_ids_encode_the_whole_link() -> None:
    encoded = share_id(SHARE_LINK)

    assert encoded.startswith("u!")
    assert "=" not in encoded
    payload = encoded.removeprefix("u!")
    assert base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode() == SHARE_LINK


def test_share_ids_stay_out_of_httpx_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    FakeGraph(_answer(200, json={"id": "item-1"})).install(monkeypatch)

    with caplog.at_level(logging.INFO, logger="httpx"):
        _result(tmp_path, lambda session: session.json("GET", f"/shares/{share_id(SHARE_LINK)}/driveItem"))

    # MindRoom's log formatter applies central redaction to every record, httpx's included.
    logged = redact_sensitive_text(caplog.text)
    assert "/v1.0/shares/***redacted***/driveItem" in logged
    assert share_id(SHARE_LINK)[:24] not in logged


def test_personal_onedrive_download_links_stay_out_of_httpx_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    download_url = "https://public.ch.files.1drv.com/y4mCAPABILITY-SECRET/report.docx?psid=1"

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(302, headers={"Location": download_url})
        return httpx.Response(200, content=b"file bytes")

    FakeGraph(answer).install(monkeypatch)
    workspace = _workspace(tmp_path)

    with caplog.at_level(logging.INFO, logger="httpx"):
        _result(
            tmp_path,
            lambda session: {
                "path": str(
                    session.save_download(
                        "/drives/d/items/i/content",
                        workspace_root=workspace,
                        directory="downloads",
                        filename="report.docx",
                        max_bytes=100,
                    ),
                ),
            },
        )

    logged = redact_sensitive_text(caplog.text)
    assert "public.ch.files.1drv.com/***redacted***" in logged
    assert "CAPABILITY-SECRET" not in logged


def test_a_tenant_change_retires_connections_made_for_another_tenant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = FakeGraph(_answer(200, json={"id": "drive-1"})).install(monkeypatch)
    paths = runtime_paths(tmp_path, {"MICROSOFT_TENANT_ID": "contoso.onmicrosoft.com"})
    manager = save_client_config(paths)
    publish_grant(PROVIDER, manager)

    def run() -> Mapping[str, object]:
        with graph_session(PROVIDER, paths, manager, worker_target(), None) as session:
            return session.json("GET", "/me/drive")

    result = json.loads(graph_tool_result("microsoft_files", run))

    assert result["oauth_connection_required"] is True
    assert result["reason"] == "refresh_rejected"
    assert graph.requests == []


def test_a_malformed_tenant_setting_is_reported_not_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph(_answer(200, json={})).install(monkeypatch)
    paths = runtime_paths(tmp_path, {"MICROSOFT_TENANT_ID": "bad/tenant"})
    manager = save_client_config(paths)
    publish_grant(PROVIDER, manager)

    def run() -> Mapping[str, object]:
        with graph_session(PROVIDER, paths, manager, worker_target(), None) as session:
            return session.json("GET", "/me/drive")

    result = json.loads(graph_tool_result("microsoft_files", run))

    assert result["code"] == "invalid_tenant"
    assert graph.requests == []


def test_a_download_that_cannot_be_written_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGraph(_answer(200, content=b"bytes")).install(monkeypatch)
    workspace = _workspace(tmp_path)
    (workspace / "downloads").write_text("a file where the directory belongs")

    result = _result(
        tmp_path,
        lambda session: {
            "path": str(
                session.save_download(
                    "/drives/d/items/i/content",
                    workspace_root=workspace,
                    directory="downloads",
                    filename="report.docx",
                    max_bytes=100,
                ),
            ),
        },
    )

    assert result["code"] == "workspace_unavailable"


@pytest.mark.parametrize("filename", ["報告" * 60 + ".docx", "x" * 300 + ".docx"])
def test_a_download_name_too_long_for_the_disk_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    FakeGraph(_answer(200, content=b"bytes")).install(monkeypatch)
    workspace = _workspace(tmp_path)

    result = _result(
        tmp_path,
        lambda session: {
            "path": str(
                session.save_download(
                    "/drives/d/items/i/content",
                    workspace_root=workspace,
                    directory="downloads",
                    filename=filename,
                    max_bytes=100,
                ),
            ),
        },
    )

    assert result["code"] == "workspace_unavailable"


def test_a_download_into_an_inaccessible_directory_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeGraph(_answer(200, content=b"bytes")).install(monkeypatch)
    workspace = _workspace(tmp_path)
    locked = workspace / "downloads"
    locked.mkdir()
    locked.chmod(0)
    try:
        result = _result(
            tmp_path,
            lambda session: {
                "path": str(
                    session.save_download(
                        "/drives/d/items/i/content",
                        workspace_root=workspace,
                        directory="downloads",
                        filename="report.docx",
                        max_bytes=100,
                    ),
                ),
            },
        )
    finally:
        locked.chmod(0o700)

    assert result["code"] == "workspace_unavailable"


@pytest.mark.parametrize(
    ("link", "token"),
    [
        (
            "https://graph.microsoft.com/v1.0/me/drive/root/children?$skiptoken=a",
            "/me/drive/root/children?$skiptoken=a",
        ),
        ("https://evil.example.com/v1.0/me/drive/root/children?$skiptoken=a", None),
        ("https://graph.microsoft.com/beta/me/drive/root/children?$skiptoken=a", None),
        (None, None),
    ],
)
def test_next_page_tokens_only_continue_graph_v1_collections(link: str | None, token: str | None) -> None:
    assert next_page_token({"@odata.nextLink": link} if link else {}) == token


@pytest.mark.parametrize(
    ("page_token", "collection"),
    [
        ("/me/drive/root/children?$skiptoken=a", "/me/drive/root/children"),
        ("/me/todo/lists/AAMk-1=/tasks?$skip=50&$filter=a%2Eb", r"/me/todo/lists/[^/]+/tasks"),
    ],
)
def test_page_paths_continue_their_own_collection(page_token: str, collection: str) -> None:
    assert page_path(page_token, collection) == page_token


@pytest.mark.parametrize(
    "page_token",
    [
        "/me/todo/lists/../../planner/tasks?x=1",
        "/me/todo/lists/%2e%2e/%2e%2e/planner/tasks?x=1",
        "/me/todo/lists/%2E%2e/%2e%2E/planner/tasks?x=1",
        "/me/todo/lists/.%2E/x/tasks?x=1",
        "/me/todo/lists/AAMk-1=/tasks",
        "//evil.example.com/tasks?x=1",
        "https://graph.microsoft.com/v1.0/me/todo/lists/AAMk-1=/tasks?x=1",
        "",
    ],
)
def test_page_paths_refuse_tokens_that_can_leave_the_collection(page_token: str) -> None:
    with pytest.raises(MicrosoftGraphError) as excinfo:
        page_path(page_token, r"/me/todo/lists/.+/tasks")

    assert excinfo.value.code == "invalid_page_token"


@pytest.mark.parametrize(
    ("value", "path"),
    [
        (None, "/me"),
        ("kim@contoso.com", "/users/kim@contoso.com"),
        ("Kim.Ng+desk@contoso.com", "/users/Kim.Ng%2Bdesk@contoso.com"),
        # Microsoft Graph takes a guest's sign-in name with each `#` percent-encoded.
        ("kim_fabrikam.com#EXT#@contoso.onmicrosoft.com", "/users/kim_fabrikam.com%23EXT%23@contoso.onmicrosoft.com"),
    ],
)
def test_mailbox_paths_name_the_requester_or_one_address(value: str | None, path: str) -> None:
    assert mailbox_path(value, "mailbox") == path


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "kim",
        "me",
        " kim@contoso.com",
        "kim@contoso.com ",
        "kim @contoso.com",
        "kim@contoso.com\n",
        "kim\x00@contoso.com",
        "kim\u00a0@contoso.com",
        "kim\u200b@contoso.com",
        "kim@contoso.com\u202e",
        "kim@contoso",
        "kim@@contoso.com",
        "a@b@contoso.com",
        "kim@contoso.com/../me",
        "kim\\x@contoso.com",
        "kim@contoso.com?x=1",
        "Kim <kim@contoso.com>",
        f"{'k' * 250}@contoso.com",
        5,
        ["kim@contoso.com"],
    ],
)
def test_mailbox_paths_refuse_anything_but_one_printable_address(value: object) -> None:
    with pytest.raises(MicrosoftGraphError) as excinfo:
        mailbox_path(value, "calendar_owner")

    assert excinfo.value.code == "invalid_argument"
    assert excinfo.value.message == "calendar_owner must be an email address or sign-in name, such as kim@contoso.com."


@pytest.mark.parametrize(
    "graph_mailbox",
    [
        "/me",
        "/users/kim@contoso.com",
        "/users/kim%40contoso.com",
        "/users/7f3c2b1a-0000-4000-8000-00000000abcd",
        "/users('7f3c2b1a-0000-4000-8000-00000000abcd')",
        "/users(7f3c2b1a-0000-4000-8000-00000000abcd)",
        "/Users('kim@contoso.com')",
    ],
)
@pytest.mark.parametrize("mailbox", ["/me", "/users/kim@contoso.com"])
def test_mailbox_next_page_tokens_name_the_mailbox_as_the_call_did(graph_mailbox: str, mailbox: str) -> None:
    link = f"{GRAPH_URL}{graph_mailbox}/mailFolders('inbox')/messages?$skiptoken=a"

    assert mailbox_next_page_token({"@odata.nextLink": link}, mailbox) == (
        f"{mailbox}/mailFolders('inbox')/messages?$skiptoken=a"
    )


def test_mailbox_next_page_tokens_end_with_the_listing() -> None:
    assert mailbox_next_page_token({}, "/users/kim@contoso.com") is None
    assert mailbox_next_page_token({"@odata.nextLink": "https://evil.example.com/v1.0/me/x?a=1"}, "/me") is None


@pytest.mark.parametrize(
    ("page_token", "mailbox"),
    [
        ("/me/calendars?$skiptoken=a", "/me"),
        ("/users/kim@contoso.com/calendars?$skiptoken=a", "/users/kim@contoso.com"),
        ("/users/Kim@Contoso.COM/calendars?$skiptoken=a", "/users/kim@contoso.com"),
    ],
)
def test_mailbox_page_paths_continue_the_same_mailbox(page_token: str, mailbox: str) -> None:
    assert mailbox_page_path(page_token, "/me/calendars", mailbox, "calendar_owner") == page_token


@pytest.mark.parametrize(
    ("page_token", "mailbox"),
    [
        ("/users/kim@contoso.com/calendars?$skiptoken=a", "/me"),
        ("/me/calendars?$skiptoken=a", "/users/kim@contoso.com"),
        ("/users/sam@contoso.com/calendars?$skiptoken=a", "/users/kim@contoso.com"),
        ("/users/kim@contoso.com.evil/calendars?$skiptoken=a", "/users/kim@contoso.com"),
        ("/users('kim@contoso.com')/calendars?$skiptoken=a", "/users/kim@contoso.com"),
    ],
)
def test_mailbox_page_paths_refuse_a_token_of_another_mailbox(page_token: str, mailbox: str) -> None:
    with pytest.raises(MicrosoftGraphError) as excinfo:
        mailbox_page_path(page_token, "/me/calendars", mailbox, "calendar_owner")

    assert excinfo.value.code == "invalid_page_token"
    assert excinfo.value.message == (
        "page_token must be the next_page_token this function returned for the same calendar_owner."
    )


def test_mailbox_page_paths_refuse_a_token_of_another_collection() -> None:
    with pytest.raises(MicrosoftGraphError) as excinfo:
        mailbox_page_path("/users/kim@contoso.com/messages?$skip=1", "/me/calendars", "/users/kim@contoso.com", "x")

    assert excinfo.value.code == "invalid_page_token"


def _refuse(code: str) -> Mapping[str, object]:
    raise MicrosoftGraphError(code=code, message="Refused.", graph_code="ErrorAccessDenied")


@pytest.mark.parametrize(
    ("operation", "fields"),
    [
        (lambda: {"deleted": True}, {"status": "ok", "deleted": True}),
        (lambda: _refuse("forbidden"), {"status": "error", "code": "forbidden", "graph_code": "ErrorAccessDenied"}),
    ],
)
def test_every_result_of_a_call_that_names_a_mailbox_repeats_it(
    operation: Callable[[], Mapping[str, object]],
    fields: dict[str, object],
) -> None:
    named = json.loads(mailbox_tool_result("outlook_mail", "mailbox", "kim@contoso.com", operation))
    own = json.loads(mailbox_tool_result("outlook_mail", "mailbox", None, operation))

    assert named == {**own, "mailbox": "kim@contoso.com"}
    assert fields.items() <= own.items()
    assert "mailbox" not in own


def test_a_malformed_mailbox_is_refused_before_the_operation_runs() -> None:
    calls: list[str] = []

    def operation() -> Mapping[str, object]:
        calls.append("ran")
        return {}

    result = json.loads(mailbox_tool_result("outlook_calendar", "calendar_owner", " kim@contoso.com", operation))

    assert (result["status"], result["code"]) == ("error", "invalid_argument")
    assert "calendar_owner" not in result
    assert calls == []


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ({"dateTime": "2026-10-21T09:00:00.0000000", "timeZone": "UTC"}, "2026-10-21T09:00:00Z"),
        ({"dateTime": "2026-10-21T09:00:00"}, "2026-10-21T09:00:00Z"),
        (
            {"dateTime": "2026-10-21T09:00:00.0000000", "timeZone": "Pacific Standard Time"},
            "2026-10-21T09:00:00 (Pacific Standard Time)",
        ),
        ({"timeZone": "UTC"}, None),
        (None, None),
        ("2026-10-21T09:00:00", None),
    ],
)
def test_graph_times_read_as_iso_8601_naming_any_zone_but_utc(value: object, text: str | None) -> None:
    assert graph_time(value) == text


@pytest.mark.parametrize(
    ("html", "text"),
    [
        ("Plain notes", "Plain notes"),
        ("First line<br>Second line", "First line\nSecond line"),
        ("Paragraph<br><br>after a blank line", "Paragraph\n\nafter a blank line"),
        ("<p>First</p><p>Second &amp; third</p>", "First\nSecond & third"),
        ("<ul><li>one</li><li>two</li></ul>", "one\ntwo"),
        ("<p>a   b</p>\n\n\n<p></p><p>c</p>", "a b\nc"),
        ('<attachment id="ee8d"></attachment>', ""),
        ("Tom &lt;tom@contoso.com&gt;&nbsp;wrote", "Tom <tom@contoso.com> wrote"),
        (
            "<html><head><title>Note</title><style>p { color: red; }</style></head>"
            "<body><div>Line  one\n  continues <b>bold</b></div></body></html>",
            "Line one continues bold",
        ),
        ("<html><head><title>Note</title><body><p>Kept</p></body></html>", "Kept"),
        (
            "<table><tr><th>Name</th><th>Value</th></tr>\n<tr><td> Alpha </td> <td>1</td><td></td><td>x</td></tr></table>",
            "Name | Value\nAlpha | 1 | | x",
        ),
        ('<p>see</p><img src="https://graph.microsoft.com/x" alt="chart">', "see\n[image]"),
        ('<p>Ship it <emoji id="1f44d_thumbsup" alt="\U0001f44d" title="Thumbs up"></emoji></p>', "Ship it \U0001f44d"),
        (
            '<blockquote itemscope="" itemtype="http://schema.skype.com/Reply" itemid="1616965872000">'
            '<strong itemprop="mri">Sam Example</strong><p itemprop="preview">Can we ship today?</p></blockquote>'
            "<p>Yes, after the review.</p>",
            "> Sam Example\n> Can we ship today?\nYes, after the review.",
        ),
        (
            "<blockquote>outer<blockquote>inner</blockquote>after</blockquote>reply",
            "> outer\n> > inner\n> after\nreply",
        ),
    ],
)
def test_html_to_text_keeps_readable_text_and_line_breaks(html: str, text: str) -> None:
    assert html_to_text(html) == text


@pytest.mark.parametrize(
    ("text", "html"),
    [
        ("Plain notes", "Plain notes"),
        (
            'Ship <b>today</b> & tell "everyone" it\'s done',
            "Ship &lt;b&gt;today&lt;/b&gt; &amp; tell &quot;everyone&quot; it&#x27;s done",
        ),
        ("one\ntwo\r\nthree\rfour", "one<br>two<br>three<br>four"),
        ("blank\n\nline\n", "blank<br><br>line"),
    ],
)
def test_text_to_html_escapes_markup_and_keeps_line_breaks(text: str, html: str) -> None:
    assert text_to_html(text) == html


def test_text_to_html_reads_back_as_the_same_text() -> None:
    text = "Tom <tom@contoso.com> wrote:\n1 < 2 & 3 > 2\n\nThanks"

    assert html_to_text(text_to_html(text)) == text
