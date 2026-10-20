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
    graph_tool_result,
    next_page_token,
    path_segment,
    share_id,
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


def test_other_graph_failures_report_the_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeGraph(_answer(502, text="bad gateway")).install(monkeypatch)

    result = _result(tmp_path, lambda session: session.json("GET", "/me/drive"))

    assert (result["code"], result["status_code"], result["graph_code"]) == ("graph_error", 502, None)


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
        return httpx.Response(401)

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
