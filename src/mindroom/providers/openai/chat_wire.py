# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Chat Completions request bodies and response parsing for every OpenAI-compatible route.

Every route shapes its requests and reads its replies by the same rules, plus the few its API differs by, which the
route name selects. Functions here are pure: they build the SDK call from canonical messages and fold SDK chunks into
one finished turn.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import cache
from typing import TYPE_CHECKING, Any, Literal

from mindroom.llm.messages import tool_result_view, with_joined_system
from mindroom.llm.provider import (
    STRUCTURED_OUTPUT_TOOL,
    ReasoningDelta,
    StopReason,
    StructuredOutput,
    TextDelta,
    UsageReported,
    structured_output_tools,
)
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.assistant_turn import ThinkTagSplitter
from mindroom.providers.openai.media import chat_audio_part, chat_file_part, chat_image_part
from mindroom.providers.openai.schema import strict_response_schema
from mindroom.providers.openai.tool_ids import chat_call_id, reformat_tool_call_ids
from mindroom.providers.tool_policy import disable_tool_selection, disables_tools

if TYPE_CHECKING:
    from collections.abc import Sequence

    from openai.types.chat import ChatCompletionChunk
    from pydantic import BaseModel

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import StreamEvent, ToolChoice, ToolSpec
    from mindroom.providers.openai.settings import ChatSettings

__all__ = [
    "ChatStream",
    "ChatTurnParts",
    "ChatWire",
    "chat_request_kwargs",
    "chat_stop_reason",
    "deepseek_request_thinks",
    "function_definition",
    "sdk_call_kwargs",
]

# The two SDK endpoints the OpenAI family calls: ``chat.completions.create`` and ``responses.create``.
type _SDKEndpoint = Literal["chat", "responses"]

