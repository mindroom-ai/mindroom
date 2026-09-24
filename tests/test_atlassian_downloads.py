"""Confluence attachment listing and the hardened attachment download path."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
from structlog.testing import capture_logs

import mindroom.matrix.media as media_module
from mindroom import attachments as attachments_module
from mindroom.attachments import load_attachment
from mindroom.custom_tools import atlassian as atlassian_module
from mindroom.custom_tools import atlassian_client
from mindroom.custom_tools.atlassian import AtlassianTools
from mindroom.oauth.atlassian import atlassian_oauth_provider
from mindroom.tool_system.runtime_context import (
    get_tool_runtime_context,
    list_tool_runtime_attachment_ids,
    tool_runtime_context,
)
from tests.atlassian_test_support import (
    ALICE,
    CLOUD_ID,
    OTHER_CLOUD_ID,
    FakeGateway,
    bearer,
    gateway_url,
    publish_grant,
    runtime_paths,
    save_client_config,
    site,
    tool_context,
    worker_target,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

TOKEN = "alice-access-token"  # noqa: S105
DOWNLOAD_PATH = "/wiki/rest/api/content/123/child/attachment/att456/download"
MEDIA_URL = "https://api.media.atlassian.com/file/abc/binary?token=signed-secret&client=xyz"
PAYLOAD = b"%PDF-1.7 attachment bytes"


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_env: dict[str, str] | None = None,
) -> tuple[AtlassianTools, FakeGateway, ToolRuntimeContext, RuntimePaths]:
    paths = runtime_paths(tmp_path, extra_env)
    manager = save_client_config(paths)
    publish_grant(atlassian_oauth_provider(), manager, TOKEN)
    gateway = FakeGateway(sites_by_token={TOKEN: [site()]}).install(monkeypatch)
    tool = AtlassianTools(runtime_paths=paths, credentials_manager=manager, worker_target=worker_target())
    return tool, gateway, tool_context(paths, tmp_path / "storage"), paths


def _redirect(location: str, **headers: str) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(302, headers={"location": location, **headers})


async def _streamed(body: bytes) -> AsyncIterator[bytes]:
    # A streamed body, as from a real connection; httpx reads plain bytes content eagerly.
    yield body


def _file(
    body: bytes = PAYLOAD,
    headers: dict[str, str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    default_headers = {"content-type": "application/pdf", "content-disposition": 'attachment; filename="Report.pdf"'}
    return lambda _request: httpx.Response(
        200,
        content=_streamed(body),
        headers={**default_headers, **(headers or {})},
    )


async def _download(tool: AtlassianTools, context: ToolRuntimeContext, **kwargs: str) -> dict[str, object]:
    with tool_runtime_context(context):
        arguments = {"page_id": "123", "attachment_id": "att456", **kwargs}
        return json.loads(await tool.confluence_download_attachment(**arguments))


def _download_requests(gateway: FakeGateway) -> list[httpx.Request]:
    return gateway.product_requests()


@pytest.mark.asyncio
async def test_list_attachments_reports_metadata_and_next_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listings use CQL by container and pass back the next cursor unchanged."""
    tool, gateway, _context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route(
        "GET",
        gateway_url("confluence", "/wiki/rest/api/content/search"),
        {
            "results": [
                {
                    "id": "att456",
                    "title": "Report.pdf",
                    "extensions": {"mediaType": "application/pdf", "fileSize": 2048, "comment": ""},
                    "version": {"number": 2, "when": "2026-01-02T00:00:00Z"},
                },
            ],
            "_links": {"next": "/rest/api/content/search?cql=x&cursor=c%2B1%2F2%3D&limit=25"},
        },
    )

    result = json.loads(await tool.confluence_list_attachments(page_id="123", limit=99))

    params = gateway.product_requests()[0].url.params
    assert params["cql"] == "type = attachment AND container = 123"
    assert params["limit"] == "50"
    assert result["attachments"] == [
        {
            "id": "att456",
            "title": "Report.pdf",
            "media_type": "application/pdf",
            "file_size": 2048,
            "comment": None,
            "version": 2,
            "updated": "2026-01-02T00:00:00Z",
        },
    ]
    assert (result["has_more"], result["next_cursor"]) == (True, "c+1/2=")
    assert "warning" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("links", "expected"),
    [
        ({}, (False, None, False)),
        ({"next": "/rest/api/content/search?cql=x&limit=25"}, (True, None, True)),
        ({"next": "/rest/api/content/search?cursor=%0Abad"}, (True, None, True)),
    ],
)
async def test_list_attachments_flags_pages_without_a_usable_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    links: dict[str, str],
    expected: tuple[bool, str | None, bool],
) -> None:
    """A next link without a safe cursor still reports that the listing is incomplete."""
    tool, gateway, _context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", "/wiki/rest/api/content/search"), {"results": [], "_links": links})

    result = json.loads(await tool.confluence_list_attachments(page_id="123"))

    assert (result["has_more"], result["next_cursor"], "warning" in result) == expected


