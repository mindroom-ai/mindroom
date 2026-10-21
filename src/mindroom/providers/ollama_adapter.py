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
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import httpx
from pydantic import BaseModel

from mindroom.credentials.sync import get_ollama_host
from mindroom.llm.errors import InvalidRequest
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.usage import MessageMetrics
from mindroom.logging_config import get_logger
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT
from mindroom.providers.assistant_turn import finished_assistant_message, one_message_per_result, split_thinking
from mindroom.providers.errors import classify_provider_error, transport_error
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs
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
    """The ``extra_kwargs`` an Ollama model accepts, named as Agno's ``Ollama`` fields."""

    api_key: str | None = None
    timeout: float | None = None
    client_params: Mapping[str, Any] | None = None
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

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:
        """Append one stored message per tool result, as Ollama's wire format takes them."""
        return one_message_per_result(messages, results)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one chat request, streamed unless ``request.stream`` is False, and yield its events.

        A reply that arrives whole has its ``<think>`` reasoning split out, as Agno did for non-streamed replies.
        """
        messages = [await _format_message(message, self.identity.provider_name) for message in request.messages]
        chat_kwargs = self._chat_kwargs(request)
        from ollama import ResponseError  # noqa: PLC0415 - provider SDK loads on first request

        reply = _OllamaReply()
        started = time.monotonic()
        first_output_at: float | None = None
        try:
            if request.stream:
                chunks = await self._sdk_client().chat(
                    model=self.identity.id.strip(),
                    messages=messages,
                    stream=True,
                    **chat_kwargs,
                )
                async with aclosing(cast("AsyncGenerator[ChatResponse]", chunks)) as responses:
                    async for chunk in responses:
                        events = _read_chunk(reply, chunk)
                        if not events:
                            yield StreamProgress()
                        for event in events:
                            first_output_at = first_output_at or time.monotonic()
                            yield event
            else:
                response = await self._sdk_client().chat(
                    model=self.identity.id.strip(),
                    messages=messages,
                    **chat_kwargs,
                )
                first_output_at = time.monotonic()
                events = _read_chunk(reply, cast("ChatResponse", response))
                reply.reasoning, reply.content = split_thinking(reply.content)
                for event in events:
                    yield event
        except ResponseError as error:
            # The SDK reports an error the server streams after a 200 response with status -1.
            raise classify_provider_error(
                str(error),
                provider=self.identity.provider_name,
                status_code=error.status_code if error.status_code > 0 else None,
            ) from error
        except (httpx.TransportError, OSError) as error:
            raise transport_error(error, provider=self.identity.provider_name) from error
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
        """Return the chat parameters of one request, in the order Agno's Ollama built them."""
        settings = self.settings
        overrides = request.overrides
        options = dict(settings.options or {})
        if overrides.temperature is not None:
            options["temperature"] = overrides.temperature
        if overrides.max_output_tokens is not None:
            options["num_predict"] = overrides.max_output_tokens
        kwargs: dict[str, Any] = {
            key: value
            for key, value in (
                ("format", settings.format),
                ("options", options or None),
                ("keep_alive", settings.keep_alive),
            )
            if value is not None
        }
        tools = [*_format_tools(request.tools), *request.native_tools]
        if tools:
            kwargs["tools"] = tools
        kwargs.update(settings.request_params or {})
        if provider_tools_disabled():
            # Ollama has no tool_choice, so a decision request carries no tools at all.
            kwargs.pop("tools", None)
        response_format = request.response_format
        if isinstance(response_format, type) and issubclass(response_format, BaseModel) and "format" not in kwargs:
            kwargs["format"] = response_format.model_json_schema()
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


def _format_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Return Ollama tool definitions sorted by name; a tool without a description sends none, as Agno's did."""
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
    if message.role == "assistant" and message.tool_calls is not None:
        tool_calls = [
            {
                "id": call.get("id"),
                "type": "function",
                "function": {
                    "name": call["function"]["name"],
                    "arguments": _call_arguments(call["function"].get("arguments")),
                },
            }
            for call in message.tool_calls
            if "function" in call
        ]
        if tool_calls:
            formatted["tool_calls"] = tool_calls
    if message.role == "user":
        images = [data for image in message.images or () if (data := await _image_bytes(image, provider))]
        if images:
            formatted["images"] = images
        if unsupported := [
            kind
            for kind, media in (("audio", message.audio), ("file", message.files), ("video", message.videos))
            if media
        ]:
            logger.warning("ollama_media_unsupported", kinds=unsupported)
    return formatted


def _call_arguments(arguments: object) -> object:
    """Return stored tool-call arguments as the object Ollama expects; undecodable arguments become empty."""
    if not isinstance(arguments, str):
        return {} if arguments is None else arguments
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return {}


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
    events: list[StreamEvent] = []
    message = chunk.message
    if message is not None:
        if message.content:
            reply.content += message.content
            events.append(TextDelta(message.content))
        for tool_call in message.tool_calls or ():
            arguments = tool_call.function.arguments
            call = {
                "id": str(uuid4()),
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": None if arguments is None else json.dumps(arguments),
                },
            }
            reply.tool_calls.append(call)
            events.append(ToolCallReady(call))
    if chunk.done:
        reply.done = True
        reply.done_reason = chunk.done_reason
        reply.usage = _usage(chunk)
    return events


def _usage(chunk: ChatResponse) -> MessageMetrics:
    input_tokens = chunk.prompt_eval_count or 0
    output_tokens = chunk.eval_count or 0
    timings = {
        name: value
        for name, value in (
            ("total_duration", chunk.total_duration),
            ("load_duration", chunk.load_duration),
            ("prompt_eval_duration", chunk.prompt_eval_duration),
            ("eval_duration", chunk.eval_duration),
        )
        if value is not None
    }
    return MessageMetrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        provider_metrics=timings or None,
    )


def _stop_reason(reply: _OllamaReply) -> StopReason | None:
    if not reply.done:
        return None
    if reply.done_reason == "length":
        return "output_limit"
    if reply.done_reason != "stop":
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
