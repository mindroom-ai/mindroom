"""Failure-path parity: how each provider route fails, pinned row by row against what the Agno-backed runtime did.

Each row scripts the wire's answer to every request of one route and stream mode, sends one request through the
route's real adapter inside the production pipeline, and pins how many HTTP requests it makes before it fails
(the SDK's own retries included), the normalized error it ends with, and how the error reply the user reads begins.
The rows were measured against the Agno-backed runtime (Agno 3.0.9 with MindRoom's stream retry hook) with the same
wires; a comment names each case that deliberately sends the request a different number of times.
Error replies follow the typed error: a rate limit reads as one, any other transient failure as temporary
unavailability, and a request that no longer fits the context window says so.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.error_handling import get_user_friendly_error_message
from mindroom.history.summary_call import generate_compaction_summary
from mindroom.llm.errors import (
    MALFORMED_TOOL_CALL,
    AuthenticationFailed,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ReportedUsage, TextDelta, complete
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.retry import STREAM_STALLED
from tests.providers.anthropic_family_support import ROUTE_CONFIGS, claude_message, provider_for
from tests.providers.family_support import scrub_provider_env
from tests.providers.gemini_ollama_support import build_adapter
from tests.providers.golden_support import PacedBytes, ndjson_response, pipeline_provider, sse_response
from tests.providers.openai_family_support import openai_family_adapter, unsigned_jwt

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

type Reply = Callable[[], httpx.Response | Exception]

# The idle limit every row runs with; a stalled reply pauses past it before its first event. The limit also covers
# a request's preparation, such as borrowing a CLI login in a thread, so it outlasts that on a loaded machine.
_IDLE_LIMIT_SECONDS = 0.5
_STALL_SECONDS = 1.0
_PARTIAL = "partial"

RATE_LIMITED = "⏱️ Rate limited."
UNAVAILABLE = "⚠️ Model provider temporarily unavailable after automatic retries."
ERROR = "⚠️ Error: "
AUTHENTICATION = "❌ Authentication failed"
CONTEXT_WINDOW = "⚠️ The conversation no longer fits the model's context window."


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip every retry's backoff; the SDKs still retry as often as they do in production."""
    scrub_provider_env(monkeypatch)
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)
    for sdk in ("openai", "anthropic"):
        monkeypatch.setattr(f"{sdk}._base_client.BaseClient._calculate_retry_timeout", lambda *_args: 0.0)


# --- Routes -------------------------------------------------------------------------------------------------------

_CLAUDE_ID = "claude-sonnet-5-5"
_OPENAI_ROUTES: dict[str, dict[str, Any]] = {
    "openai_chat": {"provider": "openai", "api": "chat_completions", "id": "gpt-6-astra", "api_key": "k"},
    "openrouter": {"provider": "openrouter", "id": "z-ai/glm-5.3", "api_key": "k"},
    "azure": {
        "provider": "azure",
        "id": "gpt-6-astra",
        "api_key": "k",
        "extra_kwargs": {"azure_deployment": "d", "azure_endpoint": "https://parity.openai.azure.com"},
    },
    "deepseek": {"provider": "deepseek", "id": "deepseek-flash", "api_key": "k"},
    "zai": {"provider": "zai", "id": "glm-5.3", "api_key": "k"},
    "llama_cpp": {"provider": "llama_cpp", "id": "unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL"},
    "kimi": {"provider": "kimi", "id": "k3", "extra_kwargs": {"kimi_home": "<kimi_home>"}},
    "groq": {"provider": "groq", "id": "openai/gpt-oss-120b", "api_key": "k"},
    "cerebras": {"provider": "cerebras", "id": "gpt-oss-120b", "api_key": "k"},
    "openai_responses": {"provider": "openai", "api": "responses", "id": "gpt-6-astra", "api_key": "k"},
    "codex": {"provider": "codex", "id": "gpt-6.1-sol", "extra_kwargs": {"codex_home": "<codex_home>"}},
}
_GEMINI_OLLAMA_ROUTES: dict[str, dict[str, Any]] = {
    "gemini": {"provider": "google", "id": "gemini-3.8-flash", "api_key": "k"},
    "gemini_vertexai": {
        "provider": "google",
        "id": "gemini-3.8-flash",
        "extra_kwargs": {"vertexai": True, "project_id": "test-project", "location": "us-central1"},
    },
    "ollama": {"provider": "ollama", "id": "qwen3.8:27b", "host": "http://ollama.test:11434"},
}


def _adapter(route: str, root: Path, respond: Callable[[httpx.Request], httpx.Response]) -> ModelProvider:
    """Build one route's adapter as the registry does, with its HTTP traffic answered by ``respond``."""
    if route in ROUTE_CONFIGS:
        return provider_for(ROUTE_CONFIGS[route], root, httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    if route in _GEMINI_OLLAMA_ROUTES:
        return build_adapter(_GEMINI_OLLAMA_ROUTES[route], root, respond)
    return openai_family_adapter(_OPENAI_ROUTES[route], root, respond)


# --- Wire answers -------------------------------------------------------------------------------------------------


def _json(status: int, body: object) -> Reply:
    if isinstance(body, str):
        return lambda: httpx.Response(status, headers={"content-type": "text/plain"}, content=body.encode())
    return lambda: httpx.Response(status, json=body)


def _claude_start() -> dict[str, Any]:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": _CLAUDE_ID,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 40, "output_tokens": 1},
    }
    return {"type": "message_start", "message": message}


def _claude_block(index: int, block: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index, "content_block": block},
        {"type": "content_block_stop", "index": index},
    ]


def _claude_text(index: int, text: str) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": text}},
    ]


def _claude_error(error_type: str, message: str = "Internal server error") -> dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


def _claude_stream(*events: dict[str, Any], pause: float = 0.0, drop: bool = False) -> Reply:
    return lambda: sse_response([_claude_start(), *events], named=True, pause_seconds=pause, drop=drop)


def _claude_status(status: int, error_type: str, message: str) -> Reply:
    return _json(status, {"type": "error", "error": {"type": error_type, "message": message}})


def _chat_chunk(delta: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


# The first chunk of every OpenAI Chat stream: the role and empty text, which shows the caller nothing.
_CHAT_START = _chat_chunk({"role": "assistant", "content": ""})


def _chat_stream(*events: dict[str, Any] | str, pause: float = 0.0, drop: bool = False) -> Reply:
    return lambda: sse_response([_CHAT_START, *events], pause_seconds=pause, drop=drop)


def _chat_error(**error: object) -> Reply:
    return _chat_stream({"error": error})


def _openai_status(status: int, message: str, error_type: str, code: str | None = None) -> Reply:
    return _json(status, {"error": {"message": message, "type": error_type, "param": None, "code": code}})


_RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "created_at": 1,
    "model": "gpt-6-astra",
    "parallel_tool_calls": True,
    "tool_choice": "auto",
    "tools": [],
}
_RESPONSE_CREATED = {"type": "response.created", "sequence_number": 0, "response": {**_RESPONSE, "output": []}}