@pytest.mark.asyncio
async def test_download_registers_a_turn_scoped_context_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file lands in managed storage under a generated name and is usable later in the same turn."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _redirect(MEDIA_URL))
    gateway.route("GET", MEDIA_URL.split("?", maxsplit=1)[0], _file())

    with tool_runtime_context(context):
        result = json.loads(await tool.confluence_download_attachment(page_id="123", attachment_id="456"))
        current = get_tool_runtime_context()
        assert current is not None
        available = list_tool_runtime_attachment_ids(current)

    attachment_id = result["attachment_id"]
    assert result["status"] == "ok"
    assert result["confluence_attachment_id"] == "att456"
    assert attachment_id in available
    assert result["attachment"]["filename"] == "Report.pdf"
    assert result["attachment"]["mime_type"] == "application/pdf"
    assert result["attachment"]["size_bytes"] == len(PAYLOAD)
    record = load_attachment(tmp_path / "storage", attachment_id)
    assert record is not None
    assert record.local_path.name == f"{attachment_id}.pdf"
    assert record.local_path.read_bytes() == PAYLOAD
    assert (record.kind, record.room_id, record.thread_id, record.sender) == (
        "file",
        "!room:example.org",
        "$thread",
        ALICE,
    )
    assert "signed-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_download_follows_the_observed_atlassian_redirect_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atlassian answers with an empty JSON 302 to a signed media URL, which serves the identity-encoded file."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    media_url = (
        "https://api.media.atlassian.com/file/0b9c2f4e-8d1a-4c3b-9e7f-5a6b7c8d9e0f/binary"
        "?token=eyJhbGciOiJIUzI1NiJ9.signed-secret.sig&client=11111111-2222-4333-8444-555555555555&dl=true"
    )
    gateway.route(
        "GET",
        gateway_url("confluence", DOWNLOAD_PATH),
        lambda _request: httpx.Response(
            302,
            headers={"location": media_url, "content-type": "application/json", "content-length": "0"},
        ),
    )
    gateway.route(
        "GET",
        media_url.split("?", maxsplit=1)[0],
        lambda _request: httpx.Response(
            200,
            content=_streamed(PAYLOAD),
            headers={
                "content-type": "application/pdf",
                "content-length": str(len(PAYLOAD)),
                "content-disposition": 'attachment; filename="Report.pdf"',
            },
        ),
    )

    result = await _download(tool, context)

    hops = _download_requests(gateway)
    assert result["status"] == "ok"
    assert result["attachment"]["size_bytes"] == len(PAYLOAD)
    assert [(str(request.url), bearer(request)) for request in hops] == [
        (gateway_url("confluence", DOWNLOAD_PATH), TOKEN),
        (media_url, None),
    ]
    record = load_attachment(tmp_path / "storage", result["attachment_id"])
    assert record is not None
    assert record.local_path.read_bytes() == PAYLOAD
    assert "signed-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_bearer_goes_only_to_the_gateway_and_every_hop_is_identity_encoded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway hops carry the bearer, media hops never do, and all ask for unencoded bytes."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    second_gateway_hop = gateway_url("confluence", "/wiki/download/attachments/123/Report.pdf")
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _redirect(second_gateway_hop))
    gateway.route("GET", second_gateway_hop, _redirect(MEDIA_URL))
    gateway.route(
        "GET",
        MEDIA_URL.split("?", maxsplit=1)[0],
        _redirect(gateway_url("confluence", "/wiki/final/Report.pdf")),
    )
    gateway.route("GET", gateway_url("confluence", "/wiki/final/Report.pdf"), _file())

    result = await _download(tool, context)

    hops = _download_requests(gateway)
    assert result["status"] == "ok"
    assert [(request.url.host, bearer(request)) for request in hops] == [
        ("api.atlassian.com", TOKEN),
        ("api.atlassian.com", TOKEN),
        ("api.media.atlassian.com", None),
        ("api.atlassian.com", TOKEN),
    ]
    assert all(request.headers["accept"] == "*/*" for request in hops)
    assert all(request.headers["accept-encoding"] == "identity" for request in hops)


