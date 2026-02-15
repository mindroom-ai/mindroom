"""Lightweight proxy that intercepts OpenAI tool_calls and executes them server-side.

Sits between a chat UI (LibreChat, Open WebUI) and the MindRoom backend.
The UI connects to the proxy as if it were an OpenAI-compatible server.
When the agent pauses for tool execution, the proxy handles the round-trip
transparently: it calls ``POST /v1/tools/execute`` on MindRoom, collects
results, and sends a continuation request.

Usage::

    python -m mindroom.proxy --upstream http://localhost:8765 --port 8766

"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

TOOL_EVENT_HEADER = "X-Tool-Event-Format"
SESSION_ID_HEADER = "X-Session-Id"
MAX_TOOL_ROUNDS = 20


@dataclass
class ProxyConfig:
    """Runtime configuration for the proxy."""

    upstream: str
    timeout: float = 120.0


@dataclass
class _CollectedToolCall:
    """A single tool call parsed from the SSE stream."""

    id: str
    name: str
    arguments: str


@dataclass
class _StreamParseResult:
    """Result of parsing an SSE stream for tool calls."""

    content_chunks: list[str] = field(default_factory=list)
    tool_calls: list[_CollectedToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    model: str | None = None


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """Parse a single SSE data line into a dict, or None."""
    line = line.strip()
    if not line.startswith("data: "):
        return None
    payload = line.removeprefix("data: ").strip()
    if payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _merge_delta_tool_call(
    tool_calls_by_index: dict[int, _CollectedToolCall],
    tc: dict[str, Any],
) -> None:
    """Merge a single delta tool_call chunk into the accumulator."""
    idx = tc.get("index", 0)
    if idx not in tool_calls_by_index:
        tool_calls_by_index[idx] = _CollectedToolCall(
            id=tc.get("id", ""),
            name=tc.get("function", {}).get("name", ""),
            arguments=tc.get("function", {}).get("arguments", ""),
        )
    else:
        existing = tool_calls_by_index[idx]
        if tc.get("id"):
            existing.id = tc["id"]
        fn = tc.get("function", {})
        if fn.get("name"):
            existing.name = fn["name"]
        if fn.get("arguments"):
            existing.arguments += fn["arguments"]


def _process_message_tool_calls(
    tool_calls_by_index: dict[int, _CollectedToolCall],
    message: dict[str, Any],
    result: _StreamParseResult,
    choice: dict[str, Any],
) -> None:
    """Extract tool calls from a non-streaming message object."""
    content_val = message.get("content")
    if content_val:
        result.content_chunks.append(content_val)
    for tc in message.get("tool_calls", []):
        tool_calls_by_index[len(tool_calls_by_index)] = _CollectedToolCall(
            id=tc.get("id", ""),
            name=tc.get("function", {}).get("name", ""),
            arguments=tc.get("function", {}).get("arguments", ""),
        )
    if message.get("tool_calls"):
        result.finish_reason = choice.get("finish_reason", "tool_calls")


def _parse_sse_events(raw: str) -> _StreamParseResult:
    """Parse the full SSE text and extract content/tool_calls."""
    result = _StreamParseResult()
    tool_calls_by_index: dict[int, _CollectedToolCall] = {}

    for line in raw.splitlines():
        chunk = _parse_sse_line(line)
        if chunk is None:
            continue

        if result.model is None:
            result.model = chunk.get("model")

        choices = chunk.get("choices", [])
        if not choices:
            continue

        choice = choices[0]
        delta = choice.get("delta", {})
        finish = choice.get("finish_reason")
        if finish is not None:
            result.finish_reason = finish

        content = delta.get("content")
        if content:
            result.content_chunks.append(content)

        for tc in delta.get("tool_calls", []):
            _merge_delta_tool_call(tool_calls_by_index, tc)

        message = choice.get("message")
        if message and isinstance(message, dict):
            _process_message_tool_calls(tool_calls_by_index, message, result, choice)

    result.tool_calls = [tool_calls_by_index[k] for k in sorted(tool_calls_by_index)]
    return result


def _sse_chunk(model: str, content: str | None = None, finish_reason: str | None = None) -> str:
    """Build a single SSE data line for a chat completion chunk."""
    delta: dict[str, str] = {}
    if content is not None:
        delta["content"] = content
    payload = {
        "choices": [{"delta": delta, "index": 0, "finish_reason": finish_reason}],
        "model": model,
        "object": "chat.completion.chunk",
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _execute_tool(
    client: httpx.AsyncClient,
    upstream: str,
    agent: str,
    tool_call: _CollectedToolCall,
    headers: dict[str, str],
) -> dict[str, Any]:
    """Call POST /v1/tools/execute and return the result."""
    try:
        args = json.loads(tool_call.arguments) if tool_call.arguments else {}
    except json.JSONDecodeError:
        args = {}

    payload = {
        "agent": agent,
        "tool_name": tool_call.name,
        "arguments": args,
    }
    resp = await client.post(
        f"{upstream}/v1/tools/execute",
        json=payload,
        headers=headers,
    )
    resp.raise_for_status()
    return resp.json()


async def _stream_upstream(
    client: httpx.AsyncClient,
    upstream: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> str:
    """Send a streaming request to upstream and collect the full SSE response."""
    body_copy = {**body, "stream": True}
    resp = await client.post(
        f"{upstream}/v1/chat/completions",
        json=body_copy,
        headers=headers,
        timeout=120.0,
    )
    resp.raise_for_status()
    return resp.text


def _build_upstream_headers(request: Request) -> dict[str, str]:
    """Build headers to forward to upstream."""
    headers: dict[str, str] = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["Authorization"] = auth
    session_id = request.headers.get(SESSION_ID_HEADER.lower())
    if session_id:
        headers[SESSION_ID_HEADER] = session_id
    headers[TOOL_EVENT_HEADER] = "openai"
    headers["Content-Type"] = "application/json"
    return headers


async def _execute_single_tool(
    client: httpx.AsyncClient,
    upstream: str,
    agent_name: str,
    tc: _CollectedToolCall,
    headers: dict[str, str],
) -> tuple[str, str]:
    """Execute one tool call and return (status_emoji, result_content)."""
    try:
        exec_result = await _execute_tool(client, upstream, agent_name, tc, headers)
        return "✅", str(exec_result.get("result", ""))
    except Exception as exc:
        return "❌", f"Error executing tool: {exc}"


async def _proxy_event_generator(
    body: dict[str, Any],
    messages: list[dict[str, Any]],
    agent_name: str,
    upstream_headers: dict[str, str],
    config: ProxyConfig,
) -> AsyncIterator[str]:
    """Core event generator for the proxy chat endpoint."""
    async with httpx.AsyncClient(timeout=config.timeout) as client:
        for _ in range(MAX_TOOL_ROUNDS):
            request_body = {**body, "messages": messages}
            try:
                raw_sse = await _stream_upstream(client, config.upstream, request_body, upstream_headers)
            except httpx.HTTPStatusError as exc:
                yield f"data: {json.dumps({'error': f'Upstream error: {exc.response.status_code}'})}\n\n"
                yield "data: [DONE]\n\n"
                return

            parsed = _parse_sse_events(raw_sse)
            model = parsed.model or agent_name

            for chunk_text in parsed.content_chunks:
                yield _sse_chunk(model, content=chunk_text)

            if parsed.finish_reason != "tool_calls" or not parsed.tool_calls:
                yield _sse_chunk(model, finish_reason="stop")
                yield "data: [DONE]\n\n"
                return

            # Execute each tool call
            for tc in parsed.tool_calls:
                yield _sse_chunk(model, content=f"\n\n🔧 Running {tc.name}...\n")
                emoji, result_content = await _execute_single_tool(
                    client,
                    config.upstream,
                    agent_name,
                    tc,
                    upstream_headers,
                )
                yield _sse_chunk(model, content=f"{emoji} {tc.name} {'done' if emoji == '✅' else 'failed'}\n\n")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result_content})

        yield _sse_chunk(agent_name, content="\n\n⚠️ Maximum tool execution rounds reached.\n", finish_reason="stop")
        yield "data: [DONE]\n\n"


def create_proxy_app(config: ProxyConfig) -> FastAPI:
    """Create the proxy FastAPI application."""
    app = FastAPI(title="MindRoom Tool Proxy")

    @app.get("/v1/models")
    async def proxy_models(request: Request) -> JSONResponse:
        """Forward model listing to upstream."""
        headers: dict[str, str] = {}
        auth = request.headers.get("authorization")
        if auth:
            headers["Authorization"] = auth
        async with httpx.AsyncClient(timeout=config.timeout) as client:
            resp = await client.get(f"{config.upstream}/v1/models", headers=headers)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)

    @app.post("/v1/chat/completions", response_model=None)
    async def proxy_chat(request: Request) -> StreamingResponse:
        """Proxy chat completions with automatic tool execution."""
        body = json.loads(await request.body())
        upstream_headers = _build_upstream_headers(request)

        if SESSION_ID_HEADER not in upstream_headers:
            from uuid import uuid4  # noqa: PLC0415

            upstream_headers[SESSION_ID_HEADER] = f"proxy-{uuid4().hex[:16]}"

        agent_name = body.get("model", "")
        messages = list(body.get("messages", []))

        return StreamingResponse(
            _proxy_event_generator(body, messages, agent_name, upstream_headers, config),
            media_type="text/event-stream",
        )

    return app


def main() -> None:
    """CLI entry point for the proxy server."""
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="MindRoom tool-calling proxy")
    parser.add_argument(
        "--upstream",
        default="http://localhost:8765",
        help="MindRoom backend URL (default: http://localhost:8765)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8766,
        help="Port to listen on (default: 8766)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",  # noqa: S104
        help="Host to bind to (default: 0.0.0.0)",
    )
    args = parser.parse_args()

    proxy_config = ProxyConfig(upstream=args.upstream.rstrip("/"))
    app = create_proxy_app(proxy_config)

    print(f"Starting MindRoom proxy on {args.host}:{args.port}")
    print(f"Upstream: {proxy_config.upstream}")
    print(f"Point your UI at http://{args.host}:{args.port}/v1")

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    sys.exit(main() or 0)
