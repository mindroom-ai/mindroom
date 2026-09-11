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
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from agno.run.agent import RunContentEvent, RunErrorEvent, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.team import ToolCallCompletedEvent as TeamToolCallCompletedEvent
from agno.run.team import ToolCallStartedEvent as TeamToolCallStartedEvent
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from mindroom.tool_system.events import format_tool_completed_event, format_tool_started_event

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.models.response import ToolExecution
    from agno.run.agent import RunOutputEvent
    from agno.run.team import TeamRunOutputEvent
    from starlette.background import BackgroundTask
    from starlette.types import Receive, Scope, Send

    from mindroom.ai import AIStreamChunk

SSE_DONE = "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Finalizer-aware response classes
# ---------------------------------------------------------------------------


async def _run_openai_response_backgrounds(
    *,
    completed: bool,
    response_error: BaseException | None,
    completion_background: BackgroundTask | None,
    always_background: BackgroundTask | None,
) -> None:
    """Run completion-scoped and always-run OpenAI response backgrounds."""
    finalizer_error: BaseException | None = None
    if always_background is not None:
        try:
            await always_background()
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


class OpenAIJSONResponse(JSONResponse):
    """JSON response with separate completion-scoped and always-run finalizers."""

    always_background: BackgroundTask | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Send the response, then run completion-scoped and always-run finalizers."""
        completion_background = self.background
        self.background = None
        completed = False
        response_error: BaseException | None = None
        try:
            await super().__call__(scope, receive, send)
        except BaseException as error:
            response_error = error
        else:
            completed = True
        await _run_openai_response_backgrounds(
            completed=completed,
            response_error=response_error,
            completion_background=completion_background,
            always_background=self.always_background,
        )


class OpenAIStreamingResponse(StreamingResponse):
    """Streaming response with completion-aware compaction and always-run finalizers."""

    always_background: BackgroundTask | None = None
    completion_predicate: Callable[[], bool] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Stream the response, then run completion-scoped and always-run finalizers."""
        completion_background = self.background
        self.background = None
        completed = False
        response_error: BaseException | None = None
        try:
            await super().__call__(scope, receive, send)
        except BaseException as error:
            response_error = error
            completed = self.completion_predicate() if self.completion_predicate is not None else False
        else:
            completed = self.completion_predicate() if self.completion_predicate is not None else True
        await _run_openai_response_backgrounds(
            completed=completed,
            response_error=response_error,
            completion_background=completion_background,
            always_background=self.always_background,
        )


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

    # Raw provider errors (agno may surface these as response content)
    return _looks_like_raw_provider_error(stripped)


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


def _looks_like_raw_provider_error(text: str) -> bool:
    """Detect raw provider error payloads surfaced as text."""
    lowered = text.casefold()
    if _RAW_PROVIDER_ERROR_PREFIX_RE.search(text):
        return True
    # Some providers return error payloads directly as serialized JSON-ish text.
    return lowered.startswith(_RAW_PROVIDER_JSON_PREFIXES)


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