@pytest.mark.asyncio
async def test_cookies_set_by_one_hop_never_reach_the_next(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Parent-domain cookies from the gateway are cleared before the media hop."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route(
        "GET",
        gateway_url("confluence", DOWNLOAD_PATH),
        _redirect(MEDIA_URL, **{"set-cookie": "session=gateway-cookie; Domain=.atlassian.com; Path=/; Secure"}),
    )
    gateway.route("GET", MEDIA_URL.split("?", maxsplit=1)[0], _file())

    result = await _download(tool, context)

    assert result["status"] == "ok"
    assert [request.headers.get("cookie") for request in _download_requests(gateway)] == [None, None]


@pytest.mark.asyncio
async def test_parent_domain_cookie_would_reach_the_media_host_without_clearing() -> None:
    """Control for the cookie test: httpx itself would forward the gateway's parent-domain cookie."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "session=gateway-cookie; Domain=.atlassian.com; Path=/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await client.get("https://api.atlassian.com/ex/confluence/x")
        await client.get(MEDIA_URL)

    assert seen == [None, "session=gateway-cookie"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    [
        "http://api.media.atlassian.com/file/abc",
        "https://api.media.atlassian.com:8443/file/abc",
        "https://user:pass@api.media.atlassian.com/file/abc",
        "https://evil.example.com/file/abc",
        "https://media.atlassian.com.evil.example/file",
        "https://example.atlassian.net/wiki/download/attachments/123/Report.pdf",
        f"https://api.atlassian.com/ex/confluence/{OTHER_CLOUD_ID}/wiki/download/x",
        f"https://api.atlassian.com/ex/jira/{CLOUD_ID}/secure/attachment/1",
        f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/../../../oauth/token",
        f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/%2e%2e/%2e%2e/x",
        f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/a%2Fb",
        f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/a%252Fb",
        f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/a%5Cb",
        f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/a;jsessionid=1",
        f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/a%252525252Fb",
        "https://api.atlassian.com:444/ex/confluence/x",
        "/wiki/download/attachments/123/Report.pdf",
        "",
    ],
)
async def test_unsupported_redirects_are_rejected_without_following_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    """Only this site's gateway path or the media service may be followed, and the bearer never leaves."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _redirect(location))

    result = await _download(tool, context)

    assert result["code"] == "redirect_rejected"
    assert len(_download_requests(gateway)) == 1
    serialized = json.dumps(result)
    assert location == "" or location not in serialized
    assert TOKEN not in serialized


@pytest.mark.asyncio
async def test_download_checks_each_hop_even_when_the_client_would_follow_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each download request disables redirect following itself, so a client default cannot skip the checks."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        atlassian_client,
        "_new_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle), follow_redirects=True),
    )
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _redirect("https://evil.example.com/file"))
    gateway.route("GET", "https://evil.example.com/file", _file())

    result = await _download(tool, context)

    assert result["code"] == "redirect_rejected"
    assert [request.url.host for request in _download_requests(gateway)] == ["api.atlassian.com"]


@pytest.mark.asyncio
async def test_malformed_redirect_fails_without_echoing_the_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Httpx refuses an unparsable Location itself, and the failure names only its type."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _redirect("https://[::1/?sig=signed-secret"))

    result = await _download(tool, context)

    assert result["code"] == "download_failed"
    assert result["message"] == "The download failed (RemoteProtocolError)."
    assert len(_download_requests(gateway)) == 1


