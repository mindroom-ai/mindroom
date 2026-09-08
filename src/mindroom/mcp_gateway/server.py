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
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mindroom.logging_config import get_logger
from mindroom.mcp_gateway.execution import ExecutionLease, execution_scope
from mindroom.mcp_gateway.types import GatewayErrorCode, GatewayErrorResponse, GatewayPrincipal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

    from starlette.types import Message, Receive, Scope, Send

_MAX_REQUEST_BYTES = 131072
_MAX_RESPONSE_BYTES = 131072
_MAX_REQUEST_ID_BYTES = 128
_MAX_PROTOCOL_VERSION_BYTES = 64
_RESPONSE_ENVELOPE_BYTES = 256
_PRINCIPAL_SCOPE_KEY = "mcp_gateway_principal"
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


def _error(code: GatewayErrorCode, message: str) -> GatewayErrorResponse:
    return {"error": {"code": code, "message": message}}


def _result(payload: Mapping[str, object]) -> types.CallToolResult:
    structured_content = dict(payload)
    text = json.dumps(structured_content, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    result = types.CallToolResult(
        content=[types.TextContent(type="text", text=text)],
        structuredContent=structured_content,
        isError="error" in structured_content,
    )
    if len(result.model_dump_json().encode()) > _MAX_RESPONSE_BYTES - _RESPONSE_ENVELOPE_BYTES:
        return _result(
            _error(GatewayErrorCode.RESULT_TOO_LARGE, "The tool response exceeds the gateway response limit."),
        )
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
        try:
            json.dumps(request_id, ensure_ascii=False).encode()
        except UnicodeEncodeError as exc:
            raise HTTPException(400, "MCP request ID is not valid UTF-8") from exc
    return payload


def _validate_message(payload: object) -> Response | None:
    """Keep SDK validation diagnostics from reflecting untrusted envelope content."""
    try:
        envelope = types.JSONRPCMessage.model_validate(payload).root
    except (ValidationError, RecursionError, PydanticSerializationError) as exc:
        raise HTTPException(400, "Invalid MCP envelope") from exc
    if isinstance(envelope, types.JSONRPCResponse | types.JSONRPCError):
        # Stateless calls never issue correlated client-result requests.
        # Future bidirectional sessions must route these to their owning session.
        return Response(status_code=202)
    try:
        normalized = envelope.model_dump(by_alias=True, mode="json", exclude_none=True)
        if isinstance(envelope, types.JSONRPCRequest):
            types.ClientRequest.model_validate(normalized)
        else:
            types.ClientNotification.model_validate(normalized)
    except (ValidationError, RecursionError, PydanticSerializationError):
        if isinstance(envelope, types.JSONRPCNotification):
            return Response(status_code=202)
        error = types.JSONRPCError(
            jsonrpc="2.0",
            id=envelope.id,
            error=types.ErrorData(code=-32602, message="Invalid request parameters"),
        )
        return JSONResponse(error.model_dump(by_alias=True, mode="json", exclude_none=True))
    return None


def _validate_protocol_header(request: Request) -> None:
    if len(request.headers.get("mcp-protocol-version", "").encode()) > _MAX_PROTOCOL_VERSION_BYTES:
        raise HTTPException(400, "MCP protocol version exceeds the size limit")


def _validate_early_response_headers(request: Request, payload: object) -> None:
    """Preserve SDK negotiation for replies that the privacy gate now owns."""
    accepted = request.headers.get("accept", "").split(",")
    if not any(value.strip().startswith("application/json") for value in accepted):
        raise HTTPException(406, "MCP requires application/json responses")
    content_types = request.headers.get("content-type", "").split(";")[0].split(",")
    if not any(value.strip() == "application/json" for value in content_types):
        raise HTTPException(415, "MCP requires application/json requests")
    if isinstance(payload, dict) and cast("dict[str, object]", payload).get("method") == "initialize":
        return
    version = request.headers.get("mcp-protocol-version")
    if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise HTTPException(400, "Unsupported MCP protocol version")


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
        authenticate: Callable[[Request], Awaitable[GatewayPrincipal]],
        dispatch: Callable[[Request, str, dict[str, Any]], Awaitable[Mapping[str, object]]],
        public_url: str,
        allowed_origins: tuple[str, ...] | None = None,
        timeout_seconds: float = 60,
        max_active_calls: int = 128,
        max_user_calls: int = 32,
        max_grant_calls: int = 16,
        record_activity: Callable[[Request], Awaitable[None]] | None = None,
    ) -> None:
        if min(max_active_calls, max_user_calls, max_grant_calls) < 1:
            msg = "MCP call limits must be positive"
            raise ValueError(msg)
        self._max_active_calls = max_active_calls
        self._max_user_calls = max_user_calls
        self._max_grant_calls = max_grant_calls
        self._authenticate = authenticate
        self._dispatch = dispatch
        self._record_activity = record_activity
        self._timeout = timeout_seconds
        self._closing = False
        self._active: dict[tuple[GatewayPrincipal, type, int | str], ExecutionLease] = {}
        self._server: Server[None, Request] = Server("MindRoom gateway")
        self._server.list_tools()(self._list_tools)
        self._server.request_handlers[types.CallToolRequest] = self._handle_call_request
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
            try:
                yield
            finally:
                self._closing = True
                leases = tuple(self._active.values())
                for lease in leases:
                    lease.cancel()
                await asyncio.gather(*(lease.wait() for lease in leases))

    def _release(self, key: tuple[GatewayPrincipal, type, int | str], lease: ExecutionLease) -> None:
        if self._active.get(key) is lease:
            del self._active[key]

    async def _list_tools(self) -> list[types.Tool]:
        identity = self._request_identity()
        if self._record_activity is not None and identity is not None:
            await self._record_activity(identity[0])
        return _meta_tools()

    def _request_identity(self) -> tuple[Request, tuple[GatewayPrincipal, type, int | str]] | None:
        context = self._server.request_context
        request = context.request
        if not isinstance(request, Request):
            return None
        principal = request.scope.get(_PRINCIPAL_SCOPE_KEY)
        if not isinstance(principal, GatewayPrincipal) or type(context.request_id) not in {str, int}:
            return None
        return request, (principal, type(context.request_id), context.request_id)

    async def _handle_call_request(self, request: types.CallToolRequest) -> types.ServerResult:
        result = await self._call_tool(request.params.name, request.params.arguments or {})
        identity = self._request_identity()
        if not result.isError and self._record_activity is not None and identity is not None:
            await self._record_activity(identity[0])
        return types.ServerResult(result)

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:  # noqa: PLR0911
        if name not in {"search_tools", "get_tool", "invoke_tool"}:
            return _result(_error(GatewayErrorCode.TOOL_NOT_FOUND, "Unknown gateway operation."))
        schema = next(tool.inputSchema for tool in _meta_tools() if tool.name == name)
        if not Draft202012Validator(schema).is_valid(arguments):
            return _result(_error(GatewayErrorCode.INVALID_ARGUMENTS, "Gateway operation arguments are invalid."))
        identity = self._request_identity()
        if identity is None:
            return _result(_error(GatewayErrorCode.UNAUTHORIZED, "Gateway request identity is unavailable."))
        request, key = identity
        if key in self._active:
            return _result(
                _error(GatewayErrorCode.DUPLICATE_REQUEST, "A call with this request ID is already running."),
            )
        principal = key[0]
        if (
            self._closing
            or len(self._active) >= self._max_active_calls
            or sum(owner.grant_id == principal.grant_id for owner, _, _ in self._active) >= self._max_grant_calls
            or sum(owner.requester_id == principal.requester_id for owner, _, _ in self._active) >= self._max_user_calls
        ):
            return _result(_error(GatewayErrorCode.BUSY, "Gateway call capacity is currently unavailable."))
        task = asyncio.current_task()
        if task is None:
            return _result(_error(GatewayErrorCode.TOOL_UNAVAILABLE, "Gateway execution is unavailable."))
        lease = ExecutionLease(task, lambda completed: self._release(key, completed))
        self._active[key] = lease
        try:
            with execution_scope(lease):
                async with asyncio.timeout(self._timeout):
                    return _result(await self._dispatch(request, name, arguments))
        except TimeoutError:
            return _result(
                _error(
                    GatewayErrorCode.TIMEOUT,
                    "Tool call timed out; its outcome may be unknown. Do not retry automatically.",
                ),
            )
        except asyncio.CancelledError:
            return _result(
                _error(
                    GatewayErrorCode.CANCELLED,
                    "Tool call cancelled; its outcome may be unknown. Do not retry automatically.",
                ),
            )
        except Exception as exc:
            logger.warning("mcp_gateway_call_failed", error_type=type(exc).__name__)
            return _result(
                _error(GatewayErrorCode.TOOL_UNAVAILABLE, "Tool execution failed. Its outcome may be unknown."),
            )

    def _cancel(self, payload: object, principal: GatewayPrincipal) -> Response | None:
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
        lease = self._active.get((principal, type(request_id), cast("int | str", request_id)))
        if lease is not None:
            lease.cancel()
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
            principal = await self._authenticate(request)
            scope[_PRINCIPAL_SCOPE_KEY] = principal
            rejected = await self._security.validate_request(request, is_post=request.method == "POST")
            if rejected is not None:
                await rejected(scope, receive, private_send)
                return
            _validate_protocol_header(request)
            if request.method != "POST":
                await Response(status_code=405, headers={"Allow": "POST"})(scope, receive, private_send)
                return
            body = await read_gateway_body(request)
            payload = _request_payload(body)
            cancelled = self._cancel(payload, principal)
            if cancelled is not None:
                await cancelled(scope, receive, private_send)
                return
            rejected = _validate_message(payload)
            if rejected is not None:
                _validate_early_response_headers(request, payload)
                await rejected(scope, receive, private_send)
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
