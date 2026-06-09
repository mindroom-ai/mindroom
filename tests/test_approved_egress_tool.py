"""Tests for the built-in approved egress toolkit."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.tools import approved_egress as approved_egress_module

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools import Toolkit


def _approved_egress_tool() -> Toolkit:
    return approved_egress_module.approved_egress_tools()()


@pytest.fixture(autouse=True)
def _approved_egress_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MINDROOM_APPROVED_EGRESS_ALLOWLIST",
        "MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH",
        "MINDROOM_EGRESS_ALLOWLIST_PATH",
        "MINDROOM_APPROVED_EGRESS_API_URL",
        "MINDROOM_APPROVED_EGRESS_TOKEN",
        "MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("Docs.Example.COM.", "docs.example.com"),
        ("münchen.example", "xn--mnchen-3ya.example"),
    ],
)
def test_canonical_hostname_normalizes_case_trailing_dot_and_idna(hostname: str, expected: str) -> None:
    """Hostnames should canonicalize to lowercase ASCII DNS names."""
    assert approved_egress_module._canonical_hostname(hostname) == expected


@pytest.mark.parametrize(
    ("hostname", "match"),
    [
        ("", "must not be empty"),
        ("https://docs.example.com", "scheme, path, query, or credentials"),
        ("docs.example.com/path", "scheme, path, query, or credentials"),
        ("user@docs.example.com", "scheme, path, query, or credentials"),
        ("docs.example.com:443", "must not include a port"),
        ("*.example.com", "wildcards are not supported"),
        ("203.0.113.7", "IP literals are not valid"),
        ("example", "fully-qualified external DNS name"),
        ("localhost", "fully-qualified external DNS name"),
        ("metadata.google.internal", "points at an internal name"),
        ("foo.svc.cluster.local", "points at an internal name"),
        ("api.svc", "points at an internal name"),
        ("foo.localhost", "points at an internal name"),
        ("bad..example.com", "not valid IDNA"),
        (f"{'a' * 64}.example.com", "not valid IDNA"),
        ("-bad.example.com", "not a valid DNS name"),
        ("foo_bar.example.com", "contains unsupported characters"),
    ],
)
def test_canonical_hostname_rejects_invalid_and_internal_names(hostname: str, match: str) -> None:
    """The hostname gate should reject malformed, internal, and non-DNS inputs."""
    with pytest.raises(ValueError, match=match):
        approved_egress_module._canonical_hostname(hostname)


def test_request_network_access_rejects_internal_hostname_before_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal hostnames must never reach the policy API."""

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        msg = "rejected hostname should not create a grant"
        raise AssertionError(msg)

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)

    with pytest.raises(ValueError, match="points at an internal name"):
        asyncio.run(
            _approved_egress_tool().request_network_access(
                "metadata.google.internal",
                5,
                "Need metadata",
            ),
        )


def test_effective_ttl_rejects_non_integer_max_ttl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed max-TTL override must fail loudly instead of restoring the default cap."""
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS", "60s")

    with pytest.raises(RuntimeError, match="MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS must be an integer"):
        approved_egress_module._effective_ttl_seconds(300)


def test_request_network_access_skips_grant_when_static_allowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Static allowlist matches should not call the policy API."""
    allowlist_path = tmp_path / "allowed-domains.txt"
    allowlist_path.write_text(".example.com\n", encoding="utf-8")
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH", str(allowlist_path))

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        msg = "static allowlist match should not create a grant"
        raise AssertionError(msg)

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)

    result = asyncio.run(
        _approved_egress_tool().request_network_access(
            "docs.example.com",
            5,
            "Need documentation",
        ),
    )

    assert "docs.example.com is already allowed" in result
    assert "No temporary grant was created" in result


def test_request_network_access_posts_worker_key_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-agent workers should receive worker-key scoped grants."""
    captured: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["content-length"]))
            captured["path"] = self.path
            captured["authorization"] = self.headers["authorization"]
            captured["payload"] = json.loads(body.decode("utf-8"))
            response = json.dumps({"ok": True, "grant": {"expires_at": 123}}).encode()
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_TOKEN", "token")

    class Config:
        def get_agent_execution_scope(self, agent_name: str) -> str:
            self.agent_name = agent_name
            return "user_agent"

    context = SimpleNamespace(
        agent_name="assistant",
        room_id="!room:server",
        resolved_thread_id=None,
        thread_id="$thread",
        requester_id="@user:server",
        config=Config(),
        runtime_paths=object(),
    )
    monkeypatch.setattr(approved_egress_module, "get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        approved_egress_module,
        "build_execution_identity_from_runtime_context",
        lambda _context: object(),
    )
    monkeypatch.setattr(
        approved_egress_module,
        "resolve_worker_key",
        lambda *_args, **_kwargs: "v1:default:user_agent:@user:server:assistant",
    )

    try:
        result = asyncio.run(
            _approved_egress_tool().request_network_access(
                "docs.example.com",
                5,
                "Need docs",
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result.startswith("Approved temporary network access to docs.example.com")
    assert captured["path"] == "/grants"
    assert captured["authorization"] == "Bearer token"
    assert captured["payload"] == {
        "agent_name": "assistant",
        "approved_by": "@user:server",
        "hostname": "docs.example.com",
        "reason": "Need docs",
        "requester_id": "@user:server",
        "room_id": "!room:server",
        "subject": "v1:default:user_agent:@user:server:assistant",
        "subject_type": "worker_key",
        "thread_id": "$thread",
        "ttl_seconds": 300,
    }


def test_post_grant_surfaces_policy_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Policy API HTTP errors should preserve JSON error details."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            response = json.dumps({"ok": False, "error": "bad token"}).encode()
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_TOKEN", "token")

    try:
        with pytest.raises(RuntimeError, match="bad token"):
            approved_egress_module._post_grant({"hostname": "docs.example.com"})
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_post_grant_rejects_success_payload_from_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Policy API HTTP errors must never create grants."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            response = json.dumps({"ok": True, "grant": {"expires_at": 123}}).encode()
            self.send_response(500)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_API_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_TOKEN", "token")

    try:
        with pytest.raises(RuntimeError, match="HTTP 500"):
            approved_egress_module._post_grant({"hostname": "docs.example.com"})
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_request_network_access_posts_grant_without_blocking_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grant posting should not block other coroutines on the event loop."""
    marker = threading.Event()

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        if not marker.wait(timeout=0.5):
            msg = "event loop was blocked by grant request"
            raise AssertionError(msg)
        return {"expires_at": 123}

    class Config:
        def get_agent_execution_scope(self, agent_name: str) -> str:
            self.agent_name = agent_name
            return "shared"

    context = SimpleNamespace(
        agent_name="assistant",
        room_id="!room:server",
        resolved_thread_id=None,
        thread_id="$thread",
        requester_id="@user:server",
        config=Config(),
        runtime_paths=object(),
    )
    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)
    monkeypatch.setattr(approved_egress_module, "get_tool_runtime_context", lambda: context)

    async def invoke_tool() -> str:
        task = asyncio.create_task(
            _approved_egress_tool().request_network_access(
                "docs.example.com",
                5,
                "Need docs",
            ),
        )
        await asyncio.sleep(0)
        marker.set()
        return await task

    result = asyncio.run(invoke_tool())

    assert result.startswith("Approved temporary network access to docs.example.com")
