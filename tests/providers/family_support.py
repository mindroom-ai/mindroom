"""Every provider family's bare adapter over a mock wire, with the wire shapes and replies its tests share.

Each family is built from a real model config over an httpx mock transport, as the loader builds it, so a test that
drives a family exercises the production adapter. ``test_provider_families.py`` wraps them in the production
pipeline for the behavior every family must share, and ``tests/test_agent_mode_history.py`` drives them bare.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from tests.providers.anthropic_family_support import provider_for
from tests.providers.gemini_ollama_support import build_adapter
from tests.providers.golden_support import Wire, ndjson_response, sse_response

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from mindroom.llm.provider import ModelProvider

# A Claude id without native tool search, so every family takes the portable tool-search projection. Every such id
# is on the SDK's deprecation list, which warns per request; the mock wire never reaches the API.
_ANTHROPIC = {"provider": "anthropic", "id": "claude-3-7-sonnet-latest", "api_key": "test-key"}
_GEMINI = {"provider": "google", "id": "gemini-3.8-flash", "api_key": "test-key"}
# Vertex AI has no Files API, so every attachment is inlined or rejected before the request.
_GEMINI_VERTEX = {
    "provider": "google",
    "id": "gemini-3.8-flash",
    "extra_kwargs": {"vertexai": True, "project_id": "test-project", "location": "us-central1"},
}
_OLLAMA = {"provider": "ollama", "id": "qwen3.8:27b", "host": "http://ollama.test:11434"}
_OPENAI_CHAT = {"provider": "openai", "api": "chat_completions", "id": "gpt-6-astra", "api_key": "test-key"}
# A proxied Responses endpoint has no native tool search, so this family too takes the portable projection.
_OPENAI_RESPONSES = {
    "provider": "openai",
    "api": "responses",
    "id": "gpt-6-astra",
    "api_key": "test-key",
    "extra_kwargs": {"base_url": "https://proxy.example/v1"},
}
_CEREBRAS = {"provider": "cerebras", "id": "gpt-oss-120b", "api_key": "test-key"}
_GROQ = {"provider": "groq", "id": "openai/gpt-oss-120b", "api_key": "test-key"}
PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AWS_",
    "CLOUD_ML_",
    "GEMINI_",
    "GOOGLE_",
    "OLLAMA_",
    "OPENAI_",
    "CEREBRAS_",
    "GROQ_",
)
# The SDK warns on every request that names the Claude id above; a module that drives that family filters it.
CLAUDE_DEPRECATION_FILTER = f"ignore:The model '{_ANTHROPIC['id']}' is deprecated:DeprecationWarning"


def scrub_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the host's provider credentials and endpoints, so no SDK falls back to them."""
    for name in list(os.environ):
        if name.startswith(PROVIDER_ENV_PREFIXES):
            monkeypatch.delenv(name)


# --- Per-family wire shapes -------------------------------------------------------------------------------------


def claude_events(
    blocks: list[dict[str, Any]],
    *,
    deltas: list[dict[str, Any]] | None = None,
    stop_reason: str = "end_turn",
) -> list[dict[str, Any]]:
    """A Claude stream: the start, each block whole (the last with ``deltas``), and the stop."""
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": _ANTHROPIC["id"],
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 40, "output_tokens": 1},
    }
    events: list[dict[str, Any]] = [{"type": "message_start", "message": message}]
    for index, block in enumerate(blocks):
        events.append({"type": "content_block_start", "index": index, "content_block": block})
        if index == len(blocks) - 1:
            events.extend({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas or [])
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 9},
        },
    )
    events.append({"type": "message_stop"})
    return events


def gemini_chunk(*parts: dict[str, Any], finish: bool = False) -> dict[str, Any]:
    """One Gemini stream chunk carrying ``parts``; the finishing chunk adds the stop reason and the usage."""
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": list(parts)}, "index": 0}
    if finish:
        candidate["finishReason"] = "STOP"
    chunk: dict[str, Any] = {"candidates": [candidate]}
    if finish:
        chunk["usageMetadata"] = {"promptTokenCount": 40, "candidatesTokenCount": 9}
    return chunk


def ollama_chunk(content: str = "", *, done: bool = False) -> dict[str, Any]:
    """One Ollama NDJSON chunk with ``content``; the done chunk adds the stop reason and the token counts."""
    chunk: dict[str, Any] = {
        "model": _OLLAMA["id"],
        "created_at": "2026-10-20T00:00:00Z",
        "done": done,
        "message": {"role": "assistant", "content": content},
    }
    if done:
        chunk.update({"done_reason": "stop", "prompt_eval_count": 40, "eval_count": 9})
    return chunk


