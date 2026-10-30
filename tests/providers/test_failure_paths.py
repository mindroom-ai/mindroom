"""Failure paths: how each provider route fails, pinned row by row.

Each row scripts the wire's answer to every request of one route, sends one request through the
route's real adapter inside the production pipeline, and pins how many HTTP requests it makes before it fails,
the normalized error it ends with, and how the error reply the user reads begins.
A failure that is not transient is final at once. After a transient failure the pipeline, since the SDK clients send
each request once, reissues the request at most four times and only until the attempt commits, by producing
output the caller could have shown or by starting a hosted tool that acts or costs; a stall it reissues only once.
A comment explains each other case.
Error replies follow the typed error: a rate limit reads as one, any other transient failure as temporary
unavailability, and a request that no longer fits the context window says so.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import httpx2
import pytest
from structlog.testing import capture_logs

from mindroom.agent_loop.helper import complete_prompt
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.output_limit import CUT_OFF_TOOL_CALL_RESULT, output_limit_policy
from mindroom.agent_loop.run import run_agent
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.history.summary_call import generate_compaction_summary
from mindroom.llm.errors import (
    MALFORMED_TOOL_CALL,
    QUOTA_EXHAUSTED,
    TOKENS_PER_MINUTE,
    AuthenticationFailed,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ReportedUsage, TextDelta, complete
from mindroom.providers.pipeline import retry as retry_stage
from mindroom.providers.pipeline.retry import STREAM_STALLED
from tests.agent_loop.helpers import collect, context, spec, toolkit
from tests.providers.anthropic_family_support import ROUTE_CONFIGS, provider_for
from tests.providers.gemini_ollama_support import build_adapter
from tests.providers.golden_support import (
    PacedBytes,
    hide_provider_environment,
    httpx2_transport,
    ndjson_response,
    pipeline_provider,
    sse_response,
)
from tests.providers.openai_family_support import openai_family_adapter, unsigned_jwt

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider
    from tests.providers.golden_support import SentRequest

type Reply = Callable[[], httpx.Response | Exception]

# The idle limit every row runs with; a stalled reply pauses past it before its first event. The limit also covers
# a request's preparation, such as borrowing a CLI login in a thread, so it outlasts that on a loaded machine.
_IDLE_LIMIT_SECONDS = 0.5
_STALL_SECONDS = 1.0
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
)
_PARTIAL = "partial"

RATE_LIMITED = "⏱️ Rate limited."
UNAVAILABLE = "⚠️ Model provider temporarily unavailable after automatic retries."
ERROR = "⚠️ Error: "
AUTHENTICATION = "❌ Authentication failed"
CONTEXT_WINDOW = "⚠️ The conversation no longer fits the model's context window."
OVER_TOKENS_PER_MINUTE = "⚠️ This request is larger than the account's tokens-per-minute limit."


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip every retry's backoff."""
    hide_provider_environment(monkeypatch)
    monkeypatch.setattr(retry_stage, "_retry_delay_seconds", lambda _attempt: 0.0)


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


def _adapter(route: str, root: Path, respond: Callable[[SentRequest], httpx.Response]) -> ModelProvider:
    """Build one route's adapter as the registry does, with its HTTP traffic answered by ``respond``."""
    if route in ROUTE_CONFIGS:
        return provider_for(ROUTE_CONFIGS[route], root, httpx2.AsyncClient(transport=httpx2_transport(respond)))
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


def _chat_cut_off(*events: dict[str, Any]) -> Reply:
    """Return a Chat stream whose body ends after ``events``, without a finish reason or ``[DONE]``.

    The body streams as a server's does: the adapter tells a body that just ended from one that stopped at
    ``[DONE]`` by whether the SDK decoded it to its end, which a body that arrives whole never shows.
    """

    def reply() -> httpx.Response:
        whole = sse_response([_CHAT_START, *events])
        return httpx.Response(whole.status_code, headers=whole.headers, stream=PacedBytes([whole.content]))

    return reply


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
    """One route and failure, and how the owned pipeline ends it.

    ``requests`` counts HTTP requests; ``code`` pins the MindRoom marker that later handling
    reads.
    """

    route: str
    failure: str
    reply: Reply
    requests: int
    error: type[ProviderError]
    message: str
    code: str | None = None

    @property
    def id(self) -> str:
        """The pytest id."""
        return f"{self.route}-{self.failure}"


@dataclass(frozen=True)
class _Case:
    """One failure of a family, with the number of HTTP requests it makes."""

    failure: str
    reply: Reply
    error: type[ProviderError]
    message: str
    requests: int
    code: str | None = None


