# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Chat Completions request bodies and response parsing for every OpenAI-compatible route.

Three dialects reproduce the bytes each route sent through Agno: ``openai`` (OpenAI, Azure, OpenRouter,
DeepSeek, Z.ai, llama.cpp, Kimi), ``groq``, and ``cerebras``. Functions here are pure: they build the SDK
call from canonical messages and fold SDK chunks or completions into one finished turn.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel

from mindroom.llm.messages import split_thinking
from mindroom.llm.provider import ReasoningDelta, StopReason, TextDelta, UsageReported
from mindroom.llm.usage import MessageMetrics
from mindroom.providers.openai.errors import error_body_error
from mindroom.providers.openai.media import chat_audio_part, chat_file_part, chat_image_part
from mindroom.providers.openai.schema import strict_response_schema
from mindroom.providers.openai.tool_ids import reformat_tool_call_ids
from mindroom.providers.tool_policy import disable_tool_selection, provider_tools_disabled

if TYPE_CHECKING:
    from collections.abc import Sequence

    from openai.types import CompletionUsage
    from openai.types.chat import ChatCompletion, ChatCompletionChunk

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import StreamEvent, ToolChoice, ToolSpec
    from mindroom.providers.openai.settings import ChatSettings

__all__ = [
    "ChatDialect",
    "ChatStream",
    "ChatTurnParts",
    "ChatWire",
    "chat_request_kwargs",
    "chat_stop_reason",
    "function_definition",
    "parse_chat_completion",
    "sdk_call_kwargs",
]

type ChatDialect = Literal["openai", "groq", "cerebras"]
# The two SDK endpoints the OpenAI family calls: ``chat.completions.create`` and ``responses.create``.
type _SDKEndpoint = Literal["chat", "responses"]

# Groq Compound runs hosted tools on its own, which a decision that must not run tools cannot allow.
_GROQ_COMPOUND_MODELS = frozenset({"groq/compound", "groq/compound-mini", "compound-beta", "compound-beta-mini"})
# Cerebras rejects parallel tool calls on this model.
_CEREBRAS_SERIAL_TOOL_MODEL = "llama-4-scout-17b-16e-instruct"
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

    Besides its dialect, the route selects rules of its own, such as ``openai`` sending the system prompt as
    ``developer`` and ``deepseek`` and ``openrouter`` replaying their reasoning fields.
    """

    route: str
    dialect: ChatDialect


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


def _with_file_parts(content: str | list[Any] | None, message: Message) -> list[Any]:
    """Return ``content`` as a part list with each file's part inserted ahead of the text."""
    parts: list[Any] = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
    for file in message.files or ():
        if (part := chat_file_part(file)) is not None:
            parts.insert(0, part)
    return parts


def _openai_message(message: Message, wire: ChatWire, role_map: Mapping[str, str]) -> dict[str, Any]:
    values: dict[str, Any] = {
        "role": role_map.get(message.role, message.role),
        "content": message.content,
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
            *(part for image in message.images or () if (part := chat_image_part(image)) is not None),
            *(part for audio in message.audio or () if (part := chat_audio_part(audio)) is not None),
        ]
    if message.tool_calls is not None and len(message.tool_calls) == 0:
        formatted["tool_calls"] = None
    if message.files is not None:
        formatted["content"] = _with_file_parts(formatted.get("content"), message)
    if message.content is None:
        formatted["content"] = ""
    reasoning_details = (message.provider_data or {}).get("reasoning_details") if message.role == "assistant" else None
    if wire.route == "openrouter" and reasoning_details:
        formatted["reasoning_details"] = reasoning_details
    return formatted


def _groq_message(message: Message) -> dict[str, Any]:
    values = {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls,
    }
    formatted = {key: value for key, value in values.items() if value is not None}
    if message.images and isinstance(message.content, str):
        formatted["content"] = [
            {"type": "text", "text": message.content},
            *(part for image in message.images if (part := chat_image_part(image)) is not None),
        ]
    return formatted