# Groq Compound runs hosted tools on its own, which a decision that must not run tools cannot allow.
_GROQ_COMPOUND_MODELS = frozenset({"groq/compound", "groq/compound-mini", "compound-beta", "compound-beta-mini"})
# Cerebras rejects parallel tool calls on this model.
_CEREBRAS_SERIAL_TOOL_MODEL = "llama-4-scout-17b-16e-instruct"
# Routes that stream usage in the last chunk unasked; Cerebras does not take the ``stream_options`` that ask for it.
_UNASKED_STREAM_USAGE_ROUTES = frozenset({"groq", "cerebras"})
_SDK_OPTION_KEYS = ("extra_headers", "extra_query", "extra_body", "timeout")
_CHAT_STOP_REASONS: dict[str, StopReason] = {
    "stop": "end",
    "length": "output_limit",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "content_filter",
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ChatWire:
    """How one route shapes Chat Completions requests and reads responses.

    The route selects the rules its API differs by, such as ``openai`` sending the system prompt as ``developer``,
    ``deepseek`` and ``openrouter`` replaying their reasoning fields, and ``groq`` streaming its usage in its own chunk
    extension.
    """

    route: str


@dataclass(slots=True, kw_only=True)
class ChatTurnParts:
    """One finished Chat Completions turn before it becomes the assistant message."""

    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    provider_data: dict[str, Any] = field(default_factory=dict)
    usage: MessageMetrics | None = None
    finish_reason: str | None = None


def chat_stop_reason(finish_reason: str | None) -> StopReason | None:
    """Map a Chat Completions ``finish_reason`` to the normalized stop vocabulary."""
    if finish_reason is None:
        return None
    return _CHAT_STOP_REASONS.get(finish_reason, "incomplete")


# --- Requests -------------------------------------------------------------------------------------------------


def _message_content(message: Message) -> str | list[Any] | None:
    """Return the content a Chat message carries; a tool result always has one, as the API requires."""
    return tool_result_view(message).content if message.role == "tool" else message.content


def _chat_message(message: Message, wire: ChatWire, role_map: Mapping[str, str], provider: str) -> dict[str, Any]:
    values: dict[str, Any] = {
        "role": role_map.get(message.role, message.role),
        "content": _message_content(message),
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls,
    }
    if wire.route == "deepseek":
        values["reasoning_content"] = message.reasoning_content
    formatted = {key: value for key, value in values.items() if value is not None}
    if (message.images or message.audio) and isinstance(message.content, str):
        formatted["content"] = [
            {"type": "text", "text": message.content},
            *(
                part
                for image in message.images or ()
                if (part := chat_image_part(image, provider=provider)) is not None
            ),
            *(part for audio in message.audio or () if (part := chat_audio_part(audio, provider=provider)) is not None),
        ]
    if message.tool_calls is not None and len(message.tool_calls) == 0:
        formatted["tool_calls"] = None
    if message.files is not None:
        content = formatted.get("content")
        parts: list[Any] = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        # Each file's part goes ahead of the text.
        for file in message.files:
            if (part := chat_file_part(file, provider=provider)) is not None:
                parts.insert(0, part)
        formatted["content"] = parts
    if message.content is None and message.role != "tool":
        formatted["content"] = ""
    reasoning_details = (message.provider_data or {}).get("reasoning_details") if message.role == "assistant" else None
    if wire.route == "openrouter" and reasoning_details:
        formatted["reasoning_details"] = reasoning_details
    return formatted


def _chat_messages(
    messages: Sequence[Message],
    wire: ChatWire,
    settings: ChatSettings,
    provider: str,
) -> list[dict[str, Any]]:
    role_map = settings.role_map or {"system": "developer" if wire.route == "openai" else "system"}
    canonical = reformat_tool_call_ids(with_joined_system(messages), "chat")
    return [_chat_message(message, wire, role_map, provider) for message in canonical]


def function_definition(tool: ToolSpec) -> dict[str, Any]:
    """Return the function definition OpenAI-family wires send: its set fields, in the order the API documents them."""
    fields = {
        "name": tool.name,
        "description": tool.description,
        "parameters": deepcopy(dict(tool.parameters)),
        "strict": tool.strict,
    }
    return {key: value for key, value in fields.items() if value is not None}


def _chat_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Return tool definitions sorted by name, the order every cached prompt prefix depends on."""
    formatted = [{"type": "function", "function": function_definition(tool)} for tool in tools]
    return sorted(formatted, key=lambda tool: tool["function"]["name"])


def _response_format(
    schema: type[BaseModel] | None,
    structured_output: StructuredOutput,
    *,
    strict: bool,
) -> dict[str, Any] | None:
    """Return the ``response_format`` that asks the route for ``schema``, or for JSON when it enforces no schema.

    A ``forced_tool`` route asks with a forced tool call instead (``chat_request_kwargs``).
    """
    if schema is None or structured_output == "forced_tool":
        return None
    if structured_output == "json_mode":
        return {"type": "json_object"}
    if structured_output == "native":
        json_schema = strict_response_schema(schema)
    else:
        # Cerebras advertises ``json_schema`` structured output, which needs every object of the schema closed.
        json_schema = schema.model_json_schema()
        _close_cerebras_objects(json_schema)
    return {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": json_schema, "strict": strict}}


def _close_cerebras_objects(schema: Any) -> None:  # noqa: ANN401 - any JSON schema node
    """Set ``additionalProperties: false`` on every object, as Cerebras JSON-schema output requires."""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
    children: list[Any] = []
    for key in ("properties", "$defs"):
        if isinstance(nested := schema.get(key), dict):
            children.extend(nested.values())
    if "items" in schema:
        children.append(schema["items"])
    for key in ("allOf", "anyOf", "oneOf"):
        if isinstance(variants := schema.get(key), list):
            children.extend(variants)
    for child in children:
        _close_cerebras_objects(child)


def deepseek_request_thinks(settings: ChatSettings) -> bool:
    """Return whether a DeepSeek request thinks.

    It thinks as an authored ``extra_body.thinking`` says, else as ``use_thinking`` does. The authored ``extra_body``,
    from ``request_params`` when it sets one, merges over the switch the route sends.
    """
    request_params = settings.request_params or {}
    authored = request_params.get("extra_body", settings.extra_body)
    thinking = authored.get("thinking") if isinstance(authored, Mapping) else None
    if isinstance(thinking, Mapping) and thinking.get("type") in {"enabled", "disabled"}:
        return thinking["type"] == "enabled"
    return settings.use_thinking


def _deepseek_thinking(params: dict[str, Any], settings: ChatSettings) -> None:
    """Send DeepSeek's thinking switch from ``use_thinking``.

    A request that does not think sends no reasoning effort.
    """
    if not deepseek_request_thinks(settings):
        params.pop("reasoning_effort", None)
    switch = {"type": "enabled" if settings.use_thinking else "disabled"}
    params["extra_body"] = {"thinking": switch, **(params.get("extra_body") or {})}


def _reject_native_search(params: Mapping[str, Any], model_id: str) -> None:
    """Fail a no-tools decision whose model or request runs hosted search or tools regardless of ``tool_choice``."""
    extra_body = params.get("extra_body")
    sources = [params, extra_body] if isinstance(extra_body, Mapping) else [params]
    model_ids = [model_id, *(str(source.get("model", "")) for source in sources)]
    if any(candidate.casefold() in _GROQ_COMPOUND_MODELS for candidate in model_ids) or any(
        source.get("compound_custom") is not None for source in sources
    ):
        msg = "Participation decisions cannot disable native Groq Compound tools"
        raise ValueError(msg)
    model_ids.extend(str(candidate) for source in sources for candidate in source.get("models") or [])
    if any(source.get("web_search_options") is not None for source in sources) or any(
        marker in candidate.casefold() for candidate in model_ids for marker in ("-search-api", "-search-preview")
    ):
        msg = "Participation decisions cannot disable native Chat Completions search"
        raise ValueError(msg)
    plugins = (
        extra_body.get("plugins", params.get("plugins")) if isinstance(extra_body, Mapping) else params.get("plugins")
    )
    if any("online" in candidate.casefold().split(":")[1:] for candidate in model_ids) or any(
        isinstance(plugin, Mapping) and plugin.get("id") == "web" and plugin.get("enabled") is not False
        for plugin in plugins or []
    ):
        msg = "Participation decisions cannot disable native OpenRouter search"
        raise ValueError(msg)


def _add_tools(
    params: dict[str, Any],
    tool_dicts: list[dict[str, Any]],
    *,
    model_id: str,
    settings: ChatSettings,
    wire: ChatWire,
) -> None:
    """Add the tools and their parallel-call setting; a decision's ``tool_choice`` is set last, for every tool list."""
    if not tool_dicts:
        return
    params["tools"] = tool_dicts
    if wire.route == "cerebras" and model_id == _CEREBRAS_SERIAL_TOOL_MODEL:
        params["parallel_tool_calls"] = False
    elif settings.parallel_tool_calls is not None:
        params["parallel_tool_calls"] = settings.parallel_tool_calls


def _add_route_params(
    params: dict[str, Any],
    *,
    model_id: str,
    settings: ChatSettings,
    wire: ChatWire,
    tool_choice: ToolChoice | None,
) -> None:
    """Apply authored request parameters, then the route's own rules on top of them."""
    params.update(settings.request_params or {})
    if wire.route == "openrouter" and settings.models:
        params["extra_body"] = {**(params.get("extra_body") or {}), "models": settings.models}
    if wire.route == "deepseek":
        _deepseek_thinking(params, settings)
    if wire.route == "kimi" and settings.prompt_cache_key:
        params.setdefault("prompt_cache_key", settings.prompt_cache_key)
    if disables_tools(tool_choice):
        _reject_native_search(params, model_id)


@cache
def _sdk_body_fields(endpoint: _SDKEndpoint) -> frozenset[str]:
    """Return the body fields the SDK's ``create`` takes as keywords."""
    if endpoint == "chat":
        from openai.resources.chat.completions import (  # noqa: PLC0415 - keep the SDK out of module import time
            AsyncCompletions,
        )

        create = AsyncCompletions.create
    else:
        from openai.resources.responses import AsyncResponses  # noqa: PLC0415 - keep the SDK out of module import time

        create = AsyncResponses.create
    return frozenset(inspect.signature(create).parameters) - {"self", *_SDK_OPTION_KEYS}


def sdk_call_kwargs(params: Mapping[str, Any], endpoint: _SDKEndpoint) -> dict[str, Any]:
    """Return the SDK ``create`` arguments for one request's body fields and SDK options.

    The SDK serializes its keywords in its own order and appends ``extra_body`` after them, so every field the SDK
    names travels as its keyword and only the fields it does not name travel in ``extra_body``.
    Authored ``extra_body`` keys still win, because the SDK merges them over the fields it serializes.
    """
    named = _sdk_body_fields(endpoint)
    kwargs = {key: value for key, value in params.items() if key in named or key in _SDK_OPTION_KEYS}
    unnamed = {key: value for key, value in params.items() if key not in kwargs}
    if body := {**unnamed, **(kwargs.pop("extra_body", None) or {})}:
        kwargs["extra_body"] = body
    return kwargs


def chat_request_kwargs(
    *,
    model_id: str,
    messages: Sequence[Message],
    tools: Sequence[ToolSpec],
    tool_choice: ToolChoice | None,
    output_schema: type[BaseModel] | None,
    structured_output: StructuredOutput,
    settings: ChatSettings,
    wire: ChatWire,
    provider: str,
) -> dict[str, Any]:
    """Return the ``chat.completions.create`` keyword arguments for one streamed request.

    Fields the SDK names are its keywords and the rest travel in ``extra_body`` (``sdk_call_kwargs``). ``provider``
    names the adapter in the error for an attachment file that cannot be read.
    """
    response_format = _response_format(output_schema, structured_output, strict=settings.strict_output)
    fields = {**settings.request_fields(), "response_format": response_format}
    params = {key: value for key, value in fields.items() if value is not None}
    output_tool = structured_output_tools(structured_output, output_schema)
    _add_tools(
        params,
        _chat_tools((*tools, *output_tool)),
        model_id=model_id,
        settings=settings,
        wire=wire,
    )
    if output_tool:
        params["tool_choice"] = {"type": "function", "function": {"name": STRUCTURED_OUTPUT_TOOL}}
    _add_route_params(params, model_id=model_id, settings=settings, wire=wire, tool_choice=tool_choice)
    params = {
        "model": model_id,
        "messages": _chat_messages(messages, wire, settings, provider),
        **disable_tool_selection(params, tool_choice),
    }
    params["stream"] = True
    if wire.route not in _UNASKED_STREAM_USAGE_ROUTES:
        params["stream_options"] = {"include_usage": True}
    return sdk_call_kwargs(params, "chat")


# --- Replies --------------------------------------------------------------------------------------------------


def _extra(model: BaseModel | None) -> dict[str, Any]:
    return dict(model.model_extra or {}) if model is not None else {}


def _detail(usage: Mapping[str, Any], section: str, name: str) -> int:
    details = usage.get(section)
    value = details.get(name) if isinstance(details, Mapping) else None
    return value if isinstance(value, int) else 0


def _usage_metrics(usage: Mapping[str, Any]) -> MessageMetrics:
    counts = TokenCounts(
        input_tokens=usage.get("prompt_tokens") or 0,
        output_tokens=usage.get("completion_tokens") or 0,
        cache_read_tokens=_detail(usage, "prompt_tokens_details", "cached_tokens"),
        cache_write_tokens=_detail(usage, "prompt_tokens_details", "cache_write_tokens"),
        reasoning_tokens=_detail(usage, "completion_tokens_details", "reasoning_tokens"),
        audio_input_tokens=_detail(usage, "prompt_tokens_details", "audio_tokens"),
        audio_output_tokens=_detail(usage, "completion_tokens_details", "audio_tokens"),
    ).with_totals()
    cost = usage.get("cost")
    # ``with_totals`` derives the audio total; the token total is the one the provider reports.
    return MessageMetrics(
        tokens=replace(counts, total_tokens=usage.get("total_tokens") or 0),
        cost=cost if isinstance(cost, int | float) else None,
    )


def _chunk_usage(chunk: ChatCompletionChunk, wire: ChatWire) -> Mapping[str, Any] | None:
    """Return the usage a chunk reports; Groq streams its usage in its own chunk extension."""
    if wire.route == "groq":
        groq = _extra(chunk).get("x_groq")
        usage = groq.get("usage") if isinstance(groq, Mapping) else None
        return usage if isinstance(usage, Mapping) else None
    return None if chunk.usage is None else chunk.usage.model_dump()


def _merge_provider_data(target: dict[str, Any], update: Mapping[str, Any]) -> None:
    """Merge one delta's provider data: lists extend, everything else is replaced."""
    for key, value in update.items():
        existing = target.get(key)
        if isinstance(existing, list) and isinstance(value, list):
            existing.extend(value)
        else:
            target[key] = value


def _completed_tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Store each call under the id its later requests send, so its result and approval receipt name that id.

    Requests rewrite an id outside the Chat rule, so a call whose server sent such an id, or none, gets a new id that
    fits the rule.
    """
    for call in calls:
        call["id"] = chat_call_id(call.get("id"))
    return calls


def _reply_metadata(reply: ChatCompletionChunk) -> dict[str, Any]:
    """Return the ``id``, ``system_fingerprint``, and unknown fields a chunk carried, for stored provider data."""
    data: dict[str, Any] = {}
    if reply.id:
        data["id"] = reply.id
    if reply.system_fingerprint:
        data["system_fingerprint"] = reply.system_fingerprint
    if extra := _extra(reply):
        data["model_extra"] = extra
    return data


def _reasoning_field(fields: Mapping[str, Any]) -> object:
    """Return the reasoning text servers send as ``reasoning_content`` (DeepSeek) or ``reasoning``."""
    reasoning = fields.get("reasoning_content")
    return fields.get("reasoning") if reasoning is None else reasoning


@dataclass(slots=True)
class ChatStream:
    """Fold streamed Chat Completions chunks into deltas and one finished turn.

    Reasoning a model writes inline as a leading ``<think>`` block also moves out of the reply text
    (``ThinkTagSplitter``), as servers without a reasoning parser send it.
    """

    wire: ChatWire
    parts: ChatTurnParts = field(default_factory=ChatTurnParts)
    _content: list[str] = field(default_factory=list)
    _reasoning: list[str] = field(default_factory=list)
    _calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    _think: ThinkTagSplitter = field(default_factory=ThinkTagSplitter)

    def add(self, chunk: ChatCompletionChunk) -> list[StreamEvent]:
        """Fold one chunk and return the events it carried: a partial report of usage it carried, then its deltas.

        The usage comes first, so a reply stopped while the chunk's deltas are delivered still settles it.
        """
        events = self._add_usage(chunk)
        if chunk.choices:
            choice = chunk.choices[0]
            if choice.finish_reason is not None:
                self.parts.finish_reason = choice.finish_reason
            if (delta := choice.delta) is not None:
                events.extend(self._add_content(chunk, delta.content))
                for position, call in enumerate(delta.tool_calls or ()):
                    self._add_call_delta(call, position)
                events.extend(self._add_reasoning(_extra(delta)))
        return events

    def _add_content(self, chunk: ChatCompletionChunk, content: str | None) -> list[StreamEvent]:
        if content is None:
            return []
        _merge_provider_data(self.parts.provider_data, _reply_metadata(chunk))
        return self._add_text(self._think.add(content))

    def _add_text(self, deltas: list[TextDelta | ReasoningDelta]) -> list[StreamEvent]:
        """Record the reply text and inline reasoning the think-tag splitter released."""
        for delta in deltas:
            (self._content if isinstance(delta, TextDelta) else self._reasoning).append(delta.text)
        return list(deltas)

    def _add_reasoning(self, fields: Mapping[str, Any]) -> list[StreamEvent]:
        if self.wire.route == "openrouter" and (details := fields.get("reasoning_details")):
            _merge_provider_data(self.parts.provider_data, {"reasoning_details": list(details)})
        reasoning = _reasoning_field(fields)
        if not isinstance(reasoning, str):
            return []
        self._reasoning.append(reasoning)
        return [ReasoningDelta(reasoning)] if reasoning else []

    def _add_call_delta(self, call: Any, position: int) -> None:  # noqa: ANN401 - SDK tool-call delta model
        """Fold one call delta into the call at its index; a server that sends no index places it by its position."""
        function = call.function
        name = function.name if function is not None else None
        arguments = function.arguments if function is not None else None
        index = call.index if call.index is not None else position
        entry = self._calls.setdefault(index, {"id": None, "type": None, "function": {"name": "", "arguments": ""}})
        if name:
            entry["function"]["name"] = name
        if arguments:
            entry["function"]["arguments"] += arguments
        if call.id:
            entry["id"] = call.id
        if call.type:
            entry["type"] = call.type

    def _add_usage(self, chunk: ChatCompletionChunk) -> list[StreamEvent]:
        if (usage := _chunk_usage(chunk, self.wire)) is None:
            return []
        metrics = _usage_metrics(usage)
        self.parts.usage = metrics if self.parts.usage is None else self.parts.usage + metrics
        return [UsageReported(self.parts.usage, final=False)]

    def finish(self) -> list[StreamEvent]:
        """End the consumed stream and return the text it held back; ``parts`` then holds the finished turn."""
        events = self._add_text(self._think.finish())
        self.parts.content = "".join(self._content) or None
        self.parts.reasoning = "".join(self._reasoning) or None
        calls = [self._calls[index] for index in sorted(self._calls)]
        self.parts.tool_calls = _completed_tool_calls(calls)
        return events
