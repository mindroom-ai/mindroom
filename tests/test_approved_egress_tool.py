"""Tests for the built-in approved egress toolkit."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config, RuntimeConfig
from mindroom.config.models import ModelConfig
from mindroom.egress import policy as egress_policy_module
from mindroom.tools import approved_egress as approved_egress_module
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
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
                ["metadata.google.internal"],
                5,
                "Need metadata",
            ),
        )


def test_request_network_access_rejects_whole_batch_on_one_bad_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One invalid hostname must fail the whole batch before any grant is created."""

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        msg = "a rejected batch should not create any grant"
        raise AssertionError(msg)

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)

    with pytest.raises(ValueError, match="points at an internal name"):
        asyncio.run(
            _approved_egress_tool().request_network_access(
                ["docs.example.com", "metadata.google.internal"],
                5,
                "Need docs and metadata",
            ),
        )


def test_request_network_access_rejects_bare_string_hostnames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare string must not be iterated as characters."""

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        msg = "a rejected batch should not create any grant"
        raise AssertionError(msg)

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)

    with pytest.raises(TypeError, match="hostnames must be a list"):
        asyncio.run(
            _approved_egress_tool().request_network_access(
                "docs.example.com",  # type: ignore[arg-type]
                5,
                "Need docs",
            ),
        )


def test_request_network_access_rejects_empty_hostnames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty hostname list is a caller error, not a silent no-op."""

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        msg = "an empty batch should not create any grant"
        raise AssertionError(msg)

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)

    with pytest.raises(ValueError, match="hostnames must not be empty"):
        asyncio.run(_approved_egress_tool().request_network_access([], 5, "Need nothing"))


def test_request_network_access_caps_batch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized batches must fail so one approval card stays reviewable."""

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        msg = "an oversized batch should not create any grant"
        raise AssertionError(msg)

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)
    hostnames = [f"host{index}.example.com" for index in range(21)]

    with pytest.raises(ValueError, match="at most 20 hostnames"):
        asyncio.run(_approved_egress_tool().request_network_access(hostnames, 5, "Need many hosts"))


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
            ["docs.example.com", "api.example.com"],
            5,
            "Need documentation",
        ),
    )

    assert "Already allowed by the static egress allowlist: docs.example.com, api.example.com" in result
    assert "No temporary grant was created" in result


def test_request_network_access_posts_worker_key_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    runtime_paths = test_runtime_paths(tmp_path)
    config = RuntimeConfig.from_authored(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", worker_scope="user_agent")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )

    context = SimpleNamespace(
        agent_name="assistant",
        room_id="!room:server",
        resolved_thread_id=None,
        thread_id="$thread",
        requester_id="@user:server",
        config=config,
        runtime_paths=runtime_paths,
    )
    monkeypatch.setattr(approved_egress_module, "get_tool_runtime_context", lambda: context)
    monkeypatch.setattr(
        approved_egress_module,
        "build_execution_identity_from_runtime_context",
        lambda _context: object(),
    )
    monkeypatch.setattr(
        egress_policy_module,
        "resolve_worker_key",
        lambda *_args, **_kwargs: "v1:default:user_agent:@user:server:assistant",
    )

    try:
        result = asyncio.run(
            _approved_egress_tool().request_network_access(
                ["docs.example.com"],
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
    tmp_path: Path,
) -> None:
    """Grant posting should not block other coroutines on the event loop."""
    marker = threading.Event()

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        if not marker.wait(timeout=0.5):
            msg = "event loop was blocked by grant request"
            raise AssertionError(msg)
        return {"expires_at": 123}

    runtime_paths = test_runtime_paths(tmp_path)
    config = RuntimeConfig.from_authored(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", worker_scope="shared")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )

    context = SimpleNamespace(
        agent_name="assistant",
        room_id="!room:server",
        resolved_thread_id=None,
        thread_id="$thread",
        requester_id="@user:server",
        config=config,
        runtime_paths=runtime_paths,
    )
    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)
    monkeypatch.setattr(approved_egress_module, "get_tool_runtime_context", lambda: context)

    async def invoke_tool() -> str:
        task = asyncio.create_task(
            _approved_egress_tool().request_network_access(
                ["docs.example.com"],
                5,
                "Need docs",
            ),
        )
        await asyncio.sleep(0)
        marker.set()
        return await task

    result = asyncio.run(invoke_tool())

    assert result.startswith("Approved temporary network access to docs.example.com")


def _shared_worker_context() -> SimpleNamespace:
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    config = RuntimeConfig.from_authored(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", worker_scope="shared")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    return SimpleNamespace(
        agent_name="assistant",
        room_id="!room:server",
        resolved_thread_id=None,
        thread_id="$thread",
        requester_id="@user:server",
        config=config,
        runtime_paths=runtime_paths,
    )


def test_request_network_access_posts_one_grant_per_blocked_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One call dedupes hostnames, skips static-allowed ones, and grants each blocked one."""
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST", ".example.com")
    payloads: list[dict[str, object]] = []

    def post_grant(payload: dict[str, object]) -> dict[str, object]:
        payloads.append(payload)
        return {"expires_at": 123}

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)
    monkeypatch.setattr(approved_egress_module, "get_tool_runtime_context", _shared_worker_context)

    result = asyncio.run(
        _approved_egress_tool().request_network_access(
            ["api.other.test", "docs.example.com", "cdn.other.test", "api.other.test"],
            5,
            "Need APIs",
        ),
    )

    assert [payload["hostname"] for payload in payloads] == ["api.other.test", "cdn.other.test"]
    assert all(payload["ttl_seconds"] == 300 for payload in payloads)
    assert result.startswith("Approved temporary network access to api.other.test, cdn.other.test for 5 minutes.")
    assert "Already allowed by the static egress allowlist (no grant needed): docs.example.com." in result


def test_request_network_access_reports_per_host_expiry_when_grants_differ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Differing grant expiries must be reported per hostname, not as one shared timestamp."""
    expiries = iter([123, 124])

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        return {"expires_at": next(expiries)}

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)
    monkeypatch.setattr(approved_egress_module, "get_tool_runtime_context", _shared_worker_context)

    result = asyncio.run(
        _approved_egress_tool().request_network_access(
            ["api.other.test", "cdn.other.test"],
            5,
            "Need APIs",
        ),
    )

    assert "api.other.test expires at Unix time 123." in result
    assert "cdn.other.test expires at Unix time 124." in result


@pytest.mark.parametrize("failure", [RuntimeError, OSError])
def test_request_network_access_reports_partial_grants_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[Exception],
) -> None:
    """A mid-batch policy or socket failure must report which hostnames already received grants."""

    def post_grant(payload: dict[str, object]) -> dict[str, object]:
        if payload["hostname"] == "cdn.other.test":
            msg = "policy service is down"
            raise failure(msg)
        return {"expires_at": 123}

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)
    monkeypatch.setattr(approved_egress_module, "get_tool_runtime_context", _shared_worker_context)

    with pytest.raises(
        RuntimeError,
        match=r"created grants for api\.other\.test but failed to grant cdn\.other\.test: policy service is down",
    ):
        asyncio.run(
            _approved_egress_tool().request_network_access(
                ["api.other.test", "cdn.other.test"],
                5,
                "Need APIs",
            ),
        )
