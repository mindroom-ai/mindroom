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

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.llm.errors import (
    INCOMPLETE_STREAM,
    MALFORMED_TOOL_CALL,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, complete
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.retry import STREAM_STALLED
from tests.providers.anthropic_family_support import ROUTE_CONFIGS, provider_for
from tests.providers.family_support import scrub_provider_env
from tests.providers.gemini_ollama_support import build_adapter
from tests.providers.golden_support import ndjson_response, pipeline_provider, sse_response
from tests.providers.openai_family_support import write_cli_logins

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

type Reply = Callable[[], httpx.Response | Exception]

# The idle limit every row runs with; a stalled reply pauses past it before its first event.
_IDLE_LIMIT_SECONDS = 0.3
_STALL_SECONDS = 0.6
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
    root.mkdir(parents=True, exist_ok=True)
    homes = write_cli_logins(root)
    config = json.loads(json.dumps(_OPENAI_ROUTES[route]))
    for key, value in (config.get("extra_kwargs") or {}).items():
        config["extra_kwargs"][key] = homes.get(value, value)
    config_path = root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime = ProviderRuntime(
        runtime_paths=resolve_runtime_paths(config_path=config_path, storage_path=root / "data", process_env={}),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return build_provider(ModelConfig(**config), runtime)


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
_WEB_SEARCH_STARTED = {
    "type": "response.output_item.added",
    "sequence_number": 1,
    "output_index": 0,
    "item": {"id": "ws_1", "type": "web_search_call", "status": "in_progress"},
}
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
        "http_400_context_window",
        _claude_status(400, "invalid_request_error", "prompt is too long: 210000 tokens > 200000 maximum"),
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
        ProviderError,
        AUTHENTICATION,
        1,
        1,
    ),
    _Case(
        "http_403",
        _claude_status(403, "permission_error", "Your API key does not have permission to use the specified resource."),
        ProviderError,
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
        _claude_stream(
            *_claude_block(0, {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}),
            _claude_error("api_error"),
        ),
        ProviderError,
        UNAVAILABLE,
        1,
        code=INCOMPLETE_STREAM,
    ),
]
_CLAUDE_SHARED = [case for case in _CLAUDE_CASES if case.failure in {"http_529", "event_api_error", "connect_error"}]

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
    _Case(
        "http_413",
        _openai_status(413, "Request Entity Too Large", "invalid_request_error"),
        InvalidRequest,
        ERROR,
        1,
        1,
    ),
    _Case(
        "http_401",
        _openai_status(401, "Incorrect API key provided.", "invalid_request_error", "invalid_api_key"),
        ProviderError,
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
_CHAT_SHARED = [case for case in _CHAT_CASES if case.failure in {"http_503", "http_400", "event_server_error"}]

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
    _Case("event_invalid_prompt", _responses_stream(_response_error("invalid_prompt")), InvalidRequest, ERROR, 1),
    _Case("response_failed", _responses_stream(_RESPONSE_FAILED), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
    _Case(
        "end_before_completed",
        _responses_stream(),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        code=INCOMPLETE_STREAM,
    ),
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
        ProviderError,
        UNAVAILABLE,
        1,
        code=INCOMPLETE_STREAM,
    ),
    _Case(
        "reset_after_visible_output",
        _responses_stream(_response_text(_PARTIAL), drop=True),
        ProviderError,
        ERROR,
        1,
        code=INCOMPLETE_STREAM,
    ),
    _Case(
        "end_after_visible_output",
        _responses_stream(_response_text(_PARTIAL)),
        ProviderError,
        UNAVAILABLE,
        1,
        code=INCOMPLETE_STREAM,
    ),
    _Case(
        "event_after_hosted_tool",
        _responses_stream(_WEB_SEARCH_STARTED, _response_error("server_error")),
        ProviderError,
        UNAVAILABLE,
        1,
        code=INCOMPLETE_STREAM,
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
        InvalidRequest,
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
    _Case(
        "reset_after_grounding",
        _gemini_stream(_gemini_chunk(grounding=True), drop=True),
        ProviderError,
        ERROR,
        1,
        code=INCOMPLETE_STREAM,
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
_GEMINI_SHARED = [case for case in _GEMINI_CASES if case.failure in {"http_503", "http_400", "event_status_503"}]

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
]

ROWS = [
    *_rows("anthropic", _CLAUDE_CASES),
    *_rows("vertexai_claude", _CLAUDE_SHARED),
    *_rows("bedrock_claude", _CLAUDE_SHARED),
    *_rows("openai_chat", _CHAT_CASES),
    *(
        row
        for route in ("openrouter", "azure", "deepseek", "zai", "llama_cpp", "kimi", "groq", "cerebras")
        for row in _rows(route, _CHAT_SHARED)
    ),
    *_rows("openai_responses", _RESPONSES_CASES),
    *_rows("codex", _RESPONSES_CASES, streams_only=True),
    *_rows("gemini", _GEMINI_CASES),
    *_rows("gemini_vertexai", _GEMINI_SHARED),
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
