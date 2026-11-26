"""Tests for the shared stdlib-only script gateway transport."""

from __future__ import annotations

import ast
import importlib
import json
import socket
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import mindroom.workflow as workflow_sdk
from mindroom.script_runs.launch_contract import GATEWAY_URL_ENV, MAX_TOKEN_BYTES, RUN_ID_ENV, TOKEN_PATH_ENV
from mindroom.script_runs.transport import (
    GatewayDecodeError,
    GatewayHTTPError,
    GatewayTransportError,
    ScriptGatewayTransport,
)
from mindroom.script_sdk import MindRoomToolCallError, MindRoomTools

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_TOKEN = "private-script-capability"  # noqa: S105 - test placeholder, not a real credential.


@contextmanager
def _serve(
    respond: Callable[[BaseHTTPRequestHandler], None],
) -> Iterator[tuple[str, list[tuple[str, str, str | None, str | None, bytes]]]]:
    seen: list[tuple[str, str, str | None, str | None, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            seen.append(
                (self.command, self.path, self.headers.get("Authorization"), self.headers.get("Content-Type"), body),
            )
            respond(self)

        do_GET = do_POST = _handle  # noqa: N815 - stdlib HTTP method dispatch names.

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib override.
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/script-gateway", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def _workflow_lane() -> Iterator[None]:
    workflow_sdk._gateway.cache_clear()
    workflow_sdk._local.lane = workflow_sdk._Lane("")
    try:
        yield
    finally:
        workflow_sdk._local.lane = None
        workflow_sdk._gateway.cache_clear()


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    gateway_url: str = "http://127.0.0.1:8765/api/script-gateway",
    run_id: str = "run-1",
    token_bytes: bytes | None = None,
) -> Path:
    token_path = tmp_path / "capability"
    token_path.write_bytes(f"{_TOKEN}\n".encode() if token_bytes is None else token_bytes)
    monkeypatch.setenv(GATEWAY_URL_ENV, gateway_url)
    monkeypatch.setenv(RUN_ID_ENV, run_id)
    monkeypatch.setenv(TOKEN_PATH_ENV, str(token_path))
    return token_path


def _send(handler: BaseHTTPRequestHandler, status: int, body: bytes, *, headers: dict[str, str] | None = None) -> None:
    handler.send_response(status)
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def test_transport_bypasses_env_proxy_and_sends_auth_and_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Proxy env vars must never intercept capability-bearing gateway calls."""
    seen_state_headers: list[str | None] = []

    def respond(handler: BaseHTTPRequestHandler) -> None:
        seen_state_headers.append(handler.headers.get("X-Workflow-State"))
        _send(handler, 200, b'{"ok":true}')

    with _serve(respond) as (url, seen):
        _configure(monkeypatch, tmp_path, gateway_url=f"{url}/")
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        transport = ScriptGatewayTransport(timeout_seconds=5.0)
        assert (transport.gateway_url, transport.run_id) == (url, "run-1")
        assert transport.request_json("POST", "calls", {"a": 1}) == {"ok": True}
        assert transport.request_json("GET", "/runs/run-1", headers={"X-Workflow-State": "running"}) == {"ok": True}

    assert seen == [
        ("POST", "/api/script-gateway/calls", f"Bearer {_TOKEN}", "application/json", b'{"a":1}'),
        ("GET", "/api/script-gateway/runs/run-1", f"Bearer {_TOKEN}", None, b""),
    ]
    assert seen_state_headers == [None, "running"]


def test_redirect_is_refused_without_following_or_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A 307 redirect is never followed across transport, script SDK (indeterminate), and workflow SDK."""

    def respond(handler: BaseHTTPRequestHandler) -> None:
        _send(handler, 307, b"", headers={"Location": f"http://localhost:{handler.server.server_port}/stolen"})

    with _serve(respond) as (url, seen):
        _configure(monkeypatch, tmp_path, gateway_url=url)
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        with pytest.raises(GatewayHTTPError) as transport_err:
            ScriptGatewayTransport(timeout_seconds=5.0).request_json("POST", "calls", {"x": 1})
        assert transport_err.value.status == 307
        assert _TOKEN not in str(transport_err.value)

        with pytest.raises(MindRoomToolCallError) as script_err:
            MindRoomTools(http_timeout_seconds=5.0, poll_interval_seconds=0.0001).call("website", "read_url")
        assert (script_err.value.kind, script_err.value.retryable, script_err.value.state) == (
            "unexpected_redirect",
            False,
            None,
        )
        assert _TOKEN not in str(script_err.value)

        with _workflow_lane(), pytest.raises(workflow_sdk.WorkflowError, match="307") as wf_err:
            workflow_sdk.tool("files", "read", path="a.txt")
        assert _TOKEN not in str(wf_err.value)

    assert [path for _method, path, *_rest in seen] == [
        "/api/script-gateway/calls",
        "/api/script-gateway/calls",
        "/api/script-gateway/workflow/run-1/nodes",
    ]


@pytest.mark.parametrize("env_name", [GATEWAY_URL_ENV, RUN_ID_ENV, TOKEN_PATH_ENV])
@pytest.mark.parametrize("value", [None, "   "])
def test_missing_or_blank_env_var_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env_name: str,
    value: str | None,
) -> None:
    """Every required launcher env var must be present and non-blank."""
    _configure(monkeypatch, tmp_path)
    if value is None:
        monkeypatch.delenv(env_name, raising=False)
    else:
        monkeypatch.setenv(env_name, value)

    with pytest.raises(RuntimeError, match=rf"^{env_name} is required inside a MindRoom background script\.$"):
        ScriptGatewayTransport(timeout_seconds=5.0)