@pytest.mark.asyncio
async def test_gateway_path_that_is_not_canonical_never_receives_the_bearer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A media host bouncing to a dot-segment gateway path is refused instead of being sent the token."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _redirect(MEDIA_URL))
    gateway.route(
        "GET",
        MEDIA_URL.split("?", maxsplit=1)[0],
        _redirect(f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/./download"),
    )

    result = await _download(tool, context)

    assert result["code"] == "redirect_rejected"
    assert [bearer(request) for request in _download_requests(gateway)] == [TOKEN, None]


@pytest.mark.asyncio
@pytest.mark.parametrize(("redirects", "succeeds"), [(3, True), (4, False)])
async def test_download_follows_at_most_three_redirects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    redirects: int,
    succeeds: bool,
) -> None:
    """Redirect chains are bounded."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    hops = [gateway_url("confluence", DOWNLOAD_PATH)] + [
        gateway_url("confluence", f"/wiki/hop/{index}") for index in range(redirects)
    ]
    for current, following in itertools.pairwise(hops):
        gateway.route("GET", current, _redirect(following))
    gateway.route("GET", hops[-1], _file())

    result = await _download(tool, context)

    assert (result["status"] == "ok") is succeeds
    if not succeeds:
        assert result["code"] == "redirect_rejected"
        assert len(_download_requests(gateway)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate, identity"])
async def test_content_encoded_downloads_are_rejected_without_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
) -> None:
    """Decoding could expand a small response past the byte limit, so coded bodies are refused."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _file(b"\x1f\x8b", {"content-encoding": encoding}))

    result = await _download(tool, context)

    assert result["code"] == "download_failed"
    assert "content-encoded" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"content-length": "9999"},
        {"content-length": "3"},
        {},
    ],
)
async def test_download_size_limit_follows_the_inline_transfer_bound_while_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    """The byte cap counts streamed bytes, whatever Content-Length claims, and follows the inline transfer limit."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch, {"MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES": "10"})

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"12345"

    gateway.route(
        "GET",
        gateway_url("confluence", DOWNLOAD_PATH),
        lambda _request: httpx.Response(200, content=chunks(), headers=headers),
    )

    result = await _download(tool, context)

    assert result["code"] == "attachment_too_large"
    assert result["max_bytes"] == 10
    assert not (tmp_path / "storage" / "incoming_media").exists()


@pytest.mark.asyncio
async def test_download_deadline_covers_a_slow_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The overall deadline bounds the whole transfer, including a body that trickles in."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(atlassian_client, "_DOWNLOAD_DEADLINE_SECONDS", 0.05)

    async def slow_chunks() -> AsyncIterator[bytes]:
        while True:
            yield b"x"
            await asyncio.sleep(0.01)

    gateway.route(
        "GET",
        gateway_url("confluence", DOWNLOAD_PATH),
        lambda _request: httpx.Response(200, content=slow_chunks()),
    )

    result = await _download(tool, context)

    assert result["code"] == "download_timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gateway_status", "media_status", "code"),
    [
        (403, None, "attachment_unavailable"),
        (404, None, "attachment_unavailable"),
        (500, None, "download_failed"),
        (None, 401, "download_failed"),
        (None, 403, "download_failed"),
    ],
)
async def test_download_errors_report_status_without_bodies_or_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_status: int | None,
    media_status: int | None,
    code: str,
) -> None:
    """Failed hops are described by status only, since error bodies can echo signed URLs."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    leaky = {"message": f"denied for {MEDIA_URL}", "token": TOKEN}
    if gateway_status is not None:
        gateway.route(
            "GET",
            gateway_url("confluence", DOWNLOAD_PATH),
            lambda _request: httpx.Response(gateway_status, json=leaky),
        )
    else:
        gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _redirect(MEDIA_URL))
        gateway.route(
            "GET",
            MEDIA_URL.split("?", maxsplit=1)[0],
            lambda _request: httpx.Response(media_status, json=leaky),
        )

    result = await _download(tool, context)

    serialized = json.dumps(result)
    assert result["code"] == code
    assert result["status_code"] == (gateway_status or media_status)
    assert "signed-secret" not in serialized
    assert "api.media.atlassian.com" not in serialized
    assert TOKEN not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"code": 401, "message": "Unauthorized"}, "access_rejected"),
        ({"message": f"denied for {MEDIA_URL}", "token": TOKEN}, "access_rejected"),
        ({"code": 401, "message": "Unauthorized; scope does not match"}, "scope_mismatch"),
    ],
)
async def test_gateway_401_asks_to_reconnect_unless_a_scope_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, object],
    expected: str,
) -> None:
    """Downloads treat a gateway 401 like any API call: reconnect, or report the missing scope."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route(
        "GET",
        gateway_url("confluence", DOWNLOAD_PATH),
        lambda _request: httpx.Response(401, json=body),
    )

    result = await _download(tool, context)

    serialized = json.dumps(result)
    if expected == "access_rejected":
        assert result["oauth_connection_required"] is True
        assert result["reason"] == "access_rejected"
    else:
        assert result["code"] == "scope_mismatch"
        assert result["status_code"] == 401
        assert "connect_url" not in result
    assert "signed-secret" not in serialized
    assert TOKEN not in serialized


@pytest.mark.asyncio
async def test_download_transport_errors_hide_signed_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Httpx errors on the media hop are reduced to their type."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)

    def failing(request: httpx.Request) -> httpx.Response:
        msg = f"timed out reading {request.url}"
        raise httpx.ReadTimeout(msg, request=request)

    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _redirect(MEDIA_URL))
    gateway.route("GET", MEDIA_URL.split("?", maxsplit=1)[0], failing)

    result = await _download(tool, context)

    assert result == {
        "status": "error",
        "tool": "atlassian",
        "code": "download_failed",
        "message": "The download failed (ReadTimeout).",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_id": "123/../1"},
        {"attachment_id": "att456/../../x"},
        {"attachment_id": "att456;v=1"},
        {"attachment_id": "https://api.media.atlassian.com/file"},
    ],
)
async def test_download_rejects_unsafe_ids_before_network_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, str],
) -> None:
    """Page and attachment IDs cannot alter the download path."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)

    result = await _download(tool, context, **kwargs)

    assert result["code"] == "invalid_argument"
    assert gateway.requests == []


