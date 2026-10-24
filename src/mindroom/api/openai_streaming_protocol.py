"""OpenAI wire-format protocol for the /v1 chat completions endpoint.

Owns the protocol-formatting side of the OpenAI-compatible API: SSE chunk
assembly, per-stream completion state, tool-call trace encoding, error-body
shaping, error-string detection, and the finalizer-aware response classes.
Input is core stream chunk events; output is wire format. No agent, team,
or config imports.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from html import escape
from typing import TYPE_CHECKING
from uuid import uuid4

from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from mindroom.llm.events import TextDelta, ToolCompleted, ToolStarted
from mindroom.tool_system.events import format_tool_completed_event, format_tool_started_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from starlette.background import BackgroundTask
    from starlette.types import Receive, Scope, Send

    from mindroom.ai import AIStreamChunk

SSE_DONE = "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Finalizer-aware response classes
# ---------------------------------------------------------------------------


class _FinalizedResponse(Response):
    """A response whose own background runs only after a completed send, and whose ``always_background`` always runs.

    ``completion_predicate``, when set, decides whether the send completed, since a stream can end early either way.
    """

    always_background: BackgroundTask | None = None
    completion_predicate: Callable[[], bool] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the response, then run the always-run finalizer and, after a completed send, the completion one."""
        completion_background = self.background
        self.background = None
        response_error: BaseException | None = None
        try:
            await super().__call__(scope, receive, send)
        except BaseException as error:
            response_error = error
        completed = self.completion_predicate() if self.completion_predicate is not None else response_error is None
        finalizer_error: BaseException | None = None
        if self.always_background is not None:
            try:
                await self.always_background()
            except BaseException as error:
                finalizer_error = error
        background_error: BaseException | None = None
        if completed and completion_background is not None:
            try:
                await completion_background()
            except BaseException as error:
                background_error = error
        if response_error is not None:
            raise response_error
        if background_error is not None:
            raise background_error
        if finalizer_error is not None:
            raise finalizer_error


class OpenAIJSONResponse(_FinalizedResponse, JSONResponse):
    """JSON response with separate completion-scoped and always-run finalizers."""


class OpenAIStreamingResponse(_FinalizedResponse, StreamingResponse):
    """Streaming response with completion-aware compaction and always-run finalizers."""


# ---------------------------------------------------------------------------
# Error bodies
# ---------------------------------------------------------------------------


class _OpenAIError(BaseModel):
    """OpenAI-compatible error detail."""

    message: str
    type: str
    param: str | None = None
    code: str | None = None


class _OpenAIErrorResponse(BaseModel):
    """OpenAI-compatible error wrapper."""

    error: _OpenAIError


def error_response(
    status_code: int,
    message: str,
    error_type: str = "invalid_request_error",
    param: str | None = None,
    code: str | None = None,
) -> JSONResponse:
    """Return an OpenAI-style error response."""
    body = _OpenAIErrorResponse(
        error=_OpenAIError(message=message, type=error_type, param=param, code=code),
    )
    return OpenAIJSONResponse(status_code=status_code, content=body.model_dump())


# ---------------------------------------------------------------------------
# Error-string detection
# ---------------------------------------------------------------------------


# TODO(simplify): a failed run is recognized by sniffing its reply text, so a real reply that starts with an error
# emoji or "Error code:" reads as a failure. Have ai_response and the stream report failure as a typed outcome.
# API clients would notice.
def is_error_response(text: str) -> bool:
    """Detect error strings returned by ai_response() / stream_agent_response().

    Checks for:
    - Emoji-prefixed errors from get_user_friendly_error_message()
    - [agent_name] bracket prefix followed by error emoji
    - Raw provider error strings (e.g. "Error code: 404 - ...")
    - Raw provider JSON error payloads
    """
    error_prefixes = ("❌", "⏱️", "⏰", "⚠️")
    stripped = text.lstrip()
    if not stripped:
        return False

    # Check for [agent_name] prefix followed by error emoji
    if stripped.startswith("["):
        bracket_end = stripped.find("]")
        if bracket_end != -1:
            after_bracket = stripped[bracket_end + 1 :].lstrip()
            return any(after_bracket.startswith(p) for p in error_prefixes)

    if any(stripped.startswith(p) for p in error_prefixes):
        return True

    # Raw provider errors that reached the reply as text, some as serialized JSON-ish payloads
    return bool(_RAW_PROVIDER_ERROR_PREFIX_RE.search(stripped)) or stripped.casefold().startswith(
        _RAW_PROVIDER_JSON_PREFIXES,
    )


