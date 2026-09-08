"""Stateless MCP protocol, bounded payloads, and requester-owned cancellation."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi import HTTPException
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Route

from mindroom.mcp_gateway.execution import retain_execution_task
from mindroom.mcp_gateway.server import GatewayServer
from mindroom.mcp_gateway.types import GatewayPrincipal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.requests import Request

pytestmark = pytest.mark.asyncio

_HEADERS = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer alice"}


async def _authenticate(request: Request) -> GatewayPrincipal:
    header = request.headers.get("authorization")
    if header not in {"Bearer alice", "Bearer alice2", "Bearer alice3", "Bearer bob"}:
        raise HTTPException(
            401,
            "Invalid gateway token",
            headers={
                "WWW-Authenticate": 'Bearer resource_metadata="https://portal.example.org/.well-known/oauth-protected-resource/mcp"',
            },
        )
    token = header.removeprefix("Bearer ")
    return GatewayPrincipal(grant_id="grant-" + token, requester_id="alice" if token.startswith("alice") else "bob")


@asynccontextmanager
async def _client(
    dispatch: Callable[[Request, str, dict[str, object]], Awaitable[dict[str, object]]],
    *,
    deadline_seconds: float = 60,
    max_active_calls: int = 128,
    max_user_calls: int = 32,
    max_grant_calls: int = 16,
    record_activity: Callable[[Request], Awaitable[None]] | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    server = GatewayServer(
        authenticate=_authenticate,
        dispatch=dispatch,
        public_url="https://portal.example.org",
        timeout_seconds=deadline_seconds,
        max_active_calls=max_active_calls,
        max_user_calls=max_user_calls,
        max_grant_calls=max_grant_calls,
        record_activity=record_activity,
    )
    app = Starlette(routes=[Route("/mcp", endpoint=server, methods=["GET", "POST", "DELETE"])])
    async with (
        server.run(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://portal.example.org",
            headers=_HEADERS,
        ) as client,
    ):
        yield client


def _call(
    request_id: int | str = 1,
    *,
    name: str = "search_tools",
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    }


def _cancel(request_id: int | str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}}


async def test_activity_records_successful_discovery_and_calls_only() -> None:
    """Malformed or failed requests cannot extend an idle grant through transport hooks."""
    activity: list[str] = []

    async def record(request: Request) -> None:
        activity.append(request.headers["authorization"])

    async def dispatch(_request: Request, _name: str, arguments: dict[str, object]) -> dict[str, object]:
        return (
            {"error": {"code": "tool_unavailable", "message": "Unavailable"}}
            if arguments.get("query")
            else {"tools": []}
        )

    async with _client(dispatch, record_activity=record) as client:
        await client.post("/mcp", json=_call(name="unknown"))
        await client.post("/mcp", json=_call(arguments={"query": "unavailable"}))
        await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "invented"})
        assert activity == []
        result = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert "tools" in result.json()["result"]
        assert activity == ["Bearer alice"]
        result = await client.post("/mcp", json=_call())
        assert result.json()["result"]["isError"] is False
        assert activity == ["Bearer alice", "Bearer alice"]


async def test_gateway_lists_only_static_bounded_meta_tools() -> None:
    """Adding hundreds of integrations cannot enter initial model context."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        pytest.fail("Initial tool list must not invoke provider discovery")

    async with _client(dispatch) as client:
        initialized = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        assert "mcp-session-id" not in initialized.headers
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert response.status_code == 200
        tools = response.json()["result"]["tools"]
        assert {tool["name"] for tool in tools} == {"search_tools", "get_tool", "invoke_tool"}
        assert len(response.content) < 8192


async def test_every_request_authenticates_its_own_identity() -> None:
    """SDK background context must not retain an earlier requester's identity."""

    async def dispatch(request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"user": request.headers["authorization"]}

    async with _client(dispatch) as client:
        alice = await client.post("/mcp", json=_call())
        bob = await client.post("/mcp", json=_call(), headers={"Authorization": "Bearer bob"})
        missing = await client.post("/mcp", json=_call(), headers={"Authorization": ""})
        assert alice.json()["result"]["structuredContent"]["user"] == "Bearer alice"
        assert bob.json()["result"]["structuredContent"]["user"] == "Bearer bob"
        assert missing.status_code == 401
        assert "resource_metadata=" in missing.headers["www-authenticate"]