def _cerebras_message(message: Message) -> dict[str, Any]:
    content = message.content if message.role == "tool" or message.content is not None else ""
    if message.role == "tool" and message.tool_call_id:
        return {
            key: value
            for key, value in (("role", "tool"), ("tool_call_id", message.tool_call_id), ("content", content))
            if value is not None
        }
    formatted: dict[str, Any] = {"role": message.role, "content": content}
    if message.name:
        formatted["name"] = message.name
    if message.tool_calls:
        formatted["tool_calls"] = [
            {
                "id": call["id"],
                "type": call["type"],
                "function": {
                    "name": call["function"]["name"],
                    "arguments": json.dumps(call["function"]["arguments"])
                    if isinstance(call["function"]["arguments"], dict | list)
                    else call["function"]["arguments"],
                },
            }
            for call in message.tool_calls
        ]
    return {key: value for key, value in formatted.items() if value is not None}


def _chat_messages(messages: Sequence[Message], wire: ChatWire, settings: ChatSettings) -> list[dict[str, Any]]:
    if wire.dialect == "groq":
        return [_groq_message(message) for message in messages]
    if wire.dialect == "cerebras":
        return [_cerebras_message(message) for message in messages]
    role_map = settings.role_map or {
        "system": "developer" if wire.route == "openai" else "system",
        "model": "assistant",
    }
    canonical = reformat_tool_call_ids(messages, "chat")
    return [_openai_message(message, wire, role_map) for message in canonical]


def function_definition(tool: ToolSpec) -> dict[str, Any]:
    """Return the function definition OpenAI-family wires send, as Agno's ``Function.to_dict()``: same keys, same order."""
    fields = {
        "name": tool.name,
        "description": tool.description,
        "parameters": deepcopy(dict(tool.parameters)),
        "strict": tool.strict,
        # TODO(simplify): Tool definitions still send Agno's approval flags, which no OpenAI-family API reads. Send
        # only the name, description, parameters, and strict. Provider request bodies and prompt caches (#3075).
        "requires_confirmation": tool.requires_confirmation,
        "external_execution": tool.external_execution,
        "approval_type": tool.approval_type,
    }
    return {key: value for key, value in fields.items() if value is not None}


def _chat_tools(tools: Sequence[ToolSpec], wire: ChatWire) -> list[dict[str, Any]]:
    """Return tool definitions sorted by name, the order every cached prompt prefix depends on."""
    formatted: list[dict[str, Any]]
    if wire.dialect == "cerebras":
        # Cerebras takes the function shape without Agno's approval flags or strict; None is left out.
        formatted = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    **({} if tool.description is None else {"description": tool.description}),
                    "parameters": deepcopy(dict(tool.parameters)),
                },
            }
            for tool in tools
        ]
    else:
        formatted = [{"type": "function", "function": function_definition(tool)} for tool in tools]
    return sorted(formatted, key=lambda tool: tool["function"]["name"])


def _json_schema_format(response_format: type[BaseModel] | Mapping[str, Any], *, strict: bool) -> dict[str, Any]:
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": response_format.__name__,
                "schema": strict_response_schema(response_format),
                "strict": strict,
            },
        }
    return dict(cast("Mapping[str, Any]", response_format))


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


def _deepseek_thinking(params: dict[str, Any], settings: ChatSettings, model_id: str) -> None:
    """Enable thinking for thinking-capable DeepSeek ids unless the config turns it off."""
    thinking = settings.use_thinking if settings.use_thinking is not None else model_id != "deepseek-chat"
    if thinking:
        params["extra_body"] = {"thinking": {"type": "enabled"}, **(params.get("extra_body") or {})}
        return
    params.pop("reasoning_effort", None)
    if settings.use_thinking is False:
        params["extra_body"] = {"thinking": {"type": "disabled"}, **(params.get("extra_body") or {})}


def _reject_native_search(params: Mapping[str, Any], model_id: str, wire: ChatWire) -> None:
    """Fail a no-tools decision on routes whose hosted search runs regardless of ``tool_choice``."""
    extra_body = params.get("extra_body")
    sources = [params, extra_body] if isinstance(extra_body, Mapping) else [params]
    model_ids = [model_id, *(str(source.get("model", "")) for source in sources)]
    if wire.dialect == "groq":
        if any(candidate.casefold() in _GROQ_COMPOUND_MODELS for candidate in model_ids) or any(
            source.get("compound_custom") is not None for source in sources
        ):
            msg = "Participation decisions cannot disable native Groq Compound tools"
            raise ValueError(msg)
        return
    if wire.dialect != "openai":
        return
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


