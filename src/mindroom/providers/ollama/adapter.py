# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Ollama provider adapter over the ollama SDK's native ``/api/chat`` endpoint.

The native endpoint keeps Ollama's ``options`` and ``keep_alive`` settings.
The provider registry imports this module, and with it the SDK, when it builds an Ollama model.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast
from uuid import uuid4

from ollama import AsyncClient

from mindroom.credentials.sync import get_ollama_api_key, get_ollama_host
from mindroom.llm.errors import InvalidRequest
from mindroom.llm.messages import tool_result_view, with_joined_system
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
from mindroom.providers.assistant_turn import ThinkTagSplitter, finished_assistant_message, output_started
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs, settings_params
from mindroom.providers.ollama.errors import provider_error
from mindroom.providers.ollama.settings import OllamaSettings
from mindroom.providers.remote_media import download_media
from mindroom.providers.tool_policy import disables_tools

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence

    from ollama import ChatResponse

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.media import Image
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest, ReasoningDelta, StopReason, StreamEvent, ToolSpec

logger = get_logger(__name__)

_PROVIDER = "Ollama"
# Remote images are downloaded up to this size; a larger one is left to the media fallback's note.
_IMAGE_LIMIT_BYTES = 20 * 1024 * 1024


@dataclass(slots=True)
class _OllamaReply:
    """What one streamed reply produced so far; a model's inline ``<think>`` block becomes its reasoning."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: MessageMetrics | None = None
    done_reason: str | None = None
    done: bool = False
    think: ThinkTagSplitter = field(default_factory=ThinkTagSplitter)

    def add_text(self, deltas: list[TextDelta | ReasoningDelta]) -> list[TextDelta | ReasoningDelta]:
        """Record the reply text and inline reasoning the think-tag splitter released, and return them."""
        for delta in deltas:
            if isinstance(delta, TextDelta):
                self.content += delta.text
            else:
                self.reasoning += delta.text
        return deltas


@dataclass(slots=True, kw_only=True)
class OllamaProvider:
    """One Ollama model; the SDK client is built on the first request and reused."""

    identity: ModelIdentity
    capabilities: Capabilities
    settings: OllamaSettings
    _client: AsyncClient | None = field(default=None, init=False, repr=False)
    # Ollama keeps no history of its own, and its replayed history is sized canonically.
    native_history_route: ClassVar[None] = None
    replay_estimator: ClassVar[None] = None

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one streamed chat request and yield its events."""
        messages = [
            await _format_message(message, self.identity.provider_name)
            for message in with_joined_system(request.messages)
        ]
        model = self.identity.id.strip()
        chat_kwargs = self._chat_kwargs(request)
        reply = _OllamaReply()
        started = time.monotonic()
        first_output_at: float | None = None
        try:
            client = self._sdk_client()
            chunks = await client.chat(model=model, messages=messages, stream=True, **chat_kwargs)
            async with aclosing(cast("AsyncGenerator[ChatResponse]", chunks)) as responses:
                async for chunk in responses:
                    for event in _read_chunk(reply, chunk) or [StreamProgress()]:
                        first_output_at = output_started(first_output_at, event)
                        yield event
        except Exception as error:
            # Any exception that is not a provider failure propagates as it is.
            if (failure := provider_error(error, provider=self.identity.provider_name)) is None:
                raise
            raise failure from error
        if not reply.done:
            # The stream ended before its done chunk, so the turn did not finish.
            return
        message = finished_assistant_message(
            self.identity,
            content=reply.content or None,
            reasoning=reply.reasoning or None,
            tool_calls=reply.tool_calls,
            usage=reply.usage,
            started=started,
            first_output_at=first_output_at,
        )
        if reply.usage is not None:
            yield UsageReported(message.metrics)
        yield TurnCompleted(message=message, stop_reason=_stop_reason(reply))

    def _chat_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        """Return the chat parameters of one request; authored ``request_params`` override the settings and tools.

        A request's temperature override, such as a thread summary's, replaces an authored ``options.temperature``.
        """
        settings = self.settings
        options = dict(settings.options or {})
        if request.overrides.temperature is not None:
            options["temperature"] = request.overrides.temperature
        authored = {"format": settings.format, "options": options or None, "keep_alive": settings.keep_alive}
        kwargs: dict[str, Any] = {key: value for key, value in authored.items() if value is not None}
        if tools := _format_tools(request.tools):
            kwargs["tools"] = tools
        kwargs.update(settings.request_params or {})
        if disables_tools(request.tool_choice):
            # Ollama has no tool_choice, so a request that allows no tool use carries no tools at all.
            kwargs.pop("tools", None)
        if request.output_schema is not None and "format" not in kwargs:
            kwargs["format"] = request.output_schema.model_json_schema()
        return kwargs

    def _sdk_client(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(**self._client_params())
        return self._client

    def _client_params(self) -> dict[str, Any]:
        settings = self.settings
        params: dict[str, Any] = {"host": self.identity.endpoint}
        if settings.timeout is not None:
            params["timeout"] = settings.timeout
        if settings.api_key:
            params["headers"] = {"authorization": f"Bearer {settings.api_key}"}
        return {**params, **(settings.client_params or {})}


def _format_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Return Ollama tool definitions sorted by name; a tool without a description sends none.

    Ollama reads only a parameter's type, items, description, and enum, so a parameter typed by an ``anyOf``, such as
    ``background_after``'s number or null, goes with those types as a type list (``_typed_property``).
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                **({} if tool.description is None else {"description": tool.description}),
                "parameters": _with_typed_properties(tool.parameters),
            },
        }
        for tool in sorted(tools, key=lambda tool: tool.name)
    ]