def _response_text(text: str) -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "sequence_number": 1,
        "item_id": "msg_1",
        "output_index": 0,
        "content_index": 0,
        "delta": text,
        "logprobs": [],
    }


def _response_error(code: str, message: str = "An error occurred.") -> dict[str, Any]:
    return {"type": "error", "sequence_number": 2, "code": code, "message": message, "param": None}


_RESPONSE_FAILED = {
    "type": "response.failed",
    "sequence_number": 2,
    "response": {**_RESPONSE, "status": "failed", "output": [], "error": {"code": "server_error", "message": "Failed"}},
}


def _item_added(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response.output_item.added", "sequence_number": 1, "output_index": 0, "item": item}


_WEB_SEARCH_STARTED = _item_added({"id": "ws_1", "type": "web_search_call", "status": "in_progress"})
# The hosted tools a Responses route may run, by the output item that shows each starting.
_RESPONSES_HOSTED_TOOLS = {
    "web_search": _WEB_SEARCH_STARTED,
    "file_search": _item_added({"id": "fs_1", "type": "file_search_call", "status": "in_progress", "queries": []}),
    "code_interpreter": _item_added(
        {"id": "ci_1", "type": "code_interpreter_call", "status": "in_progress", "code": "", "container_id": "c"},
    ),
    "image_generation": _item_added({"id": "ig_1", "type": "image_generation_call", "status": "in_progress"}),
}
_TOOL_SEARCH_STARTED = _item_added(
    {"id": "ts_1", "type": "tool_search_call", "status": "in_progress", "execution": "server", "arguments": {}},
)
_COMPACTION_STARTED = _item_added({"id": "cmp_1", "type": "compaction", "encrypted_content": "e"})
_RESPONSE_COMPLETED = {
    "type": "response.completed",
    "sequence_number": 3,
    "response": {
        **_RESPONSE,
        "status": "completed",
        "output": [
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}],
            },
        ],
        "usage": {"input_tokens": 40, "output_tokens": 9, "total_tokens": 49},
    },
}


def _responses_stream(*events: dict[str, Any], pause: float = 0.0, drop: bool = False) -> Reply:
    return lambda: sse_response([_RESPONSE_CREATED, *events], named=True, pause_seconds=pause, drop=drop)


def _gemini_chunk(*parts: dict[str, Any], finish: str | None = None, grounding: bool = False) -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": list(parts)}, "index": 0}
    if finish is not None:
        candidate["finishReason"] = finish
    if grounding:
        candidate["groundingMetadata"] = {"webSearchQueries": ["paris weather"]}
    chunk: dict[str, Any] = {"candidates": [candidate]}
    if finish is not None:
        chunk["usageMetadata"] = {"promptTokenCount": 40, "candidatesTokenCount": 9, "totalTokenCount": 49}
    return chunk


def _gemini_stream(*chunks: dict[str, Any], pause: float = 0.0, drop: bool = False) -> Reply:
    return lambda: sse_response(chunks, pause_seconds=pause, drop=drop)


def _gemini_status(status: int, message: str, reason: str) -> Reply:
    return _json(status, {"error": {"code": status, "message": message, "status": reason}})


def _ollama_chunk(content: str = "", *, done: bool = False) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "model": "qwen3.8:27b",
        "created_at": "2026-10-20T00:00:00Z",
        "done": done,
        "message": {"role": "assistant", "content": content},
    }
    if done:
        chunk.update({"done_reason": "stop", "prompt_eval_count": 40, "eval_count": 9})
    return chunk


def _ollama_stream(*chunks: dict[str, Any], pause: float = 0.0, drop: bool = False) -> Reply:
    return lambda: ndjson_response(chunks, pause_seconds=pause, drop=drop)


class _OpenAfter(httpx.AsyncByteStream):
    """A response body sent at once, after which the connection stays open without sending more."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.sent = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Send the body, mark it sent once the reader asks for more, then wait forever."""
        yield self._body
        self.sent.set()
        await asyncio.get_running_loop().create_future()


def _then_silent(reply: Reply) -> Reply:
    """Send ``reply``'s whole body at once and then keep the connection open without sending more."""

    def silent() -> httpx.Response:
        sent = reply()
        assert isinstance(sent, httpx.Response)
        return httpx.Response(
            200,
            headers={"content-type": sent.headers["content-type"]},
            stream=_OpenAfter(sent.content),
        )

    return silent


def _connect_error() -> Exception:
    return httpx.ConnectError("[Errno 111] Connection refused")


def _read_timeout() -> Exception:
    return httpx.ReadTimeout("The read operation timed out")