@pytest.mark.asyncio
async def test_download_requires_attachment_storage_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a conversation context there is nowhere safe to put the file, so nothing is fetched."""
    tool, gateway, _context, _paths = _setup(tmp_path, monkeypatch)

    result = json.loads(await tool.confluence_download_attachment(page_id="123", attachment_id="att456"))

    assert result["code"] == "attachment_context_unavailable"
    assert gateway.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "fallback", "expected"),
    [
        ("attachment; filename=\"plain.pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf", None, "résumé.pdf"),
        ("attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf; filename=\"plain.pdf\"", None, "résumé.pdf"),
        ('attachment; filename="../../etc/passwd"', None, "passwd"),
        ('attachment; filename="..\\\\windows\\\\evil.pdf"', None, "evil.pdf"),
        ('attachment; filename=".hidden\u001bname\u007f.pdf"', None, "hiddenname.pdf"),
        ("attachment; filename*=UTF-8''%E2%80%AEfdp.exe%00", None, "fdp.exe"),
        ('attachment; filename="/"', "Listing title.pdf", "Listing title.pdf"),
        (None, None, "att456"),
    ],
)
async def test_display_filenames_prefer_rfc5987_and_are_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: str | None,
    fallback: str | None,
    expected: str,
) -> None:
    """The display name is untrusted metadata; the stored file name is always generated."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    headers = {"content-type": "application/pdf"}
    if disposition is not None:
        headers["content-disposition"] = disposition
    gateway.route(
        "GET",
        gateway_url("confluence", DOWNLOAD_PATH),
        lambda _request: httpx.Response(200, content=_streamed(PAYLOAD), headers=headers),
    )

    result = await _download(tool, context, **({"filename": fallback} if fallback else {}))

    assert result["attachment"]["filename"] == expected
    record = load_attachment(tmp_path / "storage", result["attachment_id"])
    assert record is not None
    assert record.local_path.name.startswith("att_")


def test_display_filenames_are_bounded() -> None:
    """Very long names are truncated."""
    name = atlassian_module._display_filename(f'attachment; filename="{"a" * 500}.pdf"', None)
    assert name is not None
    assert len(name) == 200


@pytest.mark.asyncio
async def test_unknown_content_type_is_guessed_from_the_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic or malformed media types fall back to the display name's extension."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route(
        "GET",
        gateway_url("confluence", DOWNLOAD_PATH),
        _file(
            b"\x89PNG\r\n",
            {"content-type": "application/octet-stream", "content-disposition": "inline; filename=a.png"},
        ),
    )

    result = await _download(tool, context)

    assert result["attachment"]["mime_type"] == "image/png"
    record = load_attachment(tmp_path / "storage", result["attachment_id"])
    assert record is not None
    assert record.kind == "image"


