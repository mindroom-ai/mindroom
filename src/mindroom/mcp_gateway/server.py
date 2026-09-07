"""Small stateless MCP transport with per-request authority and cancellation."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from fastapi import HTTPException
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from starlette.types import Message, Receive, Scope, Send

_MAX_REQUEST_BYTES = 131072
_MAX_RESPONSE_BYTES = 131072
_MAX_REQUEST_ID_BYTES = 128
_RESPONSE_ENVELOPE_BYTES = 256
_MAX_ACTIVE_CALLS = 128
_GRANT_SCOPE_KEY = "mcp_gateway_grant_id"
_PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
logger = get_logger(__name__)


def _meta_tools() -> list[types.Tool]:
    handle = {"type": "string", "minLength": 1, "maxLength": 128}
    return [
        types.Tool(
            name="search_tools",
            description="Search your personal assistant's integrations. Select a toolkit to search its functions. Results omit schemas; use get_tool for one definition.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "maxLength": 256, "default": ""},
                    "toolkit": handle,
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                },
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        ),
        types.Tool(
            name="get_tool",
            description="Get the input schema for one discovered function. If a connection is required, open its connection_url and connect only that service.",
            inputSchema={
                "type": "object",
                "properties": {"toolkit": handle, "function": handle},
                "required": ["toolkit", "function"],
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        ),
        types.Tool(
            name="invoke_tool",
            description="Invoke one discovered function using your personal connections. Fetch its schema first. A failed or timed-out action must not be retried automatically.",
            inputSchema={
                "type": "object",
                "properties": {"toolkit": handle, "function": handle, "arguments": {"type": "object"}},
                "required": ["toolkit", "function", "arguments"],
                "additionalProperties": False,
            },
            annotations=types.ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=True,
            ),
        ),
    ]


def _error(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}}


def _result(payload: dict[str, object]) -> types.CallToolResult:
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=payload,
        isError="error" in payload,
    )
    if len(result.model_dump_json().encode()) > _MAX_RESPONSE_BYTES - _RESPONSE_ENVELOPE_BYTES:
        return _result(_error("result_too_large", "The tool response exceeds the gateway response limit."))
    return result


def _request_payload(body: bytes) -> object:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(400, "Invalid MCP JSON request") from exc
    if isinstance(payload, dict) and "id" in payload:
        request_id = payload["id"]
        if type(request_id) not in {str, int} or len(json.dumps(request_id).encode()) > _MAX_REQUEST_ID_BYTES:
            raise HTTPException(400, "MCP request ID is invalid or exceeds the size limit")
    return payload


async def read_gateway_body(request: Request) -> bytes:
    """Read bounded client input before handing it to SDK protocol handlers."""
    chunks: list[bytes] = []
    size = 0
    async with asyncio.timeout(10):
        async for chunk in request.stream():
            size += len(chunk)
            if size > _MAX_REQUEST_BYTES:
                raise HTTPException(413, "Gateway request exceeds the size limit")
            chunks.append(chunk)
    return b"".join(chunks)


def replay_gateway_body(body: bytes, receive: Receive) -> Receive:
    """Replay consumed input once, retaining the original disconnect channel."""
    sent = False

    async def replay() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


class GatewayServer:
    """Own one SDK lifespan, static tool surface, and bounded active call set."""

    def __init__(
        self,
        *,
        authenticate: Callable[[Request], Awaitable[str]],
        dispatch: Callable[[Request, str, dict[str, Any]], Awaitable[dict[str, object]]],
        public_url: str,
        allowed_origins: tuple[str, ...] | None = None,
        timeout_seconds: float = 60,
    ) -> None:
        self._authenticate = authenticate
        self._dispatch = dispatch
        self._timeout = timeout_seconds
        self._active: dict[tuple[str, type, int | str], asyncio.Task[Any]] = {}
        self._server: Server[None, Request] = Server("MindRoom gateway")
        self._server.list_tools()(self._list_tools)
        self._server.call_tool(validate_input=False)(self._call_tool)
        origin = urlsplit(public_url)
        security = TransportSecuritySettings(
            allowed_hosts=[origin.netloc],
            allowed_origins=list(allowed_origins)
            if allowed_origins is not None
            else [f"{origin.scheme}://{origin.netloc}"],
        )
        self._security = TransportSecurityMiddleware(security)
        self._manager = StreamableHTTPSessionManager(
            app=self._server,
            stateless=True,
            json_response=True,
            security_settings=security,
        )

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Run exactly once per owning API lifespan."""
        async with self._manager.run():
            yield

    async def _list_tools(self) -> list[types.Tool]:
        return _meta_tools()

    def _request_identity(self) -> tuple[Request, tuple[str, type, int | str]] | None:
        context = self._server.request_context
        request = context.request
        if not isinstance(request, Request):
            return None
        grant = request.scope.get(_GRANT_SCOPE_KEY)
        if not isinstance(grant, str) or type(context.request_id) not in {str, int}:
            return None
        return request, (grant, type(context.request_id), context.request_id)

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:  # noqa: PLR0911
        if name not in {"search_tools", "get_tool", "invoke_tool"}:
            return _result(_error("tool_not_found", "Unknown gateway operation."))
        schema = next(tool.inputSchema for tool in _meta_tools() if tool.name == name)
        if not Draft202012Validator(schema).is_valid(arguments):
            return _result(_error("invalid_arguments", "Gateway operation arguments are invalid."))
        identity = self._request_identity()
        if identity is None:
            return _result(_error("unauthorized", "Gateway request identity is unavailable."))
        request, key = identity
        if key in self._active:
            return _result(_error("duplicate_request", "A call with this request ID is already running."))
        if len(self._active) >= _MAX_ACTIVE_CALLS:
            return _result(_error("busy", "Gateway call capacity is currently full."))
        task = asyncio.current_task()
        if task is None:
            return _result(_error("tool_unavailable", "Gateway execution is unavailable."))
        self._active[key] = task
        try:
            async with asyncio.timeout(self._timeout):
                return _result(await self._dispatch(request, name, arguments))
        except TimeoutError:
            return _result(
                _error("timeout", "Tool call timed out; its outcome may be unknown. Do not retry automatically."),
            )
        except asyncio.CancelledError:
            return _result(
                _error("cancelled", "Tool call cancelled; its outcome may be unknown. Do not retry automatically."),
            )
        except Exception as exc:
            logger.warning("mcp_gateway_call_failed", error_type=type(exc).__name__)
            return _result(_error("tool_unavailable", "Tool execution failed. Its outcome may be unknown."))
        finally:
            self._active.pop(key, None)

    def _cancel(self, payload: object, grant: str) -> Response | None:
        if not isinstance(payload, dict):
            return None
        notification_payload = cast("dict[str, object]", payload)
        if notification_payload.get("method") != "notifications/cancelled":
            return None
        if notification_payload.get("jsonrpc") != "2.0" or "id" in notification_payload:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            notification = types.CancelledNotification.model_validate(notification_payload)
        except ValidationError:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        request_id = notification.params.requestId
        if type(request_id) not in {str, int}:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        task = self._active.get((grant, type(request_id), cast("int | str", request_id)))
        if task is not None:
            task.cancel()
        return Response(status_code=202)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Validate each request before it enters the SDK's stateless transport."""
        request = Request(scope, receive)

        async def private_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"cache-control", b"private, no-store"),
                    (b"referrer-policy", b"no-referrer"),
                ]
            await send(message)

        try:
            grant = await self._authenticate(request)
            scope[_GRANT_SCOPE_KEY] = grant
            rejected = await self._security.validate_request(request, is_post=request.method == "POST")
            if rejected is not None:
                await rejected(scope, receive, private_send)
                return
            if request.method != "POST":
                await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, private_send)
                return
            body = await read_gateway_body(request)
            cancelled = self._cancel(_request_payload(body), grant)
            if cancelled is not None:
                await cancelled(scope, receive, private_send)
                return
        except HTTPException as exc:
            response = JSONResponse(
                {"error": "invalid_token" if exc.status_code == 401 else "request_rejected"},
                status_code=exc.status_code,
                headers={**_PRIVATE_HEADERS, **(exc.headers or {})},
            )
            await response(scope, receive, private_send)
            return
        except TimeoutError:
            await JSONResponse({"error": "request_timeout"}, status_code=408)(scope, receive, private_send)
            return
        await self._manager.handle_request(scope, replay_gateway_body(body, receive), private_send)