def _chat_chunk(delta: dict[str, Any] | None, *, finish_reason: str | None = None) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-6-astra",
    }
    if delta is None:
        chunk.update({"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49}})
    else:
        chunk["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
    return chunk


def _responses_events(text: str, usage: dict[str, int]) -> list[dict[str, Any]]:
    response = {"id": "resp_1", "object": "response", "created_at": 1, "model": "gpt-6-astra"}
    content = [{"type": "output_text", "text": text, "annotations": []}]
    output = [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed", "content": content}]
    return [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {**response, "status": "in_progress", "output": []},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": text,
            "logprobs": [],
        },
        {
            "type": "response.completed",
            "sequence_number": 2,
            "response": {**response, "status": "completed", "output": output, "usage": usage},
        },
    ]


def _claude_reply() -> httpx.Response:
    return sse_response(claude_events([{"type": "text", "text": "ok"}]), named=True)


def _gemini_reply() -> httpx.Response:
    return sse_response([gemini_chunk({"text": "ok"}, finish=True)])


def _ollama_reply() -> httpx.Response:
    return ndjson_response([ollama_chunk("ok"), ollama_chunk(done=True)])


def _chat_reply() -> httpx.Response:
    return sse_response(
        [_chat_chunk({"content": "ok"}), _chat_chunk({}, finish_reason="stop"), _chat_chunk(None), "[DONE]"],
    )


def _groq_reply() -> httpx.Response:
    """A Groq stream: its usage arrives in Groq's ``x_groq`` extension of the finishing chunk."""
    finish = _chat_chunk({}, finish_reason="stop")
    finish["x_groq"] = {"id": "req_1", "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49}}
    return sse_response([_chat_chunk({"content": "ok"}), finish, "[DONE]"])


def _responses_reply() -> httpx.Response:
    return sse_response(
        _responses_events("ok", {"input_tokens": 40, "output_tokens": 9, "total_tokens": 49}),
        named=True,
    )


def _openai_family_adapter(config: dict[str, Any], root: Path, wire: Wire) -> ModelProvider:
    config_path = root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime = ProviderRuntime(
        runtime_paths=resolve_runtime_paths(config_path=config_path, storage_path=root / "data", process_env={}),
        http_client=httpx.AsyncClient(transport=wire.transport),
    )
    return build_provider(ModelConfig(**config), runtime)


@dataclass(frozen=True)
class Family:
    """How one family's adapter is built and how its wire shapes a request and a text reply."""

    config: dict[str, Any]
    reply: Callable[[], httpx.Response]
    # The function definitions as the wire sends them, keyed by tool name.
    wire_tools: Callable[[dict[str, Any]], dict[str, dict[str, Any]]]
    # The text of the last user message as the wire sends it.
    user_text: Callable[[dict[str, Any]], str] = lambda body: body["messages"][-1]["content"]
    # The media kind the route omits up front; empty when it omits none.
    omitted_media: str = ""

    def adapter(self, root: Path, wire: Wire) -> ModelProvider:
        """Build the family's bare adapter under ``root``, answering through ``wire``."""
        if self.config["provider"] == "anthropic":
            return provider_for(self.config, root, httpx.AsyncClient(transport=wire.transport))
        if self.config["provider"] in {"google", "ollama"}:
            return build_adapter(self.config, root, wire.respond)
        return _openai_family_adapter(self.config, root, wire)


def _chat_tools(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {tool["function"]["name"]: tool["function"] for tool in body["tools"]}


def _gemini_tools(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in body["tools"][0]["functionDeclarations"]}


def _gemini_user_text(body: dict[str, Any]) -> str:
    return body["contents"][-1]["parts"][0]["text"]


FAMILIES = {
    "anthropic": Family(
        _ANTHROPIC,
        _claude_reply,
        lambda body: {tool["name"]: tool for tool in body["tools"]},
        user_text=lambda body: body["messages"][-1]["content"][0]["text"],
        omitted_media="audio",
    ),
    "gemini": Family(_GEMINI, _gemini_reply, _gemini_tools, user_text=_gemini_user_text),
    "gemini_vertexai": Family(_GEMINI_VERTEX, _gemini_reply, _gemini_tools, user_text=_gemini_user_text),
    "ollama": Family(_OLLAMA, _ollama_reply, _chat_tools, omitted_media="audio"),
    "openai_chat": Family(_OPENAI_CHAT, _chat_reply, _chat_tools, omitted_media="video"),
    "openai_responses": Family(
        _OPENAI_RESPONSES,
        _responses_reply,
        lambda body: {tool["name"]: tool for tool in body["tools"]},
        user_text=lambda body: body["input"][-1]["content"],
        omitted_media="video",
    ),
    "cerebras": Family(_CEREBRAS, _chat_reply, _chat_tools, omitted_media="audio"),
    "groq": Family(_GROQ, _groq_reply, _chat_tools, omitted_media="audio"),
}