@pytest.mark.parametrize(
    "body",
    [_call(name="arbitrary"), _call(arguments={"limit": 999}), _call(arguments={"agent_name": "other"})],
)
async def test_meta_tool_names_and_inputs_cannot_bypass_static_surface(body: dict[str, object]) -> None:
    """Unknown operations and targeting fields never reach the dispatcher."""
    calls: list[str] = []

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        calls.append(_name)
        return {"ok": True}

    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=body)
        result = response.json()
        assert "error" in result or result["result"]["isError"]
        assert calls == []


async def test_gateway_hides_backend_exception_text() -> None:
    """Provider secrets and exception payloads cannot become model output."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        message = "sensitive-test-credential"
        raise RuntimeError(message)

    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=_call())
        assert response.json()["result"]["isError"] is True
        assert "sensitive-test-credential" not in response.text


async def test_cancel_is_bound_to_grant_and_typed_request_id() -> None:
    """Another user or a stringified request ID cannot cancel an active call."""
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return {}

    async with _client(dispatch) as client:
        call = asyncio.create_task(client.post("/mcp", json=_call(12)))
        await asyncio.wait_for(started.wait(), 2)
        assert (await client.post("/mcp", json=_cancel(12), headers={"Authorization": "Bearer bob"})).status_code == 202
        assert not cancelled.is_set()
        assert (await client.post("/mcp", json=_cancel("12"))).status_code == 202
        assert not cancelled.is_set()
        assert (await client.post("/mcp", json=_cancel(12))).status_code == 202
        await asyncio.wait_for(cancelled.wait(), 2)
        response = await asyncio.wait_for(call, 2)
        assert response.status_code == 200
        assert response.json()["result"]["structuredContent"]["error"]["code"] == "cancelled"


async def test_duplicate_active_request_cannot_replace_cancellation_owner() -> None:
    """A duplicate ID must not hide the first call from cancellation."""
    started, finished = asyncio.Event(), asyncio.Event()

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
        return {}

    async with _client(dispatch) as client:
        first = asyncio.create_task(client.post("/mcp", json=_call(7)))
        await asyncio.wait_for(started.wait(), 2)
        second = await client.post("/mcp", json=_call(7))
        assert second.json()["result"]["structuredContent"]["error"]["code"] == "duplicate_request"
        await client.post("/mcp", json=_cancel(7))
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), 2)


async def test_timeout_is_bounded_and_does_not_retry_execution() -> None:
    """A deadline must end the wait without replaying a potentially mutating call."""
    calls = 0

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
        return {}

    async with _client(dispatch, deadline_seconds=0.02) as client:
        response = await client.post("/mcp", json=_call())
        assert response.json()["result"]["structuredContent"]["error"]["code"] == "timeout"
        assert calls == 1


async def test_gateway_rejects_oversized_requests_and_wrong_origins() -> None:
    """Protocol limits and origin checks also protect cancellation interception."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        pytest.fail("Rejected request reached dispatcher")

    async with _client(dispatch) as client:
        oversized = await client.post(
            "/mcp",
            content=json.dumps({"padding": "x" * 131073}),
            headers={"Content-Type": "application/json"},
        )
        assert oversized.status_code == 413
        origin = await client.post("/mcp", json=_cancel(1), headers={"Origin": "https://evil.example.org"})
        assert origin.status_code == 403


