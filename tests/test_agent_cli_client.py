"""Stdlib transport never follows redirects or exposes capability contents."""

# ruff: noqa: D103
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from mindroom.agent_cli.client import AgentCliClient, AgentCliUnavailableError
from mindroom.agent_cli.main import main


@pytest.mark.parametrize("redirect", [False, True])
def test_real_http_client_and_redirect_fence(monkeypatch: pytest.MonkeyPatch, redirect: bool) -> None:
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                ),
            )
            self.send_response(307 if redirect else 200)
            if redirect:
                self.send_header("Location", f"http://localhost:{self.server.server_port}/stolen")
            self.end_headers()
            self.wfile.write(b'{"status":"queued"}')

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib override
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", "private-capability")
    # Shells inherit proxy settings, but the grant must go straight to MindRoom, never to a proxy.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    payload = {"operation": "tools.call", "call_id": str(uuid4()), "toolkit": "a", "function": "b", "arguments": {}}
    try:
        if redirect:
            with pytest.raises(AgentCliUnavailableError) as error:
                AgentCliClient().operation(payload)
            assert "private-capability" not in str(error.value)
        else:
            assert AgentCliClient().operation(payload) == {"status": "queued"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert seen == [("/api/agent-cli/operations", "Bearer private-capability", payload)]


@pytest.mark.parametrize("timeout", [0, 1])
def test_wait_timeout_returns_pending_receipt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    timeout: int,
) -> None:
    call_id = str(uuid4())
    now = 0.0
    polls: list[float] = []

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr("mindroom.agent_cli.main.time.monotonic", lambda: now)
    monkeypatch.setattr("mindroom.agent_cli.main.time.sleep", advance)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            polls.append(now)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"call_id": call_id, "status": "waiting"}).encode())

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib override
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", "private-capability")
    try:
        assert main(["calls", "wait", call_id, "--timeout", str(timeout)]) == 3
        assert json.loads(capsys.readouterr().out) == {"call_id": call_id, "status": "waiting"}
        assert polls[0] == 0
        assert all(poll < timeout for poll in polls[1:])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            b'{"detail":"This shell command\'s Bash call has ended; call mindroom-agent from a Bash call that is still running"}',
            "Agent CLI request was rejected: This shell command's Bash call has ended; call mindroom-agent from a Bash call that is still running",
        ),
        (b"<html>conflict</html>", "Agent CLI request was rejected"),
    ],
)
def test_rejection_relays_server_detail(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    expected: str,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(409)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib override
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", "private-capability")
    payload = {"operation": "tools.call", "call_id": str(uuid4()), "toolkit": "a", "function": "b", "arguments": {}}
    try:
        with pytest.raises(ValueError, match="rejected") as error:
            AgentCliClient().operation(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert str(error.value) == expected


@pytest.mark.parametrize("token", ["", "private capability", "private-capabilité", "x" * 4097])
def test_malformed_grant_is_unavailable_without_echo(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", token)
    with pytest.raises(AgentCliUnavailableError, match="authority is unavailable") as error:
        AgentCliClient()
    assert "capabilit" not in str(error.value)


def test_unreachable_api_names_the_address_but_not_the_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    monkeypatch.setenv("MINDROOM_AGENT_CLI_URL", url)
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN", "private-capability")
    payload = {"operation": "tools.call", "call_id": str(uuid4()), "toolkit": "a", "function": "b", "arguments": {}}
    with pytest.raises(AgentCliUnavailableError, match="outcome is unknown") as error:
        AgentCliClient().operation(payload)
    assert url in str(error.value)
    assert "private-capability" not in str(error.value)