def _with_typed_properties(parameters: Mapping[str, Any]) -> dict[str, Any]:
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        return dict(parameters)
    return {**parameters, "properties": {name: _typed_property(value) for name, value in properties.items()}}


def _typed_property(schema: Any) -> Any:  # noqa: ANN401 - any JSON schema node
    """Return a property whose ``anyOf`` alternatives each name a type as one property with their types as a list.

    The first alternative that has ``items`` or ``enum`` gives them, which Ollama also reads; anything else stays as
    it is.
    """
    alternatives = schema.get("anyOf") if isinstance(schema, dict) and "type" not in schema else None
    if not isinstance(alternatives, list) or not all(
        isinstance(item, dict) and isinstance(item.get("type"), str) for item in alternatives
    ):
        return schema
    typed: dict[str, Any] = {"type": [item["type"] for item in alternatives]}
    for key in ("items", "enum"):
        if (source := next((item for item in alternatives if key in item), None)) is not None:
            typed[key] = source[key]
    return {**typed, **{key: value for key, value in schema.items() if key != "anyOf"}}


async def _format_message(message: Message, provider: str) -> dict[str, Any]:
    """Return one chat message; images are read off the event loop or downloaded.

    A tool result names the tool that answered, as the API documents. An image that cannot be read raises
    ``InvalidRequest``, so the media fallback replaces it with its note.
    """
    if message.role == "tool":
        result = tool_result_view(message)
        return {"role": "tool", "content": result.content, "tool_name": result.name}
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
    """Add one chunk to ``reply`` and return its events: the done chunk's usage first, then its text and tool calls.

    The done chunk ends the reply, so it also releases the text the think-tag splitter held back.
    """
    events: list[StreamEvent] = []
    if chunk.done:
        reply.done = True
        reply.done_reason = chunk.done_reason
        reply.usage = _usage(chunk)
        events.append(UsageReported(reply.usage, final=False))
    message = chunk.message
    if message.content:
        events.extend(reply.add_text(reply.think.add(message.content)))
    if chunk.done:
        events.extend(reply.add_text(reply.think.finish()))
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
    """Build the adapter for an ``ollama`` model config; without a key of its own it sends ``OLLAMA_API_KEY``."""
    api_key = model_config.configured_api_key() or get_ollama_api_key(runtime_paths)
    extra_kwargs = {**(model_config.extra_kwargs or {}), "api_key": api_key}
    settings = settings_from_extra_kwargs(OllamaSettings, extra_kwargs, provider=model_config.provider)
    # ``client_params`` wins over every client option, so its ``host`` is the server requests reach.
    host = str(
        (settings.client_params or {}).get("host")
        or model_config.host
        or get_ollama_host(runtime_paths=runtime_paths)
        or OLLAMA_HOST_DEFAULT,
    )
    return OllamaProvider(
        identity=ModelIdentity(
            id=model_config.id,
            provider=_PROVIDER,
            provider_name=_PROVIDER,
            endpoint=host.rstrip("/"),
        ),
        capabilities=Capabilities(structured_output="native", omitted_media=frozenset({"audio", "file", "video"})),
        settings=settings,
    )


__all__ = ["OllamaProvider", "build_provider"]
