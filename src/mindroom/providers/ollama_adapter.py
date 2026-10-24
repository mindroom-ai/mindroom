# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Ollama provider adapter over the ollama SDK's native ``/api/chat`` endpoint.

The native endpoint keeps Ollama's ``options`` and ``keep_alive`` settings.
The SDK is imported on first use, so importing this module stays cheap.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast
from uuid import uuid4

from mindroom.credentials.sync import get_ollama_host
from mindroom.llm.errors import InvalidRequest, ProviderError
from mindroom.llm.messages import split_thinking
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.tool_search import call_arguments
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.logging_config import get_logger
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT
from mindroom.providers.assistant_turn import finished_assistant_message, output_started
from mindroom.providers.errors import classify_provider_error, provider_failure
from mindroom.providers.extra_kwargs import SECRET, settings_from_extra_kwargs, settings_params
from mindroom.providers.remote_media import download_media
from mindroom.providers.tool_policy import provider_tools_disabled

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence

    from ollama import AsyncClient, ChatResponse

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.media import Image
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest, StopReason, StreamEvent, ToolSpec

logger = get_logger(__name__)

_PROVIDER = "Ollama"
# Remote images are downloaded up to this size; a larger one is left to the media fallback's note.
_IMAGE_LIMIT_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True, kw_only=True)
class OllamaSettings:
    """The ``extra_kwargs`` an Ollama model accepts."""

    api_key: str | None = field(default=None, metadata=SECRET)
    timeout: float | None = None
    client_params: Mapping[str, Any] | None = field(default=None, metadata=SECRET)
    format: str | Mapping[str, Any] | None = None
    options: Mapping[str, Any] | None = None
    keep_alive: float | str | None = None
    request_params: Mapping[str, Any] | None = None


@dataclass(slots=True)
class _OllamaReply:
    content: str = ""
    reasoning: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: MessageMetrics | None = None
    done_reason: str | None = None
    done: bool = False


@dataclass(slots=True, kw_only=True)
class OllamaProvider:
    """One Ollama model; the SDK client is built on the first request and reused."""

    identity: ModelIdentity
    capabilities: Capabilities
    settings: OllamaSettings
    host: str
    _client: AsyncClient | None = field(default=None, init=False, repr=False)
    # Ollama keeps no history of its own, and its replayed history is sized canonically.
    native_history_route: ClassVar[None] = None
    replay_estimator: ClassVar[None] = None

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one chat request, streamed unless ``request.stream`` is False, and yield its events."""
        messages = [await _format_message(message, self.identity.provider_name) for message in request.messages]
        model = self.identity.id.strip()
        chat_kwargs = self._chat_kwargs(request)
        reply = _OllamaReply()
        started = time.monotonic()
        first_output_at: float | None = None
        try:
            client = self._sdk_client()
            if request.stream:
                chunks = await client.chat(model=model, messages=messages, stream=True, **chat_kwargs)
                async with aclosing(cast("AsyncGenerator[ChatResponse]", chunks)) as responses:
                    async for chunk in responses:
                        for event in _read_chunk(reply, chunk) or [StreamProgress()]:
                            first_output_at = output_started(first_output_at, event)
                            yield event
            else:
                response = await client.chat(model=model, messages=messages, **chat_kwargs)
                first_output_at = time.monotonic()
                _read_chunk(reply, cast("ChatResponse", response))
                # Only a whole reply with an opening ``<think>`` tag has its reasoning split out, kept as main behaves.
                if "<think>" in reply.content:
                    reply.reasoning, reply.content = split_thinking(reply.content)
        except Exception as error:
            raise _provider_error(error, provider=self.identity.provider_name) from error
        if request.stream and not reply.done:
            # The stream ended before its done chunk, so the turn did not finish.
            return
        message = finished_assistant_message(
            self.identity,
            content=reply.content or None,
            reasoning=reply.reasoning,
            tool_calls=reply.tool_calls,
            usage=reply.usage,
            started=started,
            first_output_at=first_output_at,
        )
        if reply.usage is not None:
            yield UsageReported(message.metrics)
        yield TurnCompleted(message=message, stop_reason=_stop_reason(reply))

    def _chat_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        """Return the chat parameters of one request; authored ``request_params`` override the settings and tools."""
        settings = self.settings
        # TODO(simplify): a request's temperature override, such as a thread summary's, is ignored. Send it as
        # ``options.temperature``, as other routes apply theirs. Models would notice (#3075).
        authored = {"format": settings.format, "options": settings.options or None, "keep_alive": settings.keep_alive}
        kwargs: dict[str, Any] = {key: value for key, value in authored.items() if value is not None}
        # TODO(simplify): ``tool_choice: "none"`` still sends the tools, so the model can call them; only a decision
        # request drops them. Drop the tools whenever ``tool_choice`` is "none". Models would notice (#3075).
        if tools := _format_tools(request.tools):
            kwargs["tools"] = tools
        kwargs.update(settings.request_params or {})
        if provider_tools_disabled():
            # Ollama has no tool_choice, so a decision request carries no tools at all.
            kwargs.pop("tools", None)
        if request.output_schema is not None and "format" not in kwargs:
            kwargs["format"] = request.output_schema.model_json_schema()
        return kwargs

    def _sdk_client(self) -> AsyncClient:
        if self._client is None:
            from ollama import AsyncClient  # noqa: PLC0415 - provider SDK loads on first request

            self._client = AsyncClient(**self._client_params())
        return self._client

    def _client_params(self) -> dict[str, Any]:
        settings = self.settings
        params: dict[str, Any] = {"host": self.host}
        if settings.timeout is not None:
            params["timeout"] = settings.timeout
        if settings.api_key:
            params["headers"] = {"authorization": f"Bearer {settings.api_key}"}
        return {**params, **(settings.client_params or {})}


def _provider_error(error: Exception, *, provider: str) -> ProviderError:
    """Return the normalized error for one failed request; any other exception propagates as it is."""
    from ollama import ResponseError  # noqa: PLC0415 - provider SDK loads on first request

    if (failure := provider_failure(error, provider=provider)) is not None:
        return failure
    if isinstance(error, ResponseError):
        # The SDK reports an error the server streams after a 200 response with status -1.
        status_code = error.status_code if error.status_code > 0 else None
        return classify_provider_error(str(error), provider=provider, status_code=status_code)
    raise error


def _format_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Return Ollama tool definitions sorted by name; a tool without a description sends none."""
    # TODO(simplify): the SDK's tool model keeps only a parameter's type, items, description, and enum, so an
    # ``anyOf`` parameter such as ``background_after`` reaches the model without a type. Send such a parameter
    # with its type. Models would notice (#3075).
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                **({} if tool.description is None else {"description": tool.description}),
                "parameters": tool.parameters,
            },
        }
        for tool in sorted(tools, key=lambda tool: tool.name)
    ]