@pytest.mark.asyncio
async def test_cancelled_download_finishes_storing_before_cancelling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation waits for the store, so no write or registration runs on behind a cancelled call."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _file())
    started = threading.Event()
    release = threading.Event()
    real_register = attachments_module.register_bytes_attachment
    stored: list[str] = []

    def slow_register(*args: object, **kwargs: object) -> object:
        started.set()
        release.wait(timeout=5)
        record = real_register(*args, **kwargs)
        stored.append(record.attachment_id)
        return record

    monkeypatch.setattr(atlassian_module, "register_bytes_attachment", slow_register)

    task = asyncio.create_task(_download(tool, context))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(stored) == 1
    assert load_attachment(tmp_path / "storage", stored[0]) is not None


@pytest.mark.asyncio
async def test_redirect_without_location_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirect status without a Location header is refused instead of followed."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), lambda _request: httpx.Response(302))

    result = await _download(tool, context)

    assert result["code"] == "redirect_rejected"
    assert result["message"] == "Atlassian returned a download redirect without a location."
    assert len(_download_requests(gateway)) == 1


@pytest.mark.asyncio
async def test_request_logs_never_contain_signed_media_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Request logs keep media URLs without the signature query, and hop logs keep only a masked path shape."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _redirect(MEDIA_URL))
    gateway.route("GET", MEDIA_URL.split("?", maxsplit=1)[0], _file())

    with caplog.at_level(logging.INFO, logger="httpx"), capture_logs() as events:
        result = await _download(tool, context)

    logged = "\n".join(record.getMessage() for record in caplog.records if record.name == "httpx")
    hops = [event for event in events if event["event"] == "atlassian_download_hop"]
    assert result["status"] == "ok"
    assert "api.media.atlassian.com/file/abc/binary" in logged
    assert "signed-secret" not in logged
    assert hops == [
        {
            "event": "atlassian_download_hop",
            "log_level": "debug",
            "host": "api.atlassian.com",
            "path_shape": "/ex/confluence/*/wiki/rest/api/content/*/child/attachment/*/download",
            "status_code": 302,
            "bearer_sent": True,
        },
        {
            "event": "atlassian_download_hop",
            "log_level": "debug",
            "host": "api.media.atlassian.com",
            "path_shape": "/file/abc/binary",
            "status_code": 200,
            "bearer_sent": False,
        },
    ]
    assert not any(marker in repr(hops) for marker in ("signed-secret", "token=", "?", CLOUD_ID, "att456", TOKEN))


@pytest.mark.parametrize(
    ("url", "shape"),
    [
        (
            f"https://api.atlassian.com/ex/confluence/{CLOUD_ID}/wiki/download/attachments/123/Report.pdf?version=1",
            "/ex/confluence/*/wiki/download/attachments/*/*",
        ),
        ("https://api.media.atlassian.com/file/0b9c2f4e-8d1a-4c3b-9e7f-5a6b7c8d9e0f/binary?token=x", "/file/*/binary"),
        ("https://api.media.atlassian.com/file/abc;sig=signed-secret/binary", "/file/abc"),
    ],
)
def test_hop_log_path_shapes_mask_identifiers_and_drop_parameters(url: str, shape: str) -> None:
    """Only plain-word segments survive, and nothing after a query or parameter separator is logged."""
    assert atlassian_client._path_shape(url) == shape


@pytest.mark.asyncio
async def test_failed_storage_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A download that cannot be retained reports a storage failure and registers nothing."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _file())
    monkeypatch.setattr(atlassian_module, "register_bytes_attachment", lambda *_args, **_kwargs: None)

    with tool_runtime_context(context):
        result = json.loads(await tool.confluence_download_attachment(page_id="123", attachment_id="att456"))
        current = get_tool_runtime_context()
        assert current is not None
        assert list_tool_runtime_attachment_ids(current) == []

    assert result["code"] == "attachment_store_failed"


@pytest.mark.asyncio
async def test_download_above_the_retained_media_limit_is_too_large(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inline transfer limit above MindRoom's retained media limit still reports a size error."""
    tool, gateway, context, _paths = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(media_module, "_matrix_media_max_bytes", 4)
    gateway.route("GET", gateway_url("confluence", DOWNLOAD_PATH), _file(b"12345"))

    result = await _download(tool, context)

    assert result["code"] == "attachment_too_large"
    assert not (tmp_path / "storage" / "incoming_media").exists()