@pytest.mark.parametrize(
    "bad_url",
    [
        "ftp://127.0.0.1:8765/api",
        "file:///etc/passwd",
        "not-a-url",
        "http://",
        "http://user:pass@127.0.0.1:8765/api",
        "http://user@127.0.0.1:8765/api",
        "http://@127.0.0.1:8765/api",
        "http://127.0.0.1:8765/api?token=x",
        "http://127.0.0.1:8765/api#frag",
        "http://127.0.0.1:notaport/api",
        "http://127.0.0.1:8765/api path",
    ],
)
def test_invalid_gateway_url_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_url: str) -> None:
    """Only plain HTTP(S) base URLs without credentials, query, fragment, or whitespace are accepted."""
    _configure(monkeypatch, tmp_path, gateway_url=bad_url)

    with pytest.raises(RuntimeError, match=rf"^{GATEWAY_URL_ENV} must be an HTTP\(S\) URL\.$"):
        ScriptGatewayTransport(timeout_seconds=5.0)


def test_missing_or_directory_token_path_reports_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing or non-file capability path reports the stable unavailable error."""
    token_path = _configure(monkeypatch, tmp_path)
    token_path.unlink()
    with pytest.raises(RuntimeError, match=r"^The MindRoom script capability token is unavailable\.$"):
        ScriptGatewayTransport(timeout_seconds=5.0)

    monkeypatch.setenv(TOKEN_PATH_ENV, str(tmp_path))
    with pytest.raises(RuntimeError, match=r"^The MindRoom script capability token is unavailable\.$"):
        ScriptGatewayTransport(timeout_seconds=5.0)


@pytest.mark.parametrize(
    "token_bytes",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"   \n\t ", id="whitespace-only"),
        pytest.param(b"x" * (MAX_TOKEN_BYTES + 1), id="oversized"),
        pytest.param("capabilité".encode(), id="non-ascii"),
        pytest.param(b"\xff\xfe", id="invalid-utf8"),
        pytest.param(b"secret token", id="internal-space"),
        pytest.param(b"secret\ntoken", id="internal-newline"),
    ],
)
def test_invalid_token_file_contents_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token_bytes: bytes,
) -> None:
    """Malformed, oversized, non-ASCII, or whitespace-containing tokens are rejected before sending requests."""
    _configure(monkeypatch, tmp_path, token_bytes=token_bytes)

    with pytest.raises(RuntimeError, match=r"^The MindRoom script capability token is invalid\.$") as exc_info:
        ScriptGatewayTransport(timeout_seconds=5.0)
    assert "secret" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("status", "body", "expected_payload", "expected_detail"),
    [
        (
            413,
            b'{"bytes":123,"limit":100,"detail":"too large"}',
            {"bytes": 123, "limit": 100, "detail": "too large"},
            "too large",
        ),
        (400, b'{"detail":""}', {"detail": ""}, None),
        (502, b"<html>bad gateway</html>", None, None),
        (500, b'["not","a","dict"]', None, None),
    ],
)
def test_http_error_parses_payload_detail_and_sent_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: int,
    body: bytes,
    expected_payload: dict[str, object] | None,
    expected_detail: str | None,
) -> None:
    """GatewayHTTPError exposes structured payload/detail when present, tolerates non-JSON, and tracks sent_bytes."""
    with _serve(lambda handler: _send(handler, status, body)) as (url, _seen):
        _configure(monkeypatch, tmp_path, gateway_url=url)
        request_body = {"key": "value"}

        with pytest.raises(GatewayHTTPError) as exc_info:
            ScriptGatewayTransport(timeout_seconds=5.0).request_json("POST", "calls", request_body)

    err = exc_info.value
    assert (err.status, err.payload, err.detail) == (status, expected_payload, expected_detail)
    assert err.sent_bytes == len(json.dumps(request_body, separators=(",", ":")).encode("utf-8"))
    assert err.reason


def test_unreachable_gateway_raises_transport_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Connection failures translate to GatewayTransportError."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        unused_port = sock.getsockname()[1]
    _configure(monkeypatch, tmp_path, gateway_url=f"http://127.0.0.1:{unused_port}/api/script-gateway")

    with pytest.raises(GatewayTransportError):
        ScriptGatewayTransport(timeout_seconds=2.0).request_json("GET", "run")


@pytest.mark.parametrize("raw_body", [b"not json", b"\xff\xfe"])
def test_invalid_2xx_json_raises_decode_error_and_workflow_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_body: bytes,
) -> None:
    """Malformed 2xx bodies raise GatewayDecodeError and become WorkflowError in workflow.py without looping."""
    with _serve(lambda handler: _send(handler, 200, raw_body)) as (url, seen):
        _configure(monkeypatch, tmp_path, gateway_url=url)

        with pytest.raises(GatewayDecodeError):
            ScriptGatewayTransport(timeout_seconds=5.0).request_json("GET", "run")

        with (
            _workflow_lane(),
            pytest.raises(
                workflow_sdk.WorkflowError,
                match=r"^MindRoom returned an invalid JSON response\.$",
            ),
        ):
            workflow_sdk.tool("files", "read", path="a.txt")

    assert len(seen) == 2


@pytest.mark.parametrize(
    ("module_name", "expected_non_stdlib"),
    [
        ("mindroom.script_runs.launch_contract", set()),
        ("mindroom.script_runs.transport", {"mindroom.script_runs.launch_contract"}),
        ("mindroom.script_sdk", {"mindroom.script_runs.launch_contract", "mindroom.script_runs.transport"}),
        ("mindroom.workflow", {"mindroom.script_runs.transport"}),
    ],
)
def test_script_process_modules_import_only_stdlib_and_declared_closure(
    module_name: str,
    expected_non_stdlib: set[str],
) -> None:
    """Every module loaded inside script processes stays within the stdlib-only import closure."""
    module = importlib.import_module(module_name)
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add(node.module or "")
    foreign = {name for name in imported if name.partition(".")[0] not in sys.stdlib_module_names}
    assert foreign == expected_non_stdlib