@pytest.mark.parametrize("request_id", [True, 1.0, None])
async def test_cancel_rejects_coercible_noninteger_request_ids(request_id: object) -> None:
    """Boolean and float JSON values must not acquire an integer call's authority."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {}

    async with _client(dispatch) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": request_id}},
        )
        assert response.status_code == 400


async def test_invalid_arguments_cannot_expand_sdk_error_beyond_response_limit() -> None:
    """SDK schema errors must not echo a large invalid input into model context."""
    calls: list[str] = []

    async def dispatch(_request: Request, name: str, _arguments: dict[str, object]) -> dict[str, object]:
        calls.append(name)
        return {}

    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=_call(arguments={"query": "\n" * 60000}))
        assert response.status_code == 200
        assert len(response.content) <= 131072
        assert response.json()["result"]["isError"] is True
        assert calls == []


async def test_request_id_cannot_expand_an_ordinary_response_beyond_limit() -> None:
    """Client-controlled envelope data must fit the complete response budget."""
    calls: list[str] = []

    async def dispatch(_request: Request, name: str, _arguments: dict[str, object]) -> dict[str, object]:
        calls.append(name)
        return {"result": "x" * 40000}

    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=_call("a" * 60000))
        assert response.status_code == 400
        assert len(response.content) <= 131072
        assert calls == []


async def test_response_limit_includes_valid_request_id_and_jsonrpc_envelope() -> None:
    """A result near the payload ceiling cannot overflow through its response envelope."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"result": "x" * 65460}

    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=_call("a" * 126))
        assert response.status_code == 200
        assert len(response.content) <= 131072


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        (
            {
                "jsonrpc": "2.0",
                "id": "bounded",
                "method": "tools/call",
                "params": {"name": "search_tools", "arguments": ["harmless-envelope-marker"]},
            },
            200,
            -32602,
        ),
        ({"jsonrpc": "2.0", "id": 4, "method": "harmless-envelope-marker"}, 200, -32602),
        ({"jsonrpc": "2.0", "id": 4, "method": "resources/list"}, 200, -32601),
        ({"jsonrpc": "2.0", "method": "notifications/harmless-envelope-marker"}, 202, None),
        (
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": "harmless-envelope-marker"}},
            202,
            None,
        ),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, 202, None),
        (
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": 1, "progress": 2}},
            202,
            None,
        ),
        ({"jsonrpc": "2.0", "id": 4, "result": {"value": "harmless-envelope-marker"}}, 202, None),
        ({"jsonrpc": "2.0", "id": 4, "error": {"code": -32603, "message": "harmless-envelope-marker"}}, 202, None),
        ({"jsonrpc": "2.0", "id": 4, "result": "harmless-envelope-marker"}, 400, None),
        ([{"jsonrpc": "2.0", "id": 4, "method": "harmless-envelope-marker"}], 400, None),
    ],
)
async def test_typed_envelope_privacy_preserves_protocol(
    payload: object,
    status: int,
    code: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Invalid typed messages and unsolicited responses cannot reach SDK input logging."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        pytest.fail("Envelope must not invoke a tool")

    caplog.set_level("DEBUG")
    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=payload)
        await asyncio.sleep(0.01)  # Let SDK receive-loop logging finish after HTTP acknowledgement.
        assert response.status_code == status
        assert len(response.content) < 1024
        assert "harmless-envelope-marker" not in response.text
        if code is not None:
            assert response.json()["error"]["code"] == code
            assert response.json()["id"] == payload["id"]
        if status == 202:
            assert response.content == b""
    # Manager shutdown drains asynchronous log paths before checking privacy.
    assert "harmless-envelope-marker" not in caplog.text


async def test_unknown_tool_name_never_enters_sdk_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Static name rejection must happen before SDK tool-cache warnings."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        pytest.fail("Unknown tool reached dispatch")

    caplog.set_level("DEBUG")
    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=_call(name="harmless-tool-name-marker"))
        assert response.json()["result"]["structuredContent"]["error"]["code"] == "tool_not_found"
    assert "harmless-tool-name-marker" not in caplog.text