def _rows(route: str, cases: list[_Case]) -> list[Row]:
    """Return the rows of ``cases`` on ``route``."""
    return [Row(route, case.failure, case.reply, case.requests, case.error, case.message, case.code) for case in cases]


# The pipeline reissues a request four times after an HTTP 408, 409, 429, or 5xx status or a connection failure.
_PIPELINE = 5
_STALLED = 2


# An early end, a stream that ends before the provider finished the turn, is retried like a dropped connection on
# every route until the attempt committed, instead of accepting what had arrived as the finished reply.
# OpenAI-compatible servers may omit the finish reason or ``[DONE]``, so a Chat stream ends early only when it has
# neither.
def _early_ends(before_output: Reply, after_output: Reply) -> list[_Case]:
    """Return the early end before any output and the early end after visible output of one route's wire."""
    return [
        _Case("end_before_output", before_output, ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        _Case("end_after_visible_output", after_output, ProviderUnavailable, UNAVAILABLE, 1),
    ]


_CLAUDE_WEB_SEARCH = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}}
# Anthropic's 400 for an organization without prepaid credit, as the API sends it.
_CLAUDE_CREDIT_TOO_LOW = (
    "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase "
    "credits."
)
# Anthropic's 429 once the organization reached its usage tier's monthly spend cap, as its rate-limit docs show it, and
# without the ``retry-after`` header the docs say it never carries.
_CLAUDE_SPEND_CAP = (
    "You have reached your API usage limits: your organization has crossed its monthly API usage threshold, set based "
    "on your organization's API tier. You will regain access on 2026-09-01 at 00:00 UTC."
)
_CLAUDE_SPEND_CAP_BODY = {
    "type": "error",
    "error": {
        "type": "rate_limit_error",
        "message": _CLAUDE_SPEND_CAP,
        "details": {"error_code": "enforced_spend_limit_reached"},
    },
    "request_id": "req_018EeWyXxfu5pfWkrYcMdjWG",
}
_CLAUDE_CASES = [
    _Case(
        "http_429",
        _claude_status(429, "rate_limit_error", "Slow down"),
        RateLimited,
        RATE_LIMITED,
        _PIPELINE,
    ),
    _Case(
        "http_529",
        _claude_status(529, "overloaded_error", "Overloaded"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_500",
        _claude_status(500, "api_error", "Internal server error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_503_proxy_text",
        _json(503, "upstream connect error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_408",
        _claude_status(408, "timeout_error", "Request timed out"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case("http_400", _claude_status(400, "invalid_request_error", "messages: bad"), InvalidRequest, ERROR, 1),
    _Case(
        "http_422",
        _claude_status(422, "invalid_request_error", "messages.0.content: Input should be a valid list"),
        InvalidRequest,
        ERROR,
        1,
    ),
    _Case(
        "http_400_context_window",
        _claude_status(400, "invalid_request_error", "prompt is too long: 210000 tokens > 200000 maximum"),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
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
    ),
    _Case(
        "http_413",
        _claude_status(413, "request_too_large", "Request exceeds the size"),
        InvalidRequest,
        ERROR,
        1,
    ),
    _Case(
        "http_401",
        _claude_status(401, "authentication_error", "invalid x-api-key"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
    ),
    _Case(
        "http_403",
        _claude_status(403, "permission_error", "Your API key does not have permission to use the specified resource."),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
    ),
    _Case("http_404", _claude_status(404, "not_found_error", "model: x"), InvalidRequest, ERROR, 1),
    # The organization's billing stops every request until it changes, so the request is sent once: no prepaid credit
    # left or a spend limit the organization or a workspace set, both a 400 told apart only by their wording, or a
    # billing or payment problem. The docs give only how a spend limit's message begins and say it then states when
    # access resumes.
    _Case(
        "http_400_credit_balance_too_low",
        _claude_status(400, "invalid_request_error", _CLAUDE_CREDIT_TOO_LOW),
        ProviderError,
        ERROR,
        1,
        code=QUOTA_EXHAUSTED,
    ),
    _Case(
        "http_400_own_spend_limit",
        _claude_status(400, "invalid_request_error", "You have reached your specified API usage limits."),
        ProviderError,
        ERROR,
        1,
        code=QUOTA_EXHAUSTED,
    ),
    _Case(
        "http_400_own_workspace_spend_limit",
        _claude_status(400, "invalid_request_error", "You have reached your specified workspace API usage limits."),
        ProviderError,
        ERROR,
        1,
        code=QUOTA_EXHAUSTED,
    ),
    # A reached spend cap is a rate-limit error by its type, and only its code tells it apart from a throttle.
    _Case(
        "http_429_spend_cap",
        _json(429, _CLAUDE_SPEND_CAP_BODY),
        ProviderError,
        ERROR,
        1,
        code=QUOTA_EXHAUSTED,
    ),
    _Case(
        "http_402_billing_error",
        _claude_status(402, "billing_error", "There's an issue with your billing or payment information."),
        ProviderError,
        ERROR,
        1,
        code=QUOTA_EXHAUSTED,
    ),
    _Case(
        "connect_error",
        _connect_error,
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        code="connection_error",
    ),
    _Case("read_timeout", _read_timeout, ProviderUnavailable, UNAVAILABLE, _PIPELINE, code="timeout"),
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
    # A deterministic error event is final, though it arrives after the 200 status.
    _Case(
        "event_invalid_request_error",
        _claude_stream(_claude_error("invalid_request_error", "Bad block")),
        ProviderError,
        ERROR,
        1,
    ),
    _Case(
        "event_credit_balance_too_low",
        _claude_stream(_claude_error("invalid_request_error", _CLAUDE_CREDIT_TOO_LOW)),
        ProviderError,
        ERROR,
        1,
        code=QUOTA_EXHAUSTED,
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
    # A retry would run the web search again, so the failure is final.
    _Case(
        "event_after_web_search",
        _claude_stream(*_claude_block(0, _CLAUDE_WEB_SEARCH), _claude_error("api_error")),
        ProviderUnavailable,
        UNAVAILABLE,
        1,
    ),
    # A stall after a server tool that acts or costs started is final too.
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
        _PIPELINE,
    ),
    # A request larger than the account's tokens-per-minute limit fails every time; only a smaller one passes.
    _Case(
        "http_429_request_too_large",
        _openai_status(
            429,
            "Request too large for gpt-6-astra in organization org-test on tokens per min (TPM): Limit 30000, "
            "Requested 50000. The input or output tokens must be reduced in order to run successfully. "
            "Visit https://platform.openai.com/account/rate-limits to learn more.",
            "tokens",
            "rate_limit_exceeded",
        ),
        ContextWindowExceeded,
        OVER_TOKENS_PER_MINUTE,
        1,
        code=TOKENS_PER_MINUTE,
    ),
    _Case(
        "http_500",
        _openai_status(500, "The server had an error while processing your request.", "server_error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_503",
        _openai_status(503, "The server is overloaded or not ready yet.", "server_error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_409",
        _openai_status(409, "The request conflicted with another request.", "conflict"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_529",
        _openai_status(529, "The service is overloaded.", "server_error"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_400",
        _openai_status(400, "Invalid value for 'messages'.", "invalid_request_error"),
        InvalidRequest,
        ERROR,
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
    ),
    # Text that names an ``author`` property is not wording about the credentials.
    _Case(
        "http_400_author_property",
        _openai_status(400, "Invalid schema for function 'save_note': 'author' is not of type 'string'.", "invalid"),
        InvalidRequest,
        ERROR,
        1,
    ),
    # A word of the auth family names the credentials beside an underscore and in British spelling.
    _Case(
        "http_403_invalid_auth",
        _openai_status(403, "invalid_auth: the access token was revoked", "invalid_request_error"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
    ),
    _Case(
        "http_403_unauthorised",
        _openai_status(403, "Unauthorised to use this model", "invalid_request_error"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
    ),
    _Case(
        "http_413",
        _openai_status(413, "Request Entity Too Large", "invalid_request_error"),
        InvalidRequest,
        ERROR,
        1,
    ),
    # A client error stays final whatever type a server gives it, a rate-limit type included; the texts are invented.
    *(
        _Case(
            f"http_{status}_rate_limit_type",
            _openai_status(status, "Rejected under a rate-limit type.", "rate_limit_error"),
            InvalidRequest,
            ERROR,
            1,
        )
        for status in (400, 413)
    ),
    _Case(
        "http_422",
        _openai_status(422, "Unprocessable Entity: the request body failed validation.", "invalid_request_error"),
        InvalidRequest,
        ERROR,
        1,
    ),
    _Case(
        "http_401",
        _openai_status(401, "Incorrect API key provided.", "invalid_request_error", "invalid_api_key"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
    ),
    _Case(
        "http_403",
        _openai_status(403, "Project proj_1 does not have access to model gpt-6-astra", "invalid_request_error"),
        ProviderError,
        ERROR,
        1,
    ),
    # An account without credit, or past its spend or usage limit, fails every time until its billing changes, so the
    # reply shows the provider's text. ``insufficient_quota`` is the older code and still the type of the others.
    _Case(
        "http_429_insufficient_quota",
        _openai_status(
            429,
            "You exceeded your current quota, please check your plan and billing details. For more information on "
            "this error, read the docs: https://platform.openai.com/docs/guides/error-codes/api-errors.",
            "insufficient_quota",
            "insufficient_quota",
        ),
        ProviderError,
        f"{ERROR}You exceeded your current quota, please check your plan and billing details.",
        1,
        code=QUOTA_EXHAUSTED,
    ),
    _Case(
        "http_429_credit_balance_exhausted",
        _openai_status(
            429,
            "Your organization has no prepaid credits remaining.",
            "insufficient_quota",
            "credit_balance_exhausted",
        ),
        ProviderError,
        f"{ERROR}Your organization has no prepaid credits remaining.",
        1,
        code=QUOTA_EXHAUSTED,
    ),
    # An invented code stands for a billing code OpenAI adds later, which still carries ``insufficient_quota``.
    _Case(
        "http_429_unlisted_billing_code",
        _openai_status(429, "A billing failure under a new code.", "insufficient_quota", "unlisted_billing_code"),
        ProviderError,
        f"{ERROR}A billing failure under a new code.",
        1,
        code=QUOTA_EXHAUSTED,
    ),
    _Case(
        "http_404",
        _openai_status(404, "The model does not exist.", "invalid_request_error"),
        InvalidRequest,
        ERROR,
        1,
    ),
    _Case(
        "connect_error",
        _connect_error,
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        code="connection_error",
    ),
    _Case("read_timeout", _read_timeout, ProviderUnavailable, UNAVAILABLE, _PIPELINE, code="timeout"),
]
# Until visible text or a complete tool call arrives, a Chat stream keeps its retry budget, so an opening empty-text
# chunk does not make a failure final.
_TOOL_ARGUMENTS = _chat_chunk(
    {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": '{"ci'}}]},
)
_CHAT_CASES = [
    *_OPENAI_HTTP_CASES,
    # Moonshot names a 429 that only a top-up lets through by its error type alone, with one of two typical messages.
    _Case(
        "http_429_moonshot_exceeded_current_quota",
        _openai_status(
            429,
            "Account balance is insufficient or the account has been disabled",
            "exceeded_current_quota_error",
        ),
        ProviderError,
        f"{ERROR}Account balance is insufficient or the account has been disabled",
        1,
        code=QUOTA_EXHAUSTED,
    ),
    # Ollama's OpenAI-compatible endpoint names every status but 400 and 404 ``api_error``, so the status decides.
    _Case(
        "http_413_ollama_api_error",
        _openai_status(413, "request body must not exceed 32 MiB", "api_error"),
        InvalidRequest,
        ERROR,
        1,
    ),
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
    # An api_error or rate_limit_error event is transient whatever its wording.
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
    *_early_ends(_chat_cut_off(), _chat_cut_off(_chat_chunk({"content": _PARTIAL}))),
]
_CHAT_SHARED_FAILURES = frozenset(
    {"http_503", "http_400", "http_429_moonshot_exceeded_current_quota", "event_server_error", "http_200_error_body"},
)
_CHAT_SHARED = [case for case in _CHAT_CASES if case.failure in _CHAT_SHARED_FAILURES]
# OpenRouter and DeepSeek answer an account without credit with a 402 whose body names no billing failure: OpenRouter's
# code is the status, and DeepSeek types it ``unknown_error``. No retry passes until the account is topped up.
_OPENROUTER_NO_CREDIT = _json(
    402,
    {"error": {"code": 402, "message": "Insufficient credits. Add more using https://openrouter.ai/credits"}},
)
_DEEPSEEK_NO_BALANCE = _openai_status(402, "Insufficient Balance", "unknown_error", "invalid_request_error")
_OPENROUTER_CASES = [
    _Case(
        "http_402_insufficient_credits",
        _OPENROUTER_NO_CREDIT,
        ProviderError,
        f"{ERROR}Insufficient credits",
        1,
        code=QUOTA_EXHAUSTED,
    ),
]
_DEEPSEEK_CASES = [
    _Case(
        "http_402_insufficient_balance",
        _DEEPSEEK_NO_BALANCE,
        ProviderError,
        f"{ERROR}Insufficient Balance",
        1,
        code=QUOTA_EXHAUSTED,
    ),
]
# Z.ai answers an account without balance or resource package with a 429 that only its business code tells apart from
# a rate limit.
_ZAI_NO_BALANCE = _json(
    429,
    {"error": {"code": "1113", "message": "Insufficient balance or no resource package. Please recharge."}},
)
_ZAI_CASES = [
    _Case(
        "http_429_insufficient_balance",
        _ZAI_NO_BALANCE,
        ProviderError,
        f"{ERROR}Insufficient balance or no resource package",
        1,
        code=QUOTA_EXHAUSTED,
    ),
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
    # A retry would run the hosted tool again, so a failure after it started is final, a stall included.
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
            ("event", _responses_stream(started, _response_error("server_error")), None),
            ("stall", _then_silent(_responses_stream(started)), STREAM_STALLED),
        )
    ),
    # Rerunning a tool search or a compaction repeats no work, so their items keep the retry budget.
    *(
        _Case(f"{failure}_after_{name}", reply, ProviderUnavailable, UNAVAILABLE, requests, code=code)
        for name, started in (("tool_search", _TOOL_SEARCH_STARTED), ("compaction", _COMPACTION_STARTED))
        for failure, reply, requests, code in (
            ("event", _responses_stream(started, _response_error("server_error")), _PIPELINE, None),
            ("stall", _then_silent(_responses_stream(started)), _STALLED, STREAM_STALLED),
        )
    ),
]

_GEMINI_CREDITS_DEPLETED = (
    "Your prepayment credits are depleted. Please go to AI Studio at https://ai.studio/projects to manage your project "
    "and billing."
)
_GEMINI_CASES = [
    _Case(
        "http_429",
        _gemini_status(429, "Resource has been exhausted (e.g. check quota).", "RESOURCE_EXHAUSTED"),
        RateLimited,
        RATE_LIMITED,
        _PIPELINE,
    ),
    # Gemini sends used-up prepaid credit and a reached monthly spending cap as a 429 like a rate limit, with no code
    # of its own, and Google's troubleshooting guide names a 402 for used-up credit; no retry passes until the credits
    # or the cap change, so the reply shows Google's text.
    *(
        _Case(
            f"http_{status}_{failure}",
            _gemini_status(status, message, reason),
            ProviderError,
            f"{ERROR}{message}",
            1,
            code=QUOTA_EXHAUSTED,
        )
        for status, reason, failure, message in (
            (429, "RESOURCE_EXHAUSTED", "prepayment_credits_depleted", _GEMINI_CREDITS_DEPLETED),
            (402, "PAYMENT_REQUIRED", "prepayment_credits_depleted", _GEMINI_CREDITS_DEPLETED),
            # The wording matches in any case, as the shared classifier's markers do.
            (429, "RESOURCE_EXHAUSTED", "prepayment_credits_depleted_capitalized", "Prepayment credits are depleted."),
            (429, "RESOURCE_EXHAUSTED", "project_spending_cap", "Your project has exceeded its monthly spending cap."),
            (
                429,
                "RESOURCE_EXHAUSTED",
                "billing_account_spending_cap",
                "Your billing account has exceeded its monthly spending cap.",
            ),
        )
    ),
    _Case(
        "http_500",
        _gemini_status(500, "An internal error has occurred.", "INTERNAL"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_503",
        _gemini_status(503, "The model is overloaded. Please try again later.", "UNAVAILABLE"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_504",
        _gemini_status(504, "Deadline exceeded.", "DEADLINE_EXCEEDED"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_529",
        _gemini_status(529, "The model is overloaded.", "UNAVAILABLE"),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_400",
        _gemini_status(400, "Invalid JSON payload received.", "INVALID_ARGUMENT"),
        InvalidRequest,
        ERROR,
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
    ),
    # How Vertex AI, and at times the Gemini API, word an input over the model's window.
    _Case(
        "http_400_context_window_unable_to_submit",
        _gemini_status(
            400,
            "Unable to submit request because the input token count is 106244 but model only supports up to 65536.",
            "INVALID_ARGUMENT",
        ),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
    ),
    _Case(
        "http_400_api_key",
        _gemini_status(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT"),
        AuthenticationFailed,
        AUTHENTICATION,
        1,
    ),
    _Case(
        "http_403",
        _gemini_status(403, "Permission denied on resource project p.", "PERMISSION_DENIED"),
        ProviderError,
        ERROR,
        1,
    ),
    _Case("http_404", _gemini_status(404, "models/gemini-x is not found.", "NOT_FOUND"), InvalidRequest, ERROR, 1),
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
    ),
    _Case(
        "http_422",
        _gemini_status(422, "Unprocessable entity.", "INVALID_ARGUMENT"),
        InvalidRequest,
        ERROR,
        1,
    ),
    _Case("connect_error", _connect_error, ProviderUnavailable, UNAVAILABLE, _PIPELINE, code="connection_error"),
    _Case("read_timeout", _read_timeout, ProviderUnavailable, UNAVAILABLE, _PIPELINE, code="timeout"),
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
    # stall included.
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
    # One guidance retry follows a malformed tool call.
    _Case(
        "malformed_tool_call",
        _gemini_stream(_gemini_chunk(finish="MALFORMED_FUNCTION_CALL")),
        ProviderError,
        ERROR,
        2,
        code=MALFORMED_TOOL_CALL,
    ),
]

# An Ollama stream retries transient failures as every other route's stream does.
_OLLAMA_CASES = [
    _Case(
        "http_500",
        _json(500, {"error": "llama runner process has terminated"}),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case(
        "http_503",
        _json(503, {"error": "server busy, please try again"}),
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
    ),
    _Case("http_400", _json(400, {"error": "invalid options"}), InvalidRequest, ERROR, 1),
    _Case("http_422", _json(422, {"error": "unprocessable request"}), InvalidRequest, ERROR, 1),
    _Case(
        "http_400_context_window",
        _json(400, {"error": "input length exceeds the context length"}),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
    ),
    _Case("http_404", _json(404, {"error": "model 'qwen3.8:27b' not found"}), InvalidRequest, ERROR, 1),
    _Case(
        "connect_error",
        _connect_error,
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
        code="connection_error",
    ),
    _Case(
        "read_timeout",
        _read_timeout,
        ProviderUnavailable,
        UNAVAILABLE,
        _PIPELINE,
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
    ),
]
# Bedrock rejects input and requested output tokens above the model's quota with a ValidationException of its own.
_BEDROCK_CASES = [
    _Case(
        "http_400_input_too_long",
        _json(400, {"message": "Input is too long for requested model."}),
        ContextWindowExceeded,
        CONTEXT_WINDOW,
        1,
    ),
]

# Vertex AI Claude, Bedrock Claude, and Vertex AI Gemini fail exactly as their family's direct route does, so they
# run every row of that family; the other OpenAI-compatible Chat routes run the shared Chat rows, and Bedrock,
# llama.cpp, OpenRouter, DeepSeek, and Z.ai their own wording or bodies too.
ROWS = [
    *_rows("anthropic", _CLAUDE_CASES),
    *_rows("vertexai_claude", _CLAUDE_CASES),
    *_rows("bedrock_claude", _CLAUDE_CASES),
    *_rows("bedrock_claude", _BEDROCK_CASES),
    *_rows("openai_chat", _CHAT_CASES),
    *(
        row
        for route in ("openrouter", "azure", "deepseek", "zai", "llama_cpp", "kimi", "groq", "cerebras")
        for row in _rows(route, _CHAT_SHARED)
    ),
    *_rows("llama_cpp", _LLAMA_CPP_CASES),
    *_rows("openrouter", _OPENROUTER_CASES),
    *_rows("deepseek", _DEEPSEEK_CASES),
    *_rows("zai", _ZAI_CASES),
    *_rows("openai_responses", _RESPONSES_CASES),
    *_rows("codex", _RESPONSES_CASES),
    *_rows("gemini", _GEMINI_CASES),
    *_rows("gemini_vertexai", _GEMINI_CASES),
    *_rows("ollama", _OLLAMA_CASES),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", ROWS, ids=[row.id for row in ROWS])
async def test_a_failed_request_is_retried_and_reported_as_its_row_says(row: Row, tmp_path: Path) -> None:
    """The route retries the failure as often as the row says, ends with its error, and reports it as the row says."""
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = row.reply()
        if isinstance(answer, Exception):
            raise answer
        return answer

    provider = pipeline_provider(_adapter(row.route, tmp_path, respond), idle_timeout_seconds=_IDLE_LIMIT_SECONDS)
    request = ModelRequest(messages=[Message(role="user", content="Hi")])

    with pytest.raises(ProviderError) as raised:
        await complete(provider, request)

    assert (type(raised.value), len(sent)) == (row.error, row.requests)
    assert get_user_friendly_error_message(raised.value).startswith(row.message)
    if row.code is not None:
        assert raised.value.code == row.code


# How a route's provider answers a request the account's billing stops.
_BILLING_STOPS: dict[str, Reply] = {
    **dict.fromkeys(ROUTE_CONFIGS, _claude_status(400, "invalid_request_error", _CLAUDE_CREDIT_TOO_LOW)),
    "openrouter": _OPENROUTER_NO_CREDIT,
    "deepseek": _DEEPSEEK_NO_BALANCE,
    "zai": _ZAI_NO_BALANCE,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_BILLING_STOPS))
async def test_a_request_the_account_s_billing_stops_is_not_resent_without_its_media(
    route: str,
    tmp_path: Path,
) -> None:
    """The image did not cause the failure, so the request is not sent a second time without it."""
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return _BILLING_STOPS[route]()

    provider = pipeline_provider(_adapter(route, tmp_path, respond))
    image = Image(content=_PNG, mime_type="image/png")

    with pytest.raises(ProviderError) as raised:
        await complete(provider, ModelRequest(messages=[Message(role="user", content="Look", images=[image])]))

    assert (raised.value.code, len(sent)) == (QUOTA_EXHAUSTED, 1)


# --- A response the provider ended incomplete --------------------------------------------------------------------


# A call the output limit cut off mid-arguments, as the response lists it.
_CUT_OFF_CALL_ITEM = {
    "id": "fc_1",
    "type": "function_call",
    "call_id": "call_1",
    "name": "get_weather",
    "arguments": '{"city": "Par',
    "status": "incomplete",
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


def _incomplete(reason: str, *, cut_off_call: bool) -> Reply:
    response = _incomplete_response(reason, cut_off_call=cut_off_call)
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


# A response that ended incomplete ends the turn with its stop reason after one request.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "stop_reason"),
    [("max_output_tokens", "output_limit"), ("content_filter", "content_filter")],
    ids=["output_limit", "content_filter"],
)
@pytest.mark.parametrize("route", ["openai_responses", "codex"])
async def test_a_response_that_ended_incomplete_ends_the_turn_with_its_stop_reason(
    route: str,
    reason: str,
    stop_reason: str,
    tmp_path: Path,
) -> None:
    """The output limit and the content filter end the reply with their stop reason, without a retry."""
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = _incomplete(reason, cut_off_call=False)()
        assert isinstance(answer, httpx.Response)
        return answer

    provider = pipeline_provider(_adapter(route, tmp_path, respond), idle_timeout_seconds=_IDLE_LIMIT_SECONDS)

    with capture_logs() as logs:
        turn = await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")]))

    assert (turn.content, turn.stop_reason, turn.usage.tokens.output_tokens, len(sent)) == (_PARTIAL, stop_reason, 9, 1)
    # An attempt that stalls before its request goes out, as on a loaded machine, logs a line without usage.
    usage_lines = [entry for entry in logs if entry["event"] == "LLM usage" and entry["usage_available"]]
    assert [entry["stop_reason"] for entry in usage_lines] == [stop_reason]


def get_weather(city: str) -> str:
    """Return the weather in a city.

    Args:
        city: The city.

    """
    return f"Sunny in {city}."


def _chat_end(finish_reason: str) -> dict[str, Any]:
    chunk = _chat_chunk({})
    chunk["choices"][0]["finish_reason"] = finish_reason
    return chunk


def _chat_cut_off_call() -> Reply:
    """A Chat reply the output limit cut off inside the arguments of its tool call."""
    call = {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "Par'}}
    call_chunk = _chat_chunk({"tool_calls": [{"index": 0, **call}]})
    return _chat_stream(_chat_chunk({"content": _PARTIAL}), call_chunk, _chat_end("length"), "[DONE]")


def _tool_results(route: str, body: dict[str, Any]) -> list[tuple[str, str]]:
    """Return the tool results a request body sends, as call id and output."""
    if route == "openai_chat":
        return [
            (message["tool_call_id"], message["content"]) for message in body["messages"] if message["role"] == "tool"
        ]
    return [(item["call_id"], item["output"]) for item in body["input"] if item.get("type") == "function_call_output"]


# The output-limit policy, which every run's hooks now carry (``tests/test_loop_hooks.py``), answers a call the output
# limit cut off on the Responses, Codex and Chat routes as not run, so the model reads why and splits the work; such a
# call used to run there when its partial arguments parsed. A stored response still anchors the next request, which
# answers the cut-off call it lists.
@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["openai_responses", "codex", "openai_chat"])
async def test_a_call_the_output_limit_cut_off_is_answered_without_running_it(route: str, tmp_path: Path) -> None:
    """The cut-off call never runs; the next request tells the model it hit the output limit."""
    if route == "openai_chat":
        cut_off = _chat_cut_off_call()
        finished = _chat_stream(_chat_chunk({"content": "ok"}), _chat_end("stop"), "[DONE]")
    else:
        cut_off = _incomplete("max_output_tokens", cut_off_call=True)
        finished = _responses_stream(_response_text("ok"), _RESPONSE_COMPLETED)
    replies = iter([cut_off, finished])
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        answer = next(replies)()
        assert isinstance(answer, httpx.Response)
        return answer

    agent = spec(pipeline_provider(_adapter(route, tmp_path, respond)), toolkit(get_weather))

    with capture_logs() as logs:
        _, record = await collect(
            run_agent(
                agent,
                [Message(role="user", content="Weather in Paris?")],
                ctx=context(),
                persist=None,
                request_template=ModelRequest(messages=()),
                hooks=LoopHooks(answer_tool_calls=output_limit_policy()),
            ),
        )

    assert (record.content, len(bodies)) == (f"{_PARTIAL}\n\nok", 2)
    assert _tool_results(route, bodies[1]) == [("call_1", CUT_OFF_TOOL_CALL_RESULT)]
    if route == "openai_responses":
        assert bodies[1]["previous_response_id"] == "resp_1"
    assert [entry.get("stop_reason") for entry in logs if entry["event"] == "LLM usage"] == ["output_limit", "end"]


# Each route's answer to a request its server failed, which the stream retry would reissue.
_SERVER_FAILURES = {row.route: row.reply for row in ROWS if row.failure in {"http_500", "http_503"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_SERVER_FAILURES))
async def test_a_compaction_summary_attempt_sends_one_request_on_every_route(route: str, tmp_path: Path) -> None:
    """Compaction retries a failed summary itself, so the stream retry does not reissue an attempt."""
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
            on_usage=None,
        )

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_compaction_summary_cut_off_after_its_text_is_not_returned(tmp_path: Path) -> None:
    """A Chat summary stream whose body ends after a text delta, without a finish reason or ``[DONE]``, is no summary.

    Compaction would otherwise store the partial text as the summary of everything it replaces.
    """
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = _chat_cut_off(_chat_chunk({"content": _PARTIAL}))()
        assert isinstance(answer, httpx.Response)
        return answer

    provider = pipeline_provider(_adapter("openai_chat", tmp_path, respond), idle_timeout_seconds=_IDLE_LIMIT_SECONDS)

    with pytest.raises(ProviderUnavailable):
        await generate_compaction_summary(
            model=provider,
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
            on_usage=None,
        )

    assert len(sent) == 1


# The answer that ends one request with "ok" on two routes.
_OK_STREAMS: dict[str, Reply] = {
    "gemini": _gemini_stream(_gemini_chunk({"text": "ok"}, finish="STOP")),
    "ollama": _ollama_stream(_ollama_chunk("ok"), _ollama_chunk(done=True)),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("route", sorted(_OK_STREAMS))
async def test_a_helper_request_is_retried_after_a_server_failure(route: str, tmp_path: Path) -> None:
    """A helper that reads only the finished answer gets the pipeline's retries, as a reply's requests do."""
    replies = [_SERVER_FAILURES[route], _OK_STREAMS[route]]
    sent: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        answer = replies[min(len(sent), len(replies)) - 1]()
        assert isinstance(answer, httpx.Response)
        return answer

    provider = pipeline_provider(_adapter(route, tmp_path, respond), idle_timeout_seconds=_IDLE_LIMIT_SECONDS)

    result = await complete_prompt(provider, "Hi")

    assert (result.content, len(sent)) == ("ok", 2)


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
    ("route", "refresh_reply", "error", "message", "refreshes"),
    [
        # A refresh that fails on the network is the provider's transient failure and is retried.
        ("codex", _connect_error(), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        ("kimi", _connect_error(), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        # A login server that is down or throttling answers with its status, which the shared classifier reads as for
        # any provider response.
        ("codex", httpx.Response(503, text="upstream down"), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        ("codex", httpx.Response(502, text="bad gateway"), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        ("kimi", httpx.Response(429, text="slow down"), RateLimited, RATE_LIMITED, _PIPELINE),
        ("kimi", httpx.Response(500, text="server error"), ProviderUnavailable, UNAVAILABLE, _PIPELINE),
        # A refresh token the login server rejects is final and asks the user to log in again.
        (
            "codex",
            httpx.Response(400, json={"error": "refresh_token_expired"}),
            AuthenticationFailed,
            AUTHENTICATION,
            1,
        ),
        ("codex", httpx.Response(403, text="forbidden"), AuthenticationFailed, AUTHENTICATION, 1),
        ("kimi", httpx.Response(401, text="invalid_grant"), AuthenticationFailed, AUTHENTICATION, 1),
    ],
    ids=[
        "codex-refused",
        "kimi-refused",
        "codex-unavailable",
        "codex-bad-gateway",
        "kimi-throttled",
        "kimi-server-error",
        "codex-rejected",
        "codex-forbidden",
        "kimi-rejected",
    ],
)
async def test_a_cli_login_refresh_failure_is_retried_only_when_it_is_transient(
    route: str,
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
        await complete(provider, ModelRequest(messages=[Message(role="user", content="Hi")]))

    assert (type(raised.value), len(attempts)) == (error, refreshes)
    assert get_user_friendly_error_message(raised.value).startswith(message)
    status = refresh_reply.status_code if isinstance(refresh_reply, httpx.Response) else None
    assert raised.value.status_code == status


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