async def _format_message(message: Message, provider: str) -> dict[str, Any]:
    """Return one chat message; images are read off the event loop or downloaded.

    An image that cannot be read raises ``InvalidRequest``, so the media fallback replaces it with its note.
    """
    formatted: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        formatted["tool_calls"] = [
            {
                "id": call.get("id"),
                "type": "function",
                "function": {"name": call["function"]["name"], "arguments": call_arguments(call["function"])},
            }
            for call in message.tool_calls
        ]
    if message.role == "user":
        images = [data for image in message.images or () if (data := await _image_bytes(image, provider))]
        if images:
            formatted["images"] = images
    return formatted


async def _image_bytes(image: Image, provider: str) -> bytes | None:
    if image.url is not None:
        data, _ = await download_media(image.url, kind="image", limit_bytes=_IMAGE_LIMIT_BYTES, provider=provider)
        return data
    if image.filepath is None:
        return image.content
    return await asyncio.to_thread(_read_image, Path(image.filepath), provider)


def _read_image(path: Path, provider: str) -> bytes:
    try:
        if not path.is_file():
            raise FileNotFoundError(path)
        return path.read_bytes()
    except OSError as error:
        logger.warning("ollama_media_file_unreadable", path=str(path), error=str(error))
        msg = "The image attachment could not be read"
        raise InvalidRequest(msg, provider=provider) from error


def _read_chunk(reply: _OllamaReply, chunk: ChatResponse) -> list[StreamEvent]:
    """Add one chunk to ``reply`` and return its events: the done chunk's usage first, then its text and tool calls."""
    events: list[StreamEvent] = []
    if chunk.done:
        reply.done = True
        reply.done_reason = chunk.done_reason
        reply.usage = _usage(chunk)
        events.append(UsageReported(reply.usage, final=False))
    message = chunk.message
    if message.content:
        reply.content += message.content
        events.append(TextDelta(message.content))
    for tool_call in message.tool_calls or ():
        call = {
            "id": str(uuid4()),
            "type": "function",
            "function": {"name": tool_call.function.name, "arguments": json.dumps(tool_call.function.arguments)},
        }
        reply.tool_calls.append(call)
        events.append(ToolCallReady(call))
    return events


def _usage(chunk: ChatResponse) -> MessageMetrics:
    timings = {
        "total_duration": chunk.total_duration,
        "load_duration": chunk.load_duration,
        "prompt_eval_duration": chunk.prompt_eval_duration,
        "eval_duration": chunk.eval_duration,
    }
    counts = TokenCounts(input_tokens=chunk.prompt_eval_count or 0, output_tokens=chunk.eval_count or 0)
    return MessageMetrics(
        tokens=counts.with_totals(),
        provider_metrics={name: value for name, value in timings.items() if value is not None} or None,
    )


def _stop_reason(reply: _OllamaReply) -> StopReason | None:
    if not reply.done:
        return None
    if reply.done_reason == "length":
        return "output_limit"
    # A server from before ``done_reason`` existed, or an Ollama-compatible one, ends a normal reply without it.
    if reply.done_reason not in {None, "stop"}:
        return "incomplete"
    return "tool_use" if reply.tool_calls else "end"


def build_provider(model_config: ModelConfig, runtime_paths: RuntimePaths) -> OllamaProvider:
    """Build the adapter for an ``ollama`` model config."""
    extra_kwargs = dict(model_config.extra_kwargs or {})
    if model_config.api_key is not None:
        extra_kwargs["api_key"] = model_config.api_key
    settings = settings_from_extra_kwargs(OllamaSettings, extra_kwargs, provider=model_config.provider)
    if settings.api_key is None and (env_api_key := os.getenv("OLLAMA_API_KEY")):
        settings = replace(settings, api_key=env_api_key)
    return OllamaProvider(
        identity=ModelIdentity(id=model_config.id, provider=_PROVIDER, provider_name=_PROVIDER),
        capabilities=Capabilities(structured_output="native", omitted_media=frozenset({"audio", "file", "video"})),
        settings=settings,
        host=model_config.host or get_ollama_host(runtime_paths=runtime_paths) or OLLAMA_HOST_DEFAULT,
    )


__all__ = ["OllamaProvider", "OllamaSettings", "build_provider"]