def _dialect_params(
    settings: ChatSettings,
    wire: ChatWire,
    response_format: type[BaseModel] | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return the route dialect's request parameters that are set, structured output included."""
    prepared = None if response_format is None else _json_schema_format(response_format, strict=settings.strict_output)
    if prepared is not None and wire.dialect == "cerebras":
        # Cerebras advertises ``json_schema`` structured output, which needs every object of the schema closed.
        prepared = deepcopy(prepared)
        prepared["json_schema"].setdefault("strict", settings.strict_output)
        _close_cerebras_objects(prepared["json_schema"]["schema"])
    if wire.dialect == "groq":
        fields = settings.groq_request_fields()
    elif wire.dialect == "cerebras":
        fields = settings.cerebras_request_fields()
    else:
        fields = settings.openai_request_fields()
    return {key: value for key, value in {**fields, "response_format": prepared}.items() if value is not None}


def _add_tools(
    params: dict[str, Any],
    tool_dicts: list[dict[str, Any]],
    tool_choice: ToolChoice | None,
    *,
    model_id: str,
    settings: ChatSettings,
    wire: ChatWire,
) -> None:
    if not tool_dicts and wire.dialect != "groq":
        return
    # Groq always received the tool list, empty included.
    params["tools"] = tool_dicts
    if wire.dialect != "cerebras":
        if tool_choice is not None:
            params["tool_choice"] = tool_choice
    elif model_id == _CEREBRAS_SERIAL_TOOL_MODEL:
        params["parallel_tool_calls"] = False
    elif settings.parallel_tool_calls is not None:
        params["parallel_tool_calls"] = settings.parallel_tool_calls


def _add_route_params(params: dict[str, Any], *, model_id: str, settings: ChatSettings, wire: ChatWire) -> None:
    """Apply authored request parameters, then the route's own rules on top of them."""
    params.update(settings.request_params or {})
    if wire.route == "openrouter" and settings.models:
        params["extra_body"] = {**(params.get("extra_body") or {}), "models": settings.models}
    if wire.route == "deepseek":
        _deepseek_thinking(params, settings, model_id)
    if wire.route == "kimi" and settings.prompt_cache_key:
        params.setdefault("prompt_cache_key", settings.prompt_cache_key)
    if provider_tools_disabled():
        _reject_native_search(params, model_id, wire)


@cache
def _sdk_body_fields(endpoint: _SDKEndpoint) -> frozenset[str]:
    """Return the body fields the SDK's ``create`` takes as keywords."""
    if endpoint == "chat":
        from openai.resources.chat.completions import AsyncCompletions  # noqa: PLC0415

        create = AsyncCompletions.create
    else:
        from openai.resources.responses import AsyncResponses  # noqa: PLC0415

        create = AsyncResponses.create
    return frozenset(inspect.signature(create).parameters) - {"self", *_SDK_OPTION_KEYS}


def sdk_call_kwargs(params: Mapping[str, Any], endpoint: _SDKEndpoint) -> dict[str, Any]:
    """Return the SDK ``create`` arguments for one request's body fields and SDK options.

    The SDK serializes its keywords in its own order and appends ``extra_body`` after them, so every field the SDK
    names travels as its keyword, as Agno passed it, and only the fields it does not name travel in ``extra_body``.
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
    response_format: type[BaseModel] | Mapping[str, Any] | None,
    stream: bool,
    settings: ChatSettings,
    wire: ChatWire,
) -> dict[str, Any]:
    """Return the ``chat.completions.create`` keyword arguments for one request.

    Fields the SDK names are its keywords and the rest travel in ``extra_body`` (``sdk_call_kwargs``).
    """
    params = _dialect_params(settings, wire, response_format)
    _add_tools(
        params,
        _chat_tools(tools, wire),
        tool_choice,
        model_id=model_id,
        settings=settings,
        wire=wire,
    )
    _add_route_params(params, model_id=model_id, settings=settings, wire=wire)
    params = {"model": model_id, "messages": _chat_messages(messages, wire, settings), **disable_tool_selection(params)}
    if stream:
        params["stream"] = True
        if wire.dialect == "openai":
            params["stream_options"] = {"include_usage": True}
    return sdk_call_kwargs(params, "chat")


# --- Replies --------------------------------------------------------------------------------------------------


def _extra(model: BaseModel | None) -> dict[str, Any]:
    return dict(model.model_extra or {}) if model is not None else {}


def _openai_usage(usage: CompletionUsage) -> MessageMetrics:
    metrics = MessageMetrics(
        input_tokens=usage.prompt_tokens or 0,
        output_tokens=usage.completion_tokens or 0,
        total_tokens=usage.total_tokens or 0,
    )
    if details := usage.prompt_tokens_details:
        metrics.audio_input_tokens = details.audio_tokens or 0
        metrics.cache_read_tokens = details.cached_tokens or 0
        metrics.cache_write_tokens = details.cache_write_tokens or 0
    if completion_details := usage.completion_tokens_details:
        metrics.audio_output_tokens = completion_details.audio_tokens or 0
        metrics.reasoning_tokens = completion_details.reasoning_tokens or 0
    metrics.audio_total_tokens = metrics.audio_input_tokens + metrics.audio_output_tokens
    cost = _extra(usage).get("cost")
    metrics.cost = cost if isinstance(cost, int | float) else None
    return metrics


def _detail(usage: Mapping[str, Any], section: str, name: str) -> int:
    details = usage.get(section)
    value = details.get(name) if isinstance(details, Mapping) else None
    return value if isinstance(value, int) else 0


def _counted_usage(usage: Mapping[str, Any], dialect: Literal["groq", "cerebras"]) -> MessageMetrics:
    """Read Groq and Cerebras usage: totals recomputed and timing counters kept as provider metrics.

    Cerebras keeps its zero counters; Groq keeps only the counters that are set.
    """
    input_tokens = usage.get("prompt_tokens") or 0
    output_tokens = usage.get("completion_tokens") or 0
    metrics = MessageMetrics(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_read_tokens=_detail(usage, "prompt_tokens_details", "cached_tokens"),
        reasoning_tokens=_detail(usage, "completion_tokens_details", "reasoning_tokens"),
    )
    if dialect == "cerebras":
        timings = {key: usage[key] for key in ("time_system", "time_prompt") if usage.get(key) is not None}
    else:
        timings = {
            key: usage[key] for key in ("completion_time", "prompt_time", "queue_time", "total_time") if usage.get(key)
        }
    if timings:
        metrics.provider_metrics = timings
    return metrics


def _merge_provider_data(target: dict[str, Any], update: Mapping[str, Any]) -> None:
    """Merge one delta's provider data: lists extend, everything else is replaced."""
    for key, value in update.items():
        existing = target.get(key)
        if isinstance(existing, list) and isinstance(value, list):
            existing.extend(value)
        else:
            target[key] = value


def _completed_tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give id-less calls (local servers) a generated id, as Agno's loop did."""
    for call in calls:
        if not call.get("id"):
            call["id"] = str(uuid4())
    return calls


def _reply_metadata(reply: ChatCompletion | ChatCompletionChunk) -> dict[str, Any]:
    """Return the ``id``, ``system_fingerprint``, and unknown fields a reply carried, as Agno stored them."""
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


def _usage_metrics(usage: CompletionUsage, wire: ChatWire) -> MessageMetrics:
    if wire.dialect == "openai":
        return _openai_usage(usage)
    return _counted_usage(usage.model_dump(), wire.dialect)


@dataclass(slots=True)
class ChatStream:
    """Fold streamed Chat Completions chunks into deltas and one finished turn."""

    wire: ChatWire
    parts: ChatTurnParts = field(default_factory=ChatTurnParts)
    _content: list[str] = field(default_factory=list)
    _reasoning: list[str] = field(default_factory=list)
    _calls: dict[int, dict[str, Any]] = field(default_factory=dict)

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
                if self.wire.dialect == "openai":
                    events.extend(self._add_reasoning(_extra(delta)))
        return events

    def _add_content(self, chunk: ChatCompletionChunk, content: str | None) -> list[StreamEvent]:
        # Cerebras ignored empty content deltas; the other dialects kept them, with their reply metadata.
        if content is None or (not content and self.wire.dialect == "cerebras"):
            return []
        self._content.append(content)
        if self.wire.dialect == "openai":
            _merge_provider_data(self.parts.provider_data, _reply_metadata(chunk))
        return [TextDelta(content)] if content else []

    def _add_reasoning(self, fields: Mapping[str, Any]) -> list[StreamEvent]:
        if self.wire.route == "openrouter" and (details := fields.get("reasoning_details")):
            _merge_provider_data(self.parts.provider_data, {"reasoning_details": list(details)})
        reasoning = _reasoning_field(fields)
        if not isinstance(reasoning, str):
            return []
        self._reasoning.append(reasoning)
        return [ReasoningDelta(reasoning)] if reasoning else []

    def _add_call_delta(self, call: Any, position: int) -> None:  # noqa: ANN401 - SDK tool-call delta model
        function = call.function
        name = function.name if function is not None else None
        arguments = function.arguments if function is not None else None
        if self.wire.dialect == "cerebras":
            index = call.index if call.index is not None else position
            entry = self._calls.setdefault(index, {"id": None, "type": None, "function": {"name": "", "arguments": ""}})
        else:
            index = call.index or 0
            if index not in self._calls:
                self._calls[index] = {
                    "id": call.id,
                    "type": call.type,
                    "function": {"name": name or "", "arguments": arguments or ""},
                }
                return
            entry = self._calls[index]
        if name:
            entry["function"]["name"] = name
        if arguments:
            entry["function"]["arguments"] += arguments
        if call.id:
            entry["id"] = call.id
        if call.type:
            entry["type"] = call.type

    def _add_usage(self, chunk: ChatCompletionChunk) -> list[StreamEvent]:
        if self.wire.dialect == "groq":
            # Groq streams its usage in its own chunk extension.
            groq = _extra(chunk).get("x_groq")
            usage = groq.get("usage") if isinstance(groq, Mapping) else None
            metrics = _counted_usage(usage, "groq") if isinstance(usage, Mapping) else None
        else:
            metrics = _usage_metrics(chunk.usage, self.wire) if chunk.usage is not None else None
        if metrics is None:
            return []
        self.parts.usage = metrics if self.parts.usage is None else self.parts.usage + metrics
        return [UsageReported(self.parts.usage, final=False)]

    def finish(self) -> ChatTurnParts:
        """Return the finished turn; the stream must be fully consumed first."""
        self.parts.content = "".join(self._content) or None
        self.parts.reasoning = "".join(self._reasoning) or None
        calls = [self._calls[index] for index in sorted(self._calls)]
        if self.wire.dialect == "cerebras":
            calls = [call for call in calls if call.get("id") and call["function"].get("name")]
        self.parts.tool_calls = _completed_tool_calls(calls)
        return self.parts


def _completion_tool_calls(message: Any, wire: ChatWire) -> list[dict[str, Any]]:  # noqa: ANN401 - SDK message model
    if wire.dialect != "cerebras":
        return _completed_tool_calls([call.model_dump() for call in message.tool_calls or []])
    return _completed_tool_calls(
        [
            {
                "id": call.id,
                "type": call.type,
                "function": {"name": call.function.name, "arguments": call.function.arguments},
            }
            for call in message.tool_calls or []
        ],
    )


def parse_chat_completion(completion: ChatCompletion, wire: ChatWire, *, provider: str) -> ChatTurnParts:
    """Read one non-streamed completion into a finished turn.

    A gateway such as OpenRouter answers a failed upstream with HTTP 200 and an ``error`` body instead of choices,
    which raises as the error it names, as a stream's error chunk does.
    """
    if (error := _extra(completion).get("error")) is not None:
        raise error_body_error(error, provider=provider, default_message="The provider returned an error")
    choice = completion.choices[0]
    message = choice.message
    parts = ChatTurnParts(
        content=message.content,
        finish_reason=choice.finish_reason,
        tool_calls=_completion_tool_calls(message, wire),
        usage=_usage_metrics(completion.usage, wire) if completion.usage is not None else None,
    )
    if wire.dialect != "openai":
        return parts
    if parts.content:
        parts.reasoning, parts.content = split_thinking(parts.content)
    message_extra = _extra(message)
    if isinstance(reasoning_field := _reasoning_field(message_extra), str):
        parts.reasoning = reasoning_field
    parts.provider_data = _reply_metadata(completion)
    if wire.route == "openrouter" and (details := message_extra.get("reasoning_details")):
        parts.provider_data["reasoning_details"] = details
    # TODO(simplify): Only a blocking reply stores its finish_reason in provider_data, and nothing reads it.
    # Store it on neither path. Stored data.
    parts.provider_data["finish_reason"] = choice.finish_reason
    return parts