def _chunk_json(
    completion_id: str,
    created: int,
    model: str,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    """Build a JSON string for a single SSE chunk."""
    chunk = _ChatCompletionChunk(
        id=completion_id,
        created=created,
        model=model,
        choices=[
            _ChatCompletionChunkChoice(delta=delta, finish_reason=finish_reason),
        ],
    )
    return chunk.model_dump_json()


def sse_chunk(
    state: CompletionStreamState,
    delta: dict,
    finish_reason: str | None = None,
) -> str:
    """Build one framed SSE line for a streaming chunk."""
    return f"data: {_chunk_json(state.completion_id, state.created, state.model, delta=delta, finish_reason=finish_reason)}\n\n"


# ---------------------------------------------------------------------------
# Tool-call trace encoding
# ---------------------------------------------------------------------------


def _extract_tool_call_id(tool: ToolExecution) -> str:
    """Extract the required tool call identifier for streaming tool events."""
    tool_call_id = str(tool.tool_call_id).strip()
    if not tool_call_id:
        msg = "Streaming tool events require a non-empty tool_call_id"
        raise ValueError(msg)
    return tool_call_id


def _allocate_next_tool_id(tool_state: ToolStreamState) -> str:
    tool_id = str(tool_state.next_tool_id)
    tool_state.next_tool_id += 1
    return tool_id


def _resolve_started_tool_id(tool: ToolExecution, tool_state: ToolStreamState) -> str:
    tool_call_id = _extract_tool_call_id(tool)

    existing_tool_id = tool_state.tool_ids_by_call_id.get(tool_call_id)
    if existing_tool_id is not None:
        return existing_tool_id

    tool_id = _allocate_next_tool_id(tool_state)
    tool_state.tool_ids_by_call_id[tool_call_id] = tool_id
    return tool_id


def _resolve_completed_tool_id(tool: ToolExecution, tool_state: ToolStreamState) -> str:
    tool_call_id = _extract_tool_call_id(tool)

    existing_tool_id = tool_state.tool_ids_by_call_id.pop(tool_call_id, None)
    if existing_tool_id is not None:
        return existing_tool_id

    return _allocate_next_tool_id(tool_state)


def _inject_tool_metadata(tool_message: str, *, tool_id: str, state: Literal["start", "done"]) -> str:
    return f'<tool id="{tool_id}" state="{state}">{tool_message}</tool>'


def _escape_tool_payload_text(text: str) -> str:
    return escape(text, quote=True)


def _format_openai_tool_call_display(tool_name: str, args_preview: str | None) -> str:
    safe_tool_name = _escape_tool_payload_text(tool_name)
    if not args_preview:
        return f"{safe_tool_name}()"
    return f"{safe_tool_name}({_escape_tool_payload_text(args_preview)})"


def _format_openai_stream_tool_message(
    tool: ToolExecution,
    *,
    completed: bool,
) -> str:
    if completed:
        _, trace = format_tool_completed_event(tool)
    else:
        _, trace = format_tool_started_event(tool)
    if trace is None:
        return ""

    call_display = _format_openai_tool_call_display(trace.tool_name, trace.args_preview)
    if not completed:
        return call_display

    if trace.result_preview is None:
        return f"{call_display}\n"
    return f"{call_display}\n{_escape_tool_payload_text(trace.result_preview)}"


def format_stream_tool_event(
    event: RunOutputEvent | TeamRunOutputEvent,
    tool_state: ToolStreamState,
) -> str | None:
    """Format tool events as inline text for the SSE stream with stable IDs."""
    if isinstance(event, (ToolCallStartedEvent, TeamToolCallStartedEvent)):
        tool = event.tool
        if tool is None:
            return None
        tool_msg = _format_openai_stream_tool_message(tool, completed=False)
        tool_id = _resolve_started_tool_id(tool, tool_state)
        state: Literal["start", "done"] = "start"
    elif isinstance(event, (ToolCallCompletedEvent, TeamToolCallCompletedEvent)):
        tool = event.tool
        if tool is None:
            return None
        tool_msg = _format_openai_stream_tool_message(tool, completed=True)
        tool_id = _resolve_completed_tool_id(tool, tool_state)
        state = "done"
    else:
        return None

    if not tool_msg:
        return None
    return _inject_tool_metadata(tool_msg, tool_id=tool_id, state=state)


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
    """Extract text content from a stream event."""
    if isinstance(event, RunContentEvent) and event.content:
        return str(event.content)
    if isinstance(event, str):
        return event
    return format_stream_tool_event(event, tool_state)


def extract_agent_stream_failure(event: AIStreamChunk) -> str | None:
    """Return terminal agent-stream failure text when the chunk represents one."""
    if isinstance(event, RunErrorEvent):
        return str(event.content or "Agent execution failed.")
    if isinstance(event, str) and is_error_response(event):
        return event
    return None
