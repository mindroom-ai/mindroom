"""Stdlib transport never follows redirects or exposes capability contents."""

# ruff: noqa: D103
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from mindroom.agent_cli.client import AgentCliClient, AgentCliUnavailableError
from mindroom.agent_cli.main import main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("redirect", [False, True])
def test_real_http_client_and_redirect_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redirect: bool) -> None:
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
    token = tmp_path / "token"
    token.write_text("private-capability")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_GATEWAY_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN_PATH", str(token))
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
    tmp_path: Path,
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
    token = tmp_path / "token"
    token.write_text("private-capability")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_GATEWAY_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN_PATH", str(token))
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
            b'{"detail":"Agent CLI tool commands require an active Bash call"}',
            "Agent CLI request was rejected: Agent CLI tool commands require an active Bash call",
        ),
        (b"<html>conflict</html>", "Agent CLI request was rejected"),
    ],
)
def test_rejection_relays_server_detail(
    tmp_path: Path,
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
    token = tmp_path / "token"
    token.write_text("private-capability")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_GATEWAY_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_AGENT_CLI_TOKEN_PATH", str(token))
    payload = {"operation": "tools.call", "call_id": str(uuid4()), "toolkit": "a", "function": "b", "arguments": {}}
    try:
        with pytest.raises(ValueError, match="rejected") as error:
            AgentCliClient().operation(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert str(error.value) == expected
