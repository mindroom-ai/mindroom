# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Ollama provider adapter over the ollama SDK's native ``/api/chat`` endpoint.

The native endpoint keeps Ollama's ``options`` and ``keep_alive`` settings.
The provider registry imports this module, and with it the SDK, when it builds an Ollama model.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, cast
from uuid import uuid4

from ollama import AsyncClient

from mindroom.credentials.sync import get_ollama_api_key, get_ollama_host
from mindroom.llm.messages import tool_result_view, with_joined_system
from mindroom.llm.provider import Capabilities, ModelIdentity, ReasoningDelta, TextDelta, ToolCallReady, UsageReported
from mindroom.llm.tool_search import call_arguments
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.assistant_turn import ThinkTagSplitter, TurnTimer, finished_turn
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs, settings_params
from mindroom.providers.identity import model_identity
from mindroom.providers.media_files import read_media_bytes
from mindroom.providers.ollama.errors import provider_error
from mindroom.providers.ollama.settings import OllamaSettings, ollama_client_options
from mindroom.providers.tool_policy import disables_tools
from mindroom.token_counting.families import counter_for_model

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence

    from ollama import ChatResponse

    from mindroom.config.models import ModelConfig
    from mindroom.constants import RuntimePaths
    from mindroom.llm.media import Image
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest, StopReason, StreamEvent, TokenCounter, ToolSpec


@dataclass(slots=True)
class _OllamaReply:
    """What one streamed reply produced so far; a model's ``thinking`` and its inline ``<think>`` block are reasoning."""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: MessageMetrics | None = None
    done_reason: str | None = None
    done: bool = False
    think: ThinkTagSplitter = field(default_factory=ThinkTagSplitter)

    def add_text(self, deltas: list[TextDelta | ReasoningDelta]) -> list[TextDelta | ReasoningDelta]:
        """Record the reply text and reasoning of ``deltas``, and return them."""
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

    @property
    def model_params(self) -> dict[str, Any]:
        """The route's JSON-safe, non-secret settings."""
        return settings_params(self.settings)

    @property
    def token_counter(self) -> TokenCounter:
        """Ollama serves models whose tokenizer does not run locally, so the route estimates."""
        return counter_for_model(self.identity)

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Send one streamed chat request and yield its events."""
        messages = [
            await _format_message(message, self.identity.provider) for message in with_joined_system(request.messages)
        ]
        # Authored ``request_params`` merge over MindRoom's fields, as on the other routes; the adapter needs streaming.
        call: dict[str, Any] = {
            "model": self.identity.id.strip(),
            "messages": messages,
            **self._chat_kwargs(request),
            "stream": True,
        }
        reply = _OllamaReply()
        timer = TurnTimer()
        try:
            client = self._sdk_client()
            await request.on_send(call)
            chunks = await client.chat(**call)
            async with aclosing(cast("AsyncGenerator[ChatResponse]", chunks)) as responses:
                async for chunk in responses:
                    for event in timer.chunk(_read_chunk(reply, chunk)):
                        yield event
        except Exception as error:
            # Any exception that is not a provider failure propagates as it is.
            if (failure := provider_error(error, provider=self.identity.provider)) is None:
                raise
            raise failure from error
        if not reply.done:
            # The stream ended before its done chunk, so the turn did not finish.
            return
        for event in finished_turn(
            self.identity,
            timer,
            content=reply.content or None,
            reasoning=reply.reasoning or None,
            tool_calls=reply.tool_calls,
            usage=reply.usage,
            stop_reason=_stop_reason(reply),
        ):
            yield event

    def _chat_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        """Return the chat parameters of one request; authored ``request_params`` override the settings and tools.

        A request's temperature override, such as a thread summary's, replaces an authored ``options.temperature``.
        """
        settings = self.settings
        options = dict(settings.options or {})
        if request.overrides.temperature is not None:
            options["temperature"] = request.overrides.temperature
        authored = {
            "format": settings.format,
            "options": options or None,
            "keep_alive": settings.keep_alive,
            "think": settings.think,
        }
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
            self._client = AsyncClient(**self.client_options())
        return self._client

    def client_options(self) -> dict[str, Any]:
        """Return the Ollama SDK client options every request of this model goes out with, as doctor checks them."""
        settings = self.settings
        params = ollama_client_options(self.identity.endpoint, settings.api_key)
        if settings.timeout is not None:
            params["timeout"] = settings.timeout
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
    """Return one chat message; images are read off the event loop, and the pipeline downloaded those at a URL.

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
    if image.filepath is None:
        return image.content
    read = partial(read_media_bytes, image.filepath, media_id=image.id, kind="image", provider=provider)
    return await asyncio.to_thread(read)


def _read_chunk(reply: _OllamaReply, chunk: ChatResponse) -> list[StreamEvent]:
    """Add one chunk to ``reply`` and return its events: the done chunk's usage first, then reasoning, text, and calls.

    The done chunk ends the reply, so it also releases the text the think-tag splitter held back.
    """
    events: list[StreamEvent] = []
    if chunk.done:
        reply.done = True
        reply.done_reason = chunk.done_reason
        reply.usage = _usage(chunk)
        events.append(UsageReported(reply.usage, final=False))
    message = chunk.message
    if message.thinking:
        events.extend(reply.add_text([ReasoningDelta(message.thinking)]))
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
    counts = TokenCounts(input_tokens=chunk.prompt_eval_count or 0, output_tokens=chunk.eval_count or 0)
    return MessageMetrics(tokens=counts.with_totals())


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
    host = get_ollama_host(
        runtime_paths=runtime_paths,
        configured_host=(settings.client_params or {}).get("host") or model_config.host,
        saved_first=False,
    )
    return OllamaProvider(
        identity=model_identity(model_config, endpoint=host.rstrip("/")),
        capabilities=Capabilities(structured_output="native", omitted_media=frozenset({"audio", "file", "video"})),
        settings=settings,
    )


__all__ = ["OllamaProvider", "build_provider"]