_RAW_PROVIDER_ERROR_PREFIX_RE = re.compile(
    r"^(?:[\w.]+(?:error|exception):\s*)?error\s*code:\s*",
    re.IGNORECASE,
)
_RAW_PROVIDER_JSON_PREFIXES = (
    '{"error":',
    "{'error':",
    '{"type":"error"',
    '{"type": "error"',
    "{'type': 'error'",
)


# ---------------------------------------------------------------------------
# Streaming chunk models and state
# ---------------------------------------------------------------------------


class _ChatCompletionChunkChoice(BaseModel):
    """A single choice in a streaming chunk."""

    index: int = 0
    delta: dict
    finish_reason: str | None = None


class _ChatCompletionChunk(BaseModel):
    """A single SSE chunk in a streaming response."""

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[_ChatCompletionChunkChoice]
    system_fingerprint: str | None = None


@dataclass(slots=True)
class ToolStreamState:
    """Track per-stream IDs so tool started/completed updates can be reconciled client-side."""

    next_tool_id: int = 1
    tool_ids_by_call_id: dict[str, str] = field(default_factory=dict)


def new_completion_id() -> str:
    """Allocate a fresh OpenAI-style completion ID."""
    return f"chatcmpl-{uuid4().hex[:12]}"


@dataclass(slots=True)
class CompletionStreamState:
    """Wire-level identity and tool-call state for one streaming completion."""

    completion_id: str
    created: int
    model: str
    tool_state: ToolStreamState = field(default_factory=ToolStreamState)

    @classmethod
    def begin(cls, model: str) -> CompletionStreamState:
        """Start stream state for one completion with a fresh ID and timestamp."""
        return cls(completion_id=new_completion_id(), created=int(time.time()), model=model)


def sse_chunk(
    state: CompletionStreamState,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    """Build one framed SSE line for a streaming chunk."""
    chunk = _ChatCompletionChunk(
        id=state.completion_id,
        created=state.created,
        model=state.model,
        choices=[_ChatCompletionChunkChoice(delta=delta, finish_reason=finish_reason)],
    )
    return f"data: {chunk.model_dump_json()}\n\n"


# ---------------------------------------------------------------------------
# Tool-call trace encoding
# ---------------------------------------------------------------------------


def format_stream_tool_event(event: ToolStarted | ToolCompleted, tool_state: ToolStreamState) -> str:
    """Format one tool event as an inline tool block whose id the call's start and completion share."""
    tool_call_id = str(event.call.tool_call_id).strip()
    if not tool_call_id:
        msg = "Streaming tool events require a non-empty tool_call_id"
        raise ValueError(msg)
    started = isinstance(event, ToolStarted)
    ids = tool_state.tool_ids_by_call_id
    tool_id = ids.get(tool_call_id) if started else ids.pop(tool_call_id, None)
    if tool_id is None:
        # A completion that was never announced still gets a fresh id.
        tool_id = str(tool_state.next_tool_id)
        tool_state.next_tool_id += 1
        if started:
            ids[tool_call_id] = tool_id
    _, trace = format_tool_started_event(event.call) if started else format_tool_completed_event(event.call)
    message = f"{escape(trace.tool_name, quote=True)}({escape(trace.args_preview or '', quote=True)})"
    if not started:
        message += f"\n{escape(trace.result_preview or '', quote=True)}"
    return f'<tool id="{tool_id}" state="{"start" if started else "done"}">{message}</tool>'


def finalize_pending_tools(tool_state: ToolStreamState) -> str | None:
    """Build done tags for tool calls that started but never completed."""
    if not tool_state.tool_ids_by_call_id:
        return None
    parts = [
        f'<tool id="{tool_id}" state="done">(interrupted)</tool>' for tool_id in tool_state.tool_ids_by_call_id.values()
    ]
    tool_state.tool_ids_by_call_id.clear()
    return "".join(parts)


# ---------------------------------------------------------------------------
# Stream-event text extraction
# ---------------------------------------------------------------------------


def extract_stream_text(event: AIStreamChunk, tool_state: ToolStreamState) -> str | None:
    """Return the wire text of one agent stream chunk: reply text, a plain string, or a tool block."""
    if isinstance(event, TextDelta):
        return event.text
    if isinstance(event, str):
        return event
    if isinstance(event, ToolStarted | ToolCompleted):
        return format_stream_tool_event(event, tool_state)
    # ``RunFinished``: its text already streamed as deltas, or the caller sends it as the completed body.
    return None


def extract_agent_stream_failure(event: AIStreamChunk) -> str | None:
    """Return the failure text when the chunk is the error message a failed agent run streams."""
    if isinstance(event, str) and is_error_response(event):
        return event
    return None