@pytest.mark.parametrize("arguments", [{}, {"arguments": None}, {"arguments": {}}])
async def test_call_arguments_preserve_sdk_defaults(arguments: dict[str, object]) -> None:
    """Omitted and null arguments remain an empty object at dispatch."""

    async def dispatch(request: Request, name: str, values: dict[str, object]) -> dict[str, object]:
        assert name == "search_tools"
        assert values == {}
        return {"user": request.headers["authorization"]}

    async with _client(dispatch) as client:
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_tools", **arguments}},
        )
        assert response.json()["result"]["structuredContent"] == {"user": "Bearer alice"}


@pytest.mark.parametrize(
    "payload",
    [
        _cancel(1),
        {"jsonrpc": "2.0", "method": "notifications/unknown"},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        _call(),
    ],
)
@pytest.mark.parametrize("version", [b"x" * 65, b"\xe9" * 33])
async def test_protocol_header_is_bounded_before_every_dispatch(payload: object, version: bytes) -> None:
    """Early acknowledgements must also reject protocol values above 64 UTF-8 bytes."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        pytest.fail("Oversized protocol header reached dispatch")

    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=payload, headers={b"MCP-Protocol-Version": version})
        assert response.status_code == 400
        assert response.json() == {"error": "request_rejected"}
        assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize(
    "payload",
    [{"jsonrpc": "2.0", "method": "notifications/unknown"}, {"jsonrpc": "2.0", "id": 1, "result": {}}],
)
@pytest.mark.parametrize(
    ("headers", "status"),
    [({"Authorization": ""}, 401), ({"Host": "evil.example.org"}, 421), ({"Origin": "https://evil.example.org"}, 403)],
)
async def test_early_acknowledgements_keep_security_checks(
    payload: object,
    headers: dict[str, str],
    status: int,
) -> None:
    """Dropping protocol messages never bypasses bearer, Host, or Origin checks."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        pytest.fail("Rejected request reached dispatch")

    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=payload, headers=headers)
        assert response.status_code == status