# --- Rows ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One route, stream mode, and failure, and how the owned pipeline ends it.

    ``requests`` counts HTTP requests, the SDK's retries included; ``code`` pins the error's code where later
    handling reads it.
    """

    route: str
    stream: bool
    failure: str
    reply: Reply
    requests: int
    error: type[ProviderError]
    message: str
    code: str | None = None

    @property
    def id(self) -> str:
        """The pytest id."""
        return f"{self.route}-{'streamed' if self.stream else 'blocking'}-{self.failure}"


@dataclass(frozen=True)
class _Case:
    """One failure of a family, with its request counts per stream mode; None leaves that mode out."""

    failure: str
    reply: Reply
    error: type[ProviderError]
    message: str
    streamed: int | None
    blocking: int | None = None
    code: str | None = None


def _rows(route: str, cases: list[_Case], *, streams_only: bool = False) -> list[Row]:
    """Return the rows of ``cases`` on ``route``; a route that ``streams_only`` fails a blocking request as a stream."""
    rows = []
    for case in cases:
        counts = {True: case.streamed, False: case.streamed if streams_only else case.blocking}
        rows.extend(
            Row(route, stream, case.failure, case.reply, requests, case.error, case.message, case.code)
            for stream, requests in counts.items()
            if requests is not None
        )
    return rows


# The SDK sends a request three times, its two retries included, before an HTTP 408, 409, 429, or 5xx status or a
# connection failure reaches the pipeline, which reissues a streamed request four more times.
_SDK = 3
_SDK_AND_PIPELINE = 15
_PIPELINE = 5
_STALLED = 2


# An early end, a stream that ends before the provider finished the turn, is retried like a dropped connection on
# every route until the attempt committed. Only on Responses did the Agno-backed runtime do the same; on Claude,
# Gemini, and Ollama it accepted what had arrived as the finished reply after one request, an empty one when nothing
# had. A Chat stream has no early end: its end finishes the turn, since OpenAI-compatible servers may omit the finish
# reason, as on the Agno-backed runtime.
def _early_ends(before_output: Reply, after_output: Reply) -> list[_Case]:
    """Return the early end before any output and the early end after visible output of one route's wire."""
    return [
        _Case("end_before_output", before_output, ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        _Case("end_after_visible_output", after_output, ProviderUnavailable, UNAVAILABLE, 1),
    ]


_CLAUDE_WEB_SEARCH = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}
_CLAUDE_CASES = [
    _Case(
        "http_429",
        _claude_status(429, "rate_limit_error", "Slow down"),
        RateLimited,
        RATE_LIMITED,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case(
        "http_529",
        _claude_status(529, "overloaded_error", "Overloaded"),
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case(
        "http_500",
        _claude_status(500, "api_error", "Internal server error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case(
        "http_503_proxy_text",
        _json(503, "upstream connect error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case(
        "http_408",
        _claude_status(408, "timeout_error", "Request timed out"),
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case("http_400", _claude_status(400, "invalid_request_error", "messages: bad"), InvalidRequest, ERROR, 1, 1),
    _Case(
        "http_422",
        _claude_status(422, "invalid_request_error", "messages.0.content: Input should be a valid list"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case(
        "http_400_context_window",
        _claude_status(400, "invalid_request_error", "prompt is too long: 210000 tokens > 200000 maximum"),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
        1,
    ),
    # Older Claude models word an input that leaves no room for the output this way.
    _Case(
        "http_400_context_limit",
        _claude_status(
            400,
            "invalid_request_error",
            "input length and `max_tokens` exceed context limit: 197000 + 8192 > 200000, decrease input length or "
            "`max_tokens` and try again",
        ),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
        1,
    ),
    _Case(
        "http_413",
        _claude_status(413, "request_too_large", "Request exceeds the size"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case(
        "http_401",
        _claude_status(401, "authentication_error", "invalid x-api-key"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
        1,
    ),
    _Case(
        "http_403",
        _claude_status(403, "permission_error", "Your API key does not have permission to use the specified resource."),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
        1,
    ),
    _Case("http_404", _claude_status(404, "not_found_error", "model: x"), InvalidRequest, ERROR, 1, 1),
    _Case(
        "connect_error",
        _connect_error,
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
        code="connection_error",
    ),
    _Case("read_timeout", _read_timeout, ProviderUnavailable, UNAVAILABLE, _SDK_AND_PIPELINE, _SDK, code="timeout"),
    _Case(
        "event_overloaded_error",
        _claude_stream(_claude_error("overloaded_error")),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "event_rate_limit_error",
        _claude_stream(_claude_error("rate_limit_error")),
        RateLimited,
        RATE_LIMITED,
        _PIPELINE,
    ),
    _Case("event_api_error", _claude_stream(_claude_error("api_error")), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
    _Case(
        "event_api_error_other_wording",
        _claude_stream(_claude_error("api_error", "Unexpected upstream failure")),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "event_timeout_error",
        _claude_stream(_claude_error("timeout_error", "Request timed out")),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    # A deterministic error event is final; the Agno-backed runtime retried every event after the 200 status.
    _Case(
        "event_invalid_request_error",
        _claude_stream(_claude_error("invalid_request_error", "Bad block")),
        ProviderError,
        ERROR,
        1,
    ),
    _Case("reset_before_output", _claude_stream(drop=True), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
    _Case(
        "stall",
        _claude_stream(*_claude_text(0, "ok"), pause=_STALL_SECONDS),
        ProviderUnavailable,
        UNAVAILABLE,
        _STALLED,
        code=STREAM_STALLED,
    ),
    _Case(
        "event_after_visible_output",
        _claude_stream(*_claude_text(0, _PARTIAL), _claude_error("api_error")),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
    ),
    _Case(
        "reset_after_visible_output",
        _claude_stream(*_claude_text(0, _PARTIAL), drop=True),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
    ),
    # The stream ends before message_stop.
    *_early_ends(_claude_stream(), _claude_stream(*_claude_text(0, _PARTIAL))),
    # Rerunning a tool search or a compaction repeats no work, so their blocks keep the retry budget.
    _Case(
        "event_after_tool_search",
        _claude_stream(
            *_claude_block(
                0,
                {"type": "server_tool_use", "id": "srvtoolu_1", "name": "tool_search_tool_regex", "input": {}},
            ),
            _claude_error("api_error"),
        ),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "event_after_compaction",
        _claude_stream(*_claude_block(0, {"type": "compaction", "content": "Summary."}), _claude_error("api_error")),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    # A retry would run the web search again, so the failure is final; the Agno-backed runtime retried it.
    _Case(
        "event_after_web_search",
        _claude_stream(*_claude_block(0, _CLAUDE_WEB_SEARCH), _claude_error("api_error")),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
    ),
    # A stall after a server tool that acts or costs started is final too; the Agno-backed runtime retried it once.
    _Case(
        "stall_after_web_search",
        _then_silent(_claude_stream(*_claude_block(0, _CLAUDE_WEB_SEARCH))),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
        code=STREAM_STALLED,
    ),
    _Case(
        "stall_after_code_execution",
        _then_silent(_claude_stream(*_claude_block(0, {**_CLAUDE_WEB_SEARCH, "name": "code_execution"}))),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
        code=STREAM_STALLED,
    ),
    _Case(
        "stall_after_tool_search",
        _then_silent(_claude_stream(*_claude_block(0, {**_CLAUDE_WEB_SEARCH, "name": "tool_search_tool_regex"}))),
        ProviderUnavailable,
        UNAVAILABLE,
        _STALLED,
        code=STREAM_STALLED,
    ),
    _Case(
        "stall_after_compaction",
        _then_silent(_claude_stream(*_claude_block(0, {"type": "compaction", "content": "Summary."}))),
        ProviderUnavailable,
        UNAVAILABLE,
        _STALLED,
        code=STREAM_STALLED,
    ),
]

_OPENAI_HTTP_CASES = [
    _Case(
        "http_429",
        _openai_status(
            429,
            "Rate limit reached for gpt-6-astra on tokens per min (TPM).",
            "requests",
            "rate_limit_exceeded",
        ),
        RateLimited,
        RATE_LIMITED,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case(
        "http_500",
        _openai_status(500, "The server had an error while processing your request.", "server_error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case(
        "http_503",
        _openai_status(503, "The server is overloaded or not ready yet.", "server_error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case(
        "http_409",
        _openai_status(409, "The request conflicted with another request.", "conflict"),
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case(
        "http_529",
        _openai_status(529, "The service is overloaded.", "server_error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
    ),
    _Case(
        "http_400",
        _openai_status(400, "Invalid value for 'messages'.", "invalid_request_error"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case(
        "http_400_context_window",
        _openai_status(
            400,
            "This model's maximum context length is 128000 tokens. However, your messages resulted in 130000 tokens.",
            "invalid_request_error",
            "context_length_exceeded",
        ),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
        1,
    ),
    # Text that names an ``author`` property is not wording about the credentials.
    _Case(
        "http_400_author_property",
        _openai_status(400, "Invalid schema for function 'save_note': 'author' is not of type 'string'.", "invalid"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    # A word of the auth family names the credentials beside an underscore and in British spelling, as the
    # Agno-backed runtime's reply read them.
    _Case(
        "http_403_invalid_auth",
        _openai_status(403, "invalid_auth: the access token was revoked", "invalid_request_error"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
        1,
    ),
    _Case(
        "http_403_unauthorised",
        _openai_status(403, "Unauthorised to use this model", "invalid_request_error"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
        1,
    ),
    _Case(
        "http_413",
        _openai_status(413, "Request Entity Too Large", "invalid_request_error"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case(
        "http_422",
        _openai_status(422, "Unprocessable Entity: the request body failed validation.", "invalid_request_error"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case(
        "http_401",
        _openai_status(401, "Incorrect API key provided.", "invalid_request_error", "invalid_api_key"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
        1,
    ),
    _Case(
        "http_403",
        _openai_status(403, "Project proj_1 does not have access to model gpt-6-astra", "invalid_request_error"),
        ProviderError,
        ERROR,
        1,
        1,
    ),
    _Case(
        "http_404",
        _openai_status(404, "The model does not exist.", "invalid_request_error"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case(
        "connect_error",
        _connect_error,
        ProviderUnavailable,
        UNAVAILABLE,
        _SDK_AND_PIPELINE,
        _SDK,
        code="connection_error",
    ),
    _Case("read_timeout", _read_timeout, ProviderUnavailable, UNAVAILABLE, _SDK_AND_PIPELINE, _SDK, code="timeout"),
]
# Until visible text or a complete tool call arrives, a Chat stream keeps its retry budget; the Agno-backed runtime
# stopped retrying at the opening empty-text chunk, so its streamed failures were final after one request.
_TOOL_ARGUMENTS = _chat_chunk(
    {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": '{"ci'}}]},
)
_CHAT_CASES = [
    *_OPENAI_HTTP_CASES,
    _Case(
        "event_server_error",
        _chat_error(message="The server had an error.", type="server_error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "event_status_429",
        _chat_error(message="Rate limit exceeded: free-models-per-min", code=429),
        RateLimited,
        RATE_LIMITED,
        _PIPELINE,
    ),
    _Case(
        "event_status_529",
        _chat_error(message="Slow down", code=529),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "event_status_503",
        _chat_error(message="Service Unavailable", code=503),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    # An api_error or rate_limit_error event is transient whatever its wording; the Agno-backed runtime treated
    # these as final on OpenAI-compatible routes.
    _Case(
        "event_api_error_other_wording",
        _chat_error(message="Unexpected upstream failure", type="api_error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "event_rate_limit_error_type",
        _chat_error(message="Slow down", type="rate_limit_error"),
        RateLimited,
        RATE_LIMITED,
        _PIPELINE,
    ),
    # Wording that names the context window outranks the transient type, since a retry cannot make the request fit.
    _Case(
        "event_api_error_context_window",
        _chat_error(message="This model's maximum context length is 128000 tokens.", type="api_error"),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
    ),
    _Case(
        "event_context_window",
        _chat_error(message="context length exceeded", code="context_length_exceeded"),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
    ),
    _Case(
        "event_invalid_request_error",
        _chat_error(message="Invalid request", type="invalid_request_error"),
        ProviderError,
        ERROR,
        1,
    ),
    _Case("reset_before_output", _chat_stream(drop=True), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
    # Gateways such as OpenRouter answer a failed upstream with HTTP 200 and an error body; the Agno-backed runtime
    # raised it as a 502.
    _Case(
        "http_200_error_body",
        _json(200, {"error": {"message": "Provider returned error", "code": 502}, "user_id": "u"}),
        ProviderUnavailable,
        UNAVAILABLE,
        None,
        1,
    ),
    _Case(
        "reset_during_tool_arguments",
        _chat_stream(_TOOL_ARGUMENTS, drop=True),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "stall",
        _chat_stream(_chat_chunk({"content": "ok"}), "[DONE]", pause=_STALL_SECONDS),
        ProviderUnavailable,
        UNAVAILABLE,
        _STALLED,
        code=STREAM_STALLED,
    ),
    _Case(
        "event_after_visible_output",
        _chat_stream(
            _chat_chunk({"content": _PARTIAL}),
            {"error": {"message": "The server had an error.", "type": "server_error"}},
        ),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
    ),
    _Case(
        "reset_after_visible_output",
        _chat_stream(_chat_chunk({"content": _PARTIAL}), drop=True),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
    ),
]
_CHAT_SHARED = [
    case
    for case in _CHAT_CASES
    if case.failure in {"http_503", "http_400", "event_server_error", "http_200_error_body"}
]

_RESPONSES_CASES = [
    *_OPENAI_HTTP_CASES,
    _Case(
        "event_server_error",
        _responses_stream(_response_error("server_error")),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "event_rate_limit",
        _responses_stream(_response_error("rate_limit_exceeded")),
        RateLimited,
        RATE_LIMITED,
        _PIPELINE,
    ),
    _Case(
        "event_context_window",
        _responses_stream(_response_error("context_length_exceeded", "Your input exceeds the context window.")),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
    ),
    _Case("event_invalid_prompt", _responses_stream(_response_error("invalid_prompt")), ProviderError, ERROR, 1),
    _Case("response_failed", _responses_stream(_RESPONSE_FAILED), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
    _Case("reset_before_output", _responses_stream(drop=True), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
    _Case(
        "stall",
        _responses_stream(_response_text("ok"), _RESPONSE_COMPLETED, pause=_STALL_SECONDS),
        ProviderUnavailable,
        UNAVAILABLE,
        _STALLED,
        code=STREAM_STALLED,
    ),
    _Case(
        "event_after_visible_output",
        _responses_stream(_response_text(_PARTIAL), _response_error("server_error")),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
        code="server_error",
    ),
    _Case(
        "reset_after_visible_output",
        _responses_stream(_response_text(_PARTIAL), drop=True),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
        code="connection_error",
    ),
    # The stream ends before response.completed.
    *_early_ends(_responses_stream(), _responses_stream(_response_text(_PARTIAL))),
    # A retry would run the hosted tool again, so a failure after it started is final, a stall included; the
    # Agno-backed runtime retried a stall once.
    *(
        _Case(
            f"{failure}_after_{tool}",
            reply,
            ProviderUnavailable,
            UNAVAILABLE,
            1,
            code=code,
        )
        for tool, started in _RESPONSES_HOSTED_TOOLS.items()
        for failure, reply, code in (
            ("event", _responses_stream(started, _response_error("server_error")), "server_error"),
            ("stall", _then_silent(_responses_stream(started)), STREAM_STALLED),
        )
    ),
    # Rerunning a tool search or a compaction repeats no work, so their items keep the retry budget; the Agno-backed
    # runtime ended the request at once when an error event followed one.
    *(
        _Case(f"{failure}_after_{name}", reply, ProviderUnavailable, UNAVAILABLE, requests, code=code)
        for name, started in (("tool_search", _TOOL_SEARCH_STARTED), ("compaction", _COMPACTION_STARTED))
        for failure, reply, requests, code in (
            ("event", _responses_stream(started, _response_error("server_error")), _PIPELINE, "server_error"),
            ("stall", _then_silent(_responses_stream(started)), _STALLED, STREAM_STALLED),
        )
    ),
]

_GEMINI_CASES = [
    _Case(
        "http_429",
        _gemini_status(429, "Resource has been exhausted (e.g. check quota).", "RESOURCE_EXHAUSTED"),
        RateLimited,
        RATE_LIMITED,
        _PIPELINE,
        1,
    ),
    _Case(
        "http_500",
        _gemini_status(500, "An internal error has occurred.", "INTERNAL"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        1,
    ),
    _Case(
        "http_503",
        _gemini_status(503, "The model is overloaded. Please try again later.", "UNAVAILABLE"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        1,
    ),
    _Case(
        "http_504",
        _gemini_status(504, "Deadline exceeded.", "DEADLINE_EXCEEDED"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        1,
    ),
    _Case(
        "http_529",
        _gemini_status(529, "The model is overloaded.", "UNAVAILABLE"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        1,
    ),
    _Case(
        "http_400",
        _gemini_status(400, "Invalid JSON payload received.", "INVALID_ARGUMENT"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case(
        "http_400_context_window",
        _gemini_status(
            400,
            "The input token count (1100000) exceeds the maximum number of tokens allowed (1048576).",
            "INVALID_ARGUMENT",
        ),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
        1,
    ),
    _Case(
        "http_400_api_key",
        _gemini_status(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
        1,
    ),
    _Case(
        "http_403",
        _gemini_status(403, "Permission denied on resource project p.", "PERMISSION_DENIED"),
        ProviderError,
        ERROR,
        1,
        1,
    ),
    _Case("http_404", _gemini_status(404, "models/gemini-x is not found.", "NOT_FOUND"), InvalidRequest, ERROR, 1, 1),
    # Google's own wording: "generateContent" holds "rate", which no longer reads as a rate limit.
    _Case(
        "http_404_unsupported_method",
        _gemini_status(
            404,
            "models/gemini-x is not found for API version v1beta, or is not supported for generateContent. "
            "Call ListModels to see the list of available models and their supported methods.",
            "NOT_FOUND",
        ),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case(
        "http_422",
        _gemini_status(422, "Unprocessable entity.", "INVALID_ARGUMENT"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case("connect_error", _connect_error, ProviderUnavailable, UNAVAILABLE, _PIPELINE, 1, code="connection_error"),
    _Case("read_timeout", _read_timeout, ProviderUnavailable, UNAVAILABLE, _PIPELINE, 1, code="timeout"),
    _Case(
        "event_status_503",
        _gemini_stream({"error": {"code": 503, "message": "The model is overloaded.", "status": "UNAVAILABLE"}}),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "reset_before_output",
        _gemini_stream(_gemini_chunk({"text": ""}), drop=True),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "stall",
        _gemini_stream(_gemini_chunk({"text": "ok"}, finish="STOP"), pause=_STALL_SECONDS),
        ProviderUnavailable,
        UNAVAILABLE,
        _STALLED,
        code=STREAM_STALLED,
    ),
    _Case(
        "reset_after_visible_output",
        _gemini_stream(_gemini_chunk({"text": _PARTIAL}), drop=True),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
    ),
    # The stream ends before a candidate carries its finish reason.
    *_early_ends(_gemini_stream(_gemini_chunk({"text": ""})), _gemini_stream(_gemini_chunk({"text": _PARTIAL}))),
    # A retry would run Google Search grounding or code execution again, so a failure after either is final, a
    # stall included; the Agno-backed runtime retried a stall.
    *(
        _Case(f"{failure}_after_{tool}", reply, ProviderUnavailable, UNAVAILABLE, 1, code=code)
        for tool, chunk in (
            ("grounding", _gemini_chunk(grounding=True)),
            ("code_execution", _gemini_chunk({"executableCode": {"language": "PYTHON", "code": "print(1)"}})),
        )
        for failure, reply, code in (
            ("reset", _gemini_stream(chunk, drop=True), "connection_error"),
            ("stall", _then_silent(_gemini_stream(chunk)), STREAM_STALLED),
        )
    ),
    # One guidance retry follows a malformed tool call in both modes.
    _Case(
        "malformed_tool_call",
        _gemini_stream(_gemini_chunk(finish="MALFORMED_FUNCTION_CALL")),
        ProviderError,
        ERROR,
        2,
        code=MALFORMED_TOOL_CALL,
    ),
    _Case(
        "malformed_tool_call",
        _json(200, _gemini_chunk(finish="MALFORMED_FUNCTION_CALL")),
        ProviderError,
        ERROR,
        None,
        2,
        code=MALFORMED_TOOL_CALL,
    ),
]

# The Agno-backed runtime never retried an Ollama request: Agno's Ollama adapter let the SDK's errors escape
# unclassified. Ollama now retries transient failures as every other route does.
_OLLAMA_CASES = [
    _Case(
        "http_500",
        _json(500, {"error": "llama runner process has terminated"}),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        1,
    ),
    _Case(
        "http_503",
        _json(503, {"error": "server busy, please try again"}),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        1,
    ),
    _Case("http_400", _json(400, {"error": "invalid options"}), InvalidRequest, ERROR, 1, 1),
    _Case("http_422", _json(422, {"error": "unprocessable request"}), InvalidRequest, ERROR, 1, 1),
    _Case(
        "http_400_context_window",
        _json(400, {"error": "input length exceeds the context length"}),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
        1,
    ),
    _Case("http_404", _json(404, {"error": "model 'qwen3.8:27b' not found"}), InvalidRequest, ERROR, 1, 1),
    _Case(
        "connect_error",
        _connect_error,
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        1,
        code="connection_error",
    ),
    _Case(
        "read_timeout",
        _read_timeout,
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        1,
        code="timeout",
    ),
    _Case(
        "event_error",
        _ollama_stream(_ollama_chunk(), {"error": "an error was encountered while running the model"}),
        ProviderError,
        ERROR,
        1,
    ),
    _Case(
        "reset_before_output",
        _ollama_stream(_ollama_chunk(), drop=True),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "stall",
        _ollama_stream(_ollama_chunk("ok"), _ollama_chunk(done=True), pause=_STALL_SECONDS),
        ProviderUnavailable,
        UNAVAILABLE,
        _STALLED,
        code=STREAM_STALLED,
    ),
    _Case(
        "reset_after_visible_output",
        _ollama_stream(_ollama_chunk(_PARTIAL), drop=True),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
    ),
    # The stream ends before its done chunk.
    *_early_ends(_ollama_stream(_ollama_chunk()), _ollama_stream(_ollama_chunk(_PARTIAL))),
]

# llama.cpp's own wording for a request larger than the context size the server was started with.
_LLAMA_CPP_CASES = [
    _Case(
        "http_400_context_size",
        _json(
            400,
            {
                "error": {
                    "code": 400,
                    "message": "the request exceeds the available context size, try increasing it",
                    "type": "exceed_context_size_error",
                    "n_prompt_tokens": 9000,
                    "n_ctx": 8192,
                },
            },
        ),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
        1,
    ),
]

# Vertex AI Claude, Bedrock Claude, and Vertex AI Gemini failed exactly as their family's direct route did on the
# Agno-backed runtime, so they run every row of that family; the other OpenAI-compatible Chat routes run the shared
# Chat rows, and llama.cpp its own context-size wording too.
ROWS = [
    *_rows("anthropic", _CLAUDE_CASES),
    *_rows("vertexai_claude", _CLAUDE_CASES),
    *_rows("bedrock_claude", _CLAUDE_CASES),
    *_rows("openai_chat", _CHAT_CASES),
    *(
        row
        for route in ("openrouter", "azure", "deepseek", "zai", "llama_cpp", "kimi", "groq", "cerebras")
        for row in _rows(route, _CHAT_SHARED)
    ),
    *_rows("llama_cpp", _LLAMA_CPP_CASES),
    *_rows("openai_responses", _RESPONSES_CASES),
    *_rows("codex", _RESPONSES_CASES, streams_only=True),
    *_rows("gemini", _GEMINI_CASES),
    *_rows("gemini_vertexai", _GEMINI_CASES),
    *_rows("ollama", _OLLAMA_CASES),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", ROWS, ids=[row.id for row in ROWS])
async def test_a_failed_request_is_retried_and_reported_as_the_parity_table_says(row: Row, tmp_path: Path) -> None:
    """The route retries the failure as often as the row says, ends with its error, and reports it as the row says."""
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = row.reply()
        if isinstance(answer, Exception):
            raise answer
        return answer

    provider = pipeline_provider(_adapter(row.route, tmp_path, respond), idle_timeout_seconds=_IDLE_LIMIT_SECONDS)
    request = ModelRequest(messages=[Message(role="user", content="Hi")], stream=row.stream)

    with pytest.raises(ProviderError) as raised:
        await complete(provider, request)

    assert (type(raised.value), len(sent)) == (row.error, row.requests)
    assert get_user_friendly_error_message(raised.value).startswith(row.message)
    if row.code is not None:
        assert raised.value.code == row.code


# --- A response the provider ended incomplete --------------------------------------------------------------------


# A call the output limit cut off mid-arguments, as the response lists it and as the turn keeps it.
_CUT_OFF_CALL_ITEM = {
    "id": "fc_1",
    "type": "function_call",
    "call_id": "call_1",
    "name": "get_weather",
    "arguments": '{"city": "Par',
    "status": "incomplete",
}
_CUT_OFF_CALL = {
    "id": "fc_1",
    "call_id": "call_1",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city": "Par'},
}


def _incomplete_response(reason: str, *, cut_off_call: bool) -> dict[str, Any]:
    output = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "incomplete",
            "content": [{"type": "output_text", "text": _PARTIAL, "annotations": []}],
        },
        *([_CUT_OFF_CALL_ITEM] if cut_off_call else []),
    ]
    usage = {"input_tokens": 40, "output_tokens": 9, "total_tokens": 49}
    return {
        **_RESPONSE,
        "status": "incomplete",
        "incomplete_details": {"reason": reason},
        "output": output,
        "usage": usage,
    }


def _incomplete(reason: str, *, stream: bool, cut_off_call: bool) -> Reply:
    response = _incomplete_response(reason, cut_off_call=cut_off_call)
    if not stream:
        return _json(200, response)
    call_events = [
        {
            "type": "response.output_item.added",
            "sequence_number": 2,
            "output_index": 1,
            "item": {**_CUT_OFF_CALL_ITEM, "arguments": "", "status": "in_progress"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "sequence_number": 3,
            "item_id": "fc_1",
            "output_index": 1,
            "delta": _CUT_OFF_CALL_ITEM["arguments"],
        },
    ]
    ended = {"type": "response.incomplete", "sequence_number": 4, "response": response}
    return _responses_stream(_response_text(_PARTIAL), *(call_events if cut_off_call else []), ended)


# A response that ended incomplete ends the turn with its stop reason after one request, streamed or blocking, as a
# blocking one did on the Agno-backed runtime; that runtime failed a streamed one as an incomplete stream instead.
# The turn keeps a call the response ended in the middle of, as a blocking response and a Chat reply did there.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "stop_reason", "cut_off_call"),
    [
        ("max_output_tokens", "output_limit", False),
        ("content_filter", "content_filter", False),
        ("max_output_tokens", "output_limit", True),
    ],
    ids=["output_limit", "content_filter", "output_limit-cut_off_call"],
)
@pytest.mark.parametrize(
    ("route", "stream"),
    [("openai_responses", True), ("openai_responses", False), ("codex", True)],
)
async def test_a_response_that_ended_incomplete_ends_the_turn_with_its_stop_reason_in_either_mode(
    route: str,
    stream: bool,
    reason: str,
    stop_reason: str,
    cut_off_call: bool,
    tmp_path: Path,
) -> None:
    """The output limit and the content filter end a streamed and a blocking reply the same way, without a retry."""
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = _incomplete(reason, stream=stream, cut_off_call=cut_off_call)()
        assert isinstance(answer, httpx.Response)
        return answer

    provider = pipeline_provider(_adapter(route, tmp_path, respond), idle_timeout_seconds=_IDLE_LIMIT_SECONDS)

    turn = await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")], stream=stream))

    assert (turn.content, turn.stop_reason, turn.usage.tokens.output_tokens, len(sent)) == (_PARTIAL, stop_reason, 9, 1)
    assert turn.tool_calls == ([_CUT_OFF_CALL] if cut_off_call else [])


# Each route's answer to a request its server failed, which the SDK and the stream retry would both reissue.
_SERVER_FAILURES = {row.route: row.reply for row in ROWS if row.failure in {"http_500", "http_503"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_SERVER_FAILURES))
async def test_a_compaction_summary_attempt_sends_one_request_on_every_route(route: str, tmp_path: Path) -> None:
    """Compaction retries a failed summary itself, so neither the SDK nor the stream retry reissues an attempt.

    Codex streams every request, and its summaries once took the stream's four retries on top of compaction's.
    """
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = _SERVER_FAILURES[route]()
        assert isinstance(answer, httpx.Response)
        return answer

    provider = pipeline_provider(_adapter(route, tmp_path, respond), idle_timeout_seconds=_IDLE_LIMIT_SECONDS)

    with pytest.raises(ProviderUnavailable):
        await generate_compaction_summary(
            model=provider,
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
        )

    assert len(sent) == 1


# --- CLI login refresh --------------------------------------------------------------------------------------------


def _expire_cli_login(route: str, root: Path) -> None:
    """Replace the route's saved CLI login with one whose access token expired, so each request refreshes it."""
    if route == "codex":
        tokens = {"access_token": unsigned_jwt({"exp": 1000}), "refresh_token": "r", "account_id": "parity-account"}
        auth = {"auth_mode": "chatgpt", "tokens": tokens}
        (root / "codex" / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    else:
        credentials = {"access_token": "expired", "refresh_token": "r", "expires_at": 1000}
        (root / "kimi" / "credentials" / "kimi-code.json").write_text(json.dumps(credentials), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "stream", "refresh_reply", "error", "message", "refreshes"),
    [
        # A refresh that fails on the network is the provider's transient failure, retried as the Agno-backed
        # runtime retried it; Kimi's blocking requests get no stream retries, and Codex always streams.
        ("codex", True, _connect_error(), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        ("codex", False, _connect_error(), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        ("kimi", True, _connect_error(), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        ("kimi", False, _connect_error(), ProviderUnavailable, UNAVAILABLE, 1),
        # A login server that is down or throttling answers with its status, which the shared classifier reads as for
        # any provider response, as the Agno-backed runtime reported and retried it.
        ("codex", True, httpx.Response(503, text="upstream down"), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        ("codex", False, httpx.Response(502, text="bad gateway"), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        ("kimi", True, httpx.Response(429, text="slow down"), RateLimited, RATE_LIMITED, _PIPELINE),
        ("kimi", False, httpx.Response(500, text="server error"), ProviderUnavailable, UNAVAILABLE, 1),
        # A refresh token the login server rejects is final and asks the user to log in again.
        (
            "codex",
            True,
            httpx.Response(400, json={"error": "refresh_token_expired"}),
            AuthenticationFailed,
            AUTHENTICATION,
            1,
        ),
        ("codex", True, httpx.Response(403, text="forbidden"), AuthenticationFailed, AUTHENTICATION, 1),
        ("kimi", True, httpx.Response(401, text="invalid_grant"), AuthenticationFailed, AUTHENTICATION, 1),
    ],
    ids=[
        "codex-streamed-refused",
        "codex-blocking-refused",
        "kimi-streamed-refused",
        "kimi-blocking-refused",
        "codex-streamed-unavailable",
        "codex-blocking-bad-gateway",
        "kimi-streamed-throttled",
        "kimi-blocking-server-error",
        "codex-rejected",
        "codex-forbidden",
        "kimi-rejected",
    ],
)
async def test_a_cli_login_refresh_failure_is_retried_only_when_it_is_transient(
    route: str,
    stream: bool,
    refresh_reply: httpx.Response | Exception,
    error: type[ProviderError],
    message: str,
    refreshes: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired CLI login refreshes inside the request; a network failure or a transient status there is retried.

    The error carries the status the login server answered; a refresh that never reached it is a transport failure.
    """
    attempts: list[str] = []

    def refresh(url: str, **_kwargs: object) -> httpx.Response:
        attempts.append(url)
        if isinstance(refresh_reply, Exception):
            raise refresh_reply
        return refresh_reply

    monkeypatch.setattr(httpx, "post", refresh)
    adapter = _adapter(route, tmp_path, lambda _request: pytest.fail("no model request may be sent"))
    _expire_cli_login(route, tmp_path)
    provider = pipeline_provider(adapter, idle_timeout_seconds=_IDLE_LIMIT_SECONDS)

    with pytest.raises(ProviderError) as raised:
        await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")], stream=stream))

    assert (type(raised.value), len(attempts)) == (error, refreshes)
    assert get_user_friendly_error_message(raised.value).startswith(message)
    status = refresh_reply.status_code if isinstance(refresh_reply, httpx.Response) else None
    assert raised.value.status_code == status


_CHAT_COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
}
# The whole reply of every route that answers a blocking request without streaming; Codex always streams.
_WHOLE_REPLIES: dict[str, dict[str, Any]] = {
    "anthropic": claude_message([{"type": "text", "text": "ok"}]),
    "openai_chat": _CHAT_COMPLETION,
    "openai_responses": _RESPONSE_COMPLETED["response"],
    "gemini": _gemini_chunk({"text": "ok"}, finish="STOP"),
    "ollama": _ollama_chunk("ok", done=True),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _WHOLE_REPLIES)
async def test_a_blocking_reply_slower_than_the_idle_limit_is_waited_for(route: str, tmp_path: Path) -> None:
    """Only a stream has an idle limit: a blocking request waits for a slow reply, as the Agno-backed runtime did."""
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        body = json.dumps(_WHOLE_REPLIES[route]).encode()
        stream = PacedBytes([body], pause_seconds=_STALL_SECONDS)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    provider = pipeline_provider(_adapter(route, tmp_path, respond), idle_timeout_seconds=_IDLE_LIMIT_SECONDS)

    turn = await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")], stream=False))

    assert (turn.content, len(sent)) == ("ok", 1)


# --- Usage of a cut-off stream ------------------------------------------------------------------------------------


_CLAUDE_USAGE_DELTA = {
    "type": "message_delta",
    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
    "usage": {"output_tokens": 9},
}
_CHAT_USAGE = {
    "id": "chatcmpl-1",
    "object": "chat.completion.chunk",
    "created": 1,
    "model": "m",
    "choices": [],
    "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
}
# Each route's reply up to and including the event that reports the request's usage, without the stream's end.
_USAGE_THEN_SILENCE: dict[str, Reply] = {
    "anthropic": _claude_stream(
        *_claude_text(0, "ok"),
        {"type": "content_block_stop", "index": 0},
        _CLAUDE_USAGE_DELTA,
    ),
    "openai_chat": _chat_stream(_chat_chunk({"content": "ok"}), _CHAT_USAGE),
    "openai_responses": _responses_stream(_response_text("ok"), _RESPONSE_COMPLETED),
    "gemini": _gemini_stream(_gemini_chunk({"text": "ok"}, finish="STOP")),
    "ollama": _ollama_stream(_ollama_chunk("ok"), _ollama_chunk(done=True)),
}


def _silent_after_usage(
    route: str,
    replies: dict[str, Reply] = _USAGE_THEN_SILENCE,
) -> tuple[_OpenAfter, Callable[[httpx.Request], httpx.Response]]:
    reply = _then_silent(replies[route])()
    assert isinstance(reply, httpx.Response)
    assert isinstance(reply.stream, _OpenAfter)
    body = reply.stream
    return body, lambda _request: reply


def _billed(usage: ReportedUsage) -> tuple[int, int] | None:
    received = usage.received
    return None if received is None else (received.tokens.input_tokens, received.tokens.output_tokens)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _USAGE_THEN_SILENCE)
async def test_usage_the_provider_reported_is_kept_when_the_reply_is_cancelled_after_it(
    route: str,
    tmp_path: Path,
) -> None:
    """A reply cancelled after the provider reported its usage, before the stream ended, still settles that usage."""
    body, respond = _silent_after_usage(route)
    provider = pipeline_provider(_adapter(route, tmp_path, respond), idle_timeout_seconds=None)
    usage = ReportedUsage()

    async def consume() -> None:
        async for event in provider.stream(ModelRequest(messages=[Message(role="user", content="Hi")])):
            usage.observe(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(body.sent.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _billed(usage) == (40, 9)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _USAGE_THEN_SILENCE)
async def test_usage_the_provider_reported_is_kept_when_the_stream_stalls_after_it(route: str, tmp_path: Path) -> None:
    """A stream that stalls after the provider reported its usage fails at the idle limit and still settles that usage."""
    _, respond = _silent_after_usage(route)
    provider = pipeline_provider(_adapter(route, tmp_path, respond), idle_timeout_seconds=_IDLE_LIMIT_SECONDS)
    usage = ReportedUsage()

    async def consume() -> None:
        async for event in provider.stream(ModelRequest(messages=[Message(role="user", content="Hi")])):
            usage.observe(event)

    with pytest.raises(ProviderUnavailable) as raised:
        await consume()

    assert raised.value.code == STREAM_STALLED
    assert _billed(usage) == (40, 9)


# Gemini usage on a chunk before the finished one; Gemini repeats the reply's cumulative usage on every chunk.
_GEMINI_RUNNING_USAGE = {"promptTokenCount": 40, "candidatesTokenCount": 1, "totalTokenCount": 41}
# Each route's reply up to an event that reports usage before the reply ends, without the stream's end: Claude's
# message start, a Chat server that reports usage on its content chunks, and an unfinished Gemini chunk.
_RUNNING_USAGE_THEN_SILENCE: dict[str, Reply] = {
    "anthropic": _claude_stream(*_claude_text(0, "ok")),
    "openai_chat": _chat_stream(
        {**_chat_chunk({"content": "ok"}), "usage": {"prompt_tokens": 40, "completion_tokens": 1, "total_tokens": 41}},
    ),
    "gemini": _gemini_stream({**_gemini_chunk({"text": "ok"}), "usageMetadata": _GEMINI_RUNNING_USAGE}),
    "gemini_vertexai": _gemini_stream({**_gemini_chunk({"text": "ok"}), "usageMetadata": _GEMINI_RUNNING_USAGE}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _RUNNING_USAGE_THEN_SILENCE)
async def test_usage_reported_before_the_reply_ends_is_kept_when_the_reply_is_cancelled_after_it(
    route: str,
    tmp_path: Path,
) -> None:
    """A reply cancelled after an intermediate chunk that reported usage settles that chunk's usage."""
    body, respond = _silent_after_usage(route, _RUNNING_USAGE_THEN_SILENCE)
    provider = pipeline_provider(_adapter(route, tmp_path, respond), idle_timeout_seconds=None)
    usage = ReportedUsage()

    async def consume() -> None:
        async for event in provider.stream(ModelRequest(messages=[Message(role="user", content="Hi")])):
            usage.observe(event)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(body.sent.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _billed(usage) == (40, 1)


# Each route's reply whose usage arrives on the chunk that also carries output: Gemini's finished chunk, a Chat
# server's content chunk, and Ollama's done chunk.
_USAGE_WITH_OUTPUT: dict[str, Reply] = {
    "openai_chat": _chat_stream({**_chat_chunk({"content": "ok"}), "usage": _CHAT_USAGE["usage"]}),
    "gemini": _gemini_stream(_gemini_chunk({"text": "ok"}, finish="STOP")),
    "gemini_vertexai": _gemini_stream(_gemini_chunk({"text": "ok"}, finish="STOP")),
    "ollama": _ollama_stream(_ollama_chunk("ok", done=True)),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _USAGE_WITH_OUTPUT)
async def test_usage_is_kept_when_the_reply_is_cancelled_while_its_last_chunk_is_delivered(
    route: str,
    tmp_path: Path,
) -> None:
    """A reply cancelled while the chunk that carries its usage delivers its text still settles that usage."""
    provider = pipeline_provider(
        _adapter(route, tmp_path, lambda _request: _USAGE_WITH_OUTPUT[route]()),
        idle_timeout_seconds=None,
    )
    usage = ReportedUsage()
    delivered = asyncio.Event()

    async def consume() -> None:
        async for event in provider.stream(ModelRequest(messages=[Message(role="user", content="Hi")])):
            usage.observe(event)
            if isinstance(event, TextDelta):
                delivered.set()
                await asyncio.get_running_loop().create_future()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(delivered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _billed(usage) == (40, 9)
