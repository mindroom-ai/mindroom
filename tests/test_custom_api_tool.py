"""Native custom API toolkit contract, request formatting, and defensive policy checks."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.redaction import REDACTED
from mindroom.server_fetch_url import ServerFetchUrlError
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools import custom_api as custom_api_module
from mindroom.tools.custom_api import CustomApiTools, custom_api_tools

if TYPE_CHECKING:
    from collections.abc import Callable


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Route CustomApiTools transport calls through an in-memory HTTPX MockTransport."""
    sent: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        response = handler(request)
        return httpx.Response(response.status_code, headers=response.headers, stream=httpx.ByteStream(response.content))

    monkeypatch.setattr(
        custom_api_module,
        "ServerFetchAsyncHTTPTransport",
        lambda **_kwargs: httpx.MockTransport(record),
    )
    return sent


def test_custom_api_toolkit_is_native_and_respects_enable_switches() -> None:
    """The custom_api factory returns the native CustomApiTools class honoring enable switches."""
    tool_class = custom_api_tools()

    assert tool_class is CustomApiTools
    assert issubclass(tool_class, Toolkit)
    assert tool_class().name == "api_tools"
    assert list(tool_class().functions) == ["make_request"]
    assert list(tool_class(enable_make_request=False).functions) == []
    assert list(tool_class(enable_make_request=False, all=True).functions) == ["make_request"]


def test_custom_api_handles_json_text_non_2xx_and_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom API requests send query, JSON, and headers, decode JSON or text, and report HTTP/transport errors."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/items":
            return httpx.Response(
                201,
                headers={"content-type": "application/json", "set-cookie": "session=secret"},
                json={"received": json.loads(request.content), "query": dict(request.url.params)},
            )
        if request.url.path == "/v1/plain":
            return httpx.Response(502, headers={"content-type": "text/plain"}, content=b"upstream down")
        msg = "connection reset"
        raise httpx.ConnectError(msg, request=request)

    sent = _install_transport(monkeypatch, respond)
    toolkit = CustomApiTools(base_url="https://api.example.com/v1", api_key="sk-test", headers={"X-App": "mindroom"})

    created = json.loads(
        toolkit.make_request(
            "items",
            method="POST",
            params={"page": "2"},
            headers={"X-Trace": "t-1"},
            json_data={"name": "lamp"},
        ),
    )
    assert created["status_code"] == 201
    assert created["data"] == {"received": {"name": "lamp"}, "query": {"page": "2"}}
    assert created["headers"]["set-cookie"] == REDACTED
    assert "error" not in created
    assert sent[0].headers["Authorization"] == "Bearer sk-test"
    assert sent[0].headers["X-App"] == "mindroom"
    assert sent[0].headers["X-Trace"] == "t-1"
    assert sent[0].headers["Accept-Encoding"] == "identity"

    failed = json.loads(toolkit.make_request("/plain"))
    assert failed["status_code"] == 502
    assert failed["data"] == {"text": "upstream down"}
    assert failed["error"] == "Request failed"

    errored = json.loads(toolkit.make_request("boom"))
    assert errored == {"error": "Request failed: connection reset"}


def test_custom_api_enforces_defensive_destination_and_credential_policies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Private destinations, unanchored credentials, and off-origin credential forwarding stay blocked."""
    sent = _install_transport(
        monkeypatch,
        lambda request: (
            httpx.Response(302, headers={"Location": "https://cdn.example/file"})
            if request.url.host == "api.example.com"
            else httpx.Response(200, json={"ok": True})
        ),
    )

    with pytest.raises(ServerFetchUrlError):
        CustomApiTools().make_request("http://127.0.0.1:8080/private")

    unanchored = json.loads(CustomApiTools(api_key="sk-test").make_request("https://example.com/data"))
    assert "base_url" in unanchored["error"]
    assert sent == []

    redirected = json.loads(
        CustomApiTools(base_url="https://api.example.com", api_key="sk-test", headers={"X-Key": "secret"}).make_request(
            "start",
            headers={"Host": "other.example", "X-Trace": "keep"},
        ),
    )
    assert redirected["data"] == {"ok": True}
    assert sent[0].headers.get_list("Host") == ["api.example.com"]
    assert sent[0].headers["Authorization"] == "Bearer sk-test"
    assert sent[0].headers["X-Key"] == "secret"
    assert "Authorization" not in sent[1].headers
    assert "X-Key" not in sent[1].headers
    assert sent[1].headers["X-Trace"] == "keep"