@pytest.mark.parametrize(
    ("tokens", "calls_per_grant", "rejected_token", "other_user_admitted", "limits"),
    [
        (["alice"], 16, "alice", True, {}),
        (["alice", "alice2"], 16, "alice3", True, {}),
        (["alice"], 2, "alice", True, {"max_grant_calls": 2}),
        (["alice", "alice2"], 2, "alice3", True, {"max_user_calls": 4}),
        (["alice", "bob"], 1, "alice3", False, {"max_active_calls": 2}),
        (["alice"], 17, "alice", True, {"max_grant_calls": 17}),
        (["alice"], 129, "alice3", False, {"max_active_calls": 129, "max_user_calls": 256, "max_grant_calls": 256}),
        (["alice", "alice2"], 17, "alice3", True, {"max_grant_calls": 17, "max_user_calls": 34}),
    ],
)
async def test_call_fairness_and_retained_cleanup(
    tokens: list[str],
    calls_per_grant: int,
    rejected_token: str,
    other_user_admitted: bool,
    limits: dict[str, int],
) -> None:
    """Configured admission stays occupied until cancelled work and cleanup finish."""
    started: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()
    retained: list[asyncio.Task[bool]] = []
    calls: list[asyncio.Task[httpx.Response]] = []

    async def dispatch(_request: Request, _name: str, arguments: dict[str, object]) -> dict[str, object]:
        if arguments.get("query") == "probe":
            return {"admitted": True}
        cleanup = asyncio.create_task(release.wait())
        retained.append(cleanup)
        retain_execution_task(cleanup)
        started.put_nowait(None)
        await asyncio.Event().wait()
        return {}

    async with _client(dispatch, **limits) as client:
        try:
            for token in tokens:
                for request_id in range(calls_per_grant):
                    calls.append(
                        asyncio.create_task(
                            client.post("/mcp", json=_call(request_id), headers={"Authorization": f"Bearer {token}"}),
                        ),
                    )
                    await asyncio.wait_for(started.get(), 2)

            async def probe(token: str) -> httpx.Response:
                return await client.post(
                    "/mcp",
                    json=_call(100, arguments={"query": "probe"}),
                    headers={"Authorization": f"Bearer {token}"},
                )

            busy = await probe(rejected_token)
            assert busy.json()["result"]["structuredContent"]["error"]["code"] == "busy"
            bob = await probe("bob")
            if other_user_admitted:
                assert bob.json()["result"]["structuredContent"] == {"admitted": True}
            else:
                assert bob.json()["result"]["structuredContent"]["error"]["code"] == "busy"
            # Same requester, other grant cannot cancel this grant's request.
            await client.post("/mcp", json=_cancel(0), headers={"Authorization": "Bearer alice3"})
            assert not calls[0].done()
            await client.post("/mcp", json=_cancel(0))
            cancelled = await asyncio.wait_for(calls[0], 2)
            assert cancelled.json()["result"]["structuredContent"]["error"]["code"] == "cancelled"
            still_busy = await probe(rejected_token)
            assert still_busy.json()["result"]["structuredContent"]["error"]["code"] == "busy"
            duplicate = await client.post("/mcp", json=_call(0))
            assert duplicate.json()["result"]["structuredContent"]["error"]["code"] == "duplicate_request"
            release.set()
            await asyncio.gather(*retained)
            admitted = await probe(rejected_token)
            assert admitted.json()["result"]["structuredContent"] == {"admitted": True}
        finally:
            release.set()
            for call in calls:
                if not call.done():
                    call.cancel()
            await asyncio.gather(*calls, *retained, return_exceptions=True)


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "2.0", "method": "notifications/unknown"},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "unknown"},
    ],
)
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Accept": "text/plain"}, 406),
        ({"Content-Type": "text/plain"}, 400),
        ({"MCP-Protocol-Version": "unsupported"}, 400),
    ],
)
async def test_early_responses_preserve_transport_negotiation(
    payload: object,
    headers: dict[str, str],
    status: int,
) -> None:
    """Pre-SDK replies cannot turn rejected transport headers into successful acknowledgements."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        pytest.fail("Rejected request reached dispatch")

    async with _client(dispatch) as client:
        response = await client.post("/mcp", json=payload, headers=headers)
        assert response.status_code == status


async def test_sdk_client_handshake_search_and_call() -> None:
    """The real client keeps initialization, tool discovery and typed invocation working."""

    async def dispatch(request: Request, name: str, arguments: dict[str, object]) -> dict[str, object]:
        assert request.headers["authorization"] == "Bearer alice"
        if name == "search_tools":
            return {"results": [{"toolkit": "calculator", "function": "add"}]}
        assert name == "invoke_tool"
        assert arguments == {"toolkit": "calculator", "function": "add", "arguments": {"a": 1, "b": 2}}
        return {"result": 3}

    async with (
        _client(dispatch) as client,
        streamable_http_client("https://portal.example.org/mcp", http_client=client) as (read, write, _session_id),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        assert initialized.protocolVersion == "2025-11-25"
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == {"search_tools", "get_tool", "invoke_tool"}
        searched = await session.call_tool("search_tools", {})
        assert searched.structuredContent == {"results": [{"toolkit": "calculator", "function": "add"}]}
        result = await session.call_tool(
            "invoke_tool",
            {"toolkit": "calculator", "function": "add", "arguments": {"a": 1, "b": 2}},
        )
        assert result.structuredContent == {"result": 3}
        assert result.isError is False


@pytest.mark.parametrize("method", ["unknown", "tools/list"])
async def test_request_id_must_be_serializable_as_utf8(method: str) -> None:
    """An escaped lone surrogate must not crash SDK or fixed-error response encoding."""

    async def dispatch(_request: Request, _name: str, _arguments: dict[str, object]) -> dict[str, object]:
        pytest.fail("Malformed ID reached dispatch")

    body = json.dumps({"jsonrpc": "2.0", "id": "\ud800", "method": method})
    async with _client(dispatch) as client:
        response = await client.post("/mcp", content=body, headers={"Content-Type": "application/json"})
        assert response.status_code == 400
        assert response.json() == {"error": "request_rejected"}
