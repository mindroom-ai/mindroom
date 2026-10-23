# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Responses API request bodies and event parsing for the OpenAI and Codex routes.

Stored responses continue with ``previous_response_id``; ``store: false`` responses and portable replay
send the history explicitly, with ordered output items and encrypted reasoning so a later request can
replay a tool loop exactly. A streamed turn counts only when ``response.completed`` arrives.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel

from mindroom.llm.provider import (
    ReasoningDelta,
    StopReason,
    TextDelta,
    ToolCallReady,
    UsageReported,
    hosted_tool_started,
)
from mindroom.llm.usage import MessageMetrics
from mindroom.native_compaction import native_replay_messages, record_native_checkpoint
from mindroom.providers.errors import classify_provider_error
from mindroom.providers.legacy_stored_tool_calls import repair_legacy_tool_calls
from mindroom.providers.openai.chat_wire import function_definition, sdk_call_kwargs
from mindroom.providers.openai.codex_auth import codex_installation_metadata, codex_session_headers
from mindroom.providers.openai.media import responses_file_part, responses_image_part
from mindroom.providers.openai.prompt_cache import formatted_input_with_shared_system_prefix
from mindroom.providers.openai.response_replay import (
    RESPONSE_OUTPUT_KEY,
    TOOL_SEARCH_ITEMS_KEY,
    formatted_input_with_provider_items,
)
from mindroom.providers.openai.schema import strict_response_schema
from mindroom.providers.openai.tool_ids import reformat_tool_call_ids
from mindroom.providers.openai.tool_search import request_params_with_deferred_tool_search
from mindroom.providers.tool_policy import disable_tool_selection

if TYPE_CHECKING:
    from collections.abc import Sequence

    from openai.types.responses import Response, ResponseStreamEvent, ResponseUsage

    from mindroom.llm.errors import ProviderError
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest, NativeCompactionReplay, StreamEvent, ToolSpec
    from mindroom.providers.openai.settings import ResponsesSettings

__all__ = [
    "ResponsesStream",
    "ResponsesTurnParts",
    "ResponsesWire",
    "parse_response",
    "request_store",
    "responses_input",
    "responses_request_kwargs",
    "responses_stop_reason",
    "usage_report",
]

_ENCRYPTED_REASONING = "reasoning.encrypted_content"
# The events that end a response; each carries the response with its usage.
_TERMINAL_EVENT_TYPES = frozenset({"response.completed", "response.incomplete", "response.failed"})
_TOOL_SEARCH_ITEM_TYPES = frozenset({"tool_search_call", "tool_search_output"})
# The output items the model writes itself; every other item is a hosted tool's call or output.
_MODEL_ITEM_TYPES = frozenset({"message", "reasoning", "function_call"})
_STORED_KEY = "mindroom_response_stored"
_PORTABLE_KEY = "mindroom_portable_replay"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponsesWire:
    """How one Responses route shapes requests: its storage setting and prompt-cache behavior."""

    route: Literal["openai", "codex"]
    provider_name: str
    store: bool | None
    explicit_breakpoint: bool
    default_instructions: str | None = None


def request_store(wire: ResponsesWire, request: ModelRequest) -> bool | None:
    """Return the request's storage setting; native compaction replays self-contained history, never stored."""
    return False if request.native_compaction is not None else wire.store


# --- Input ----------------------------------------------------------------------------------------------------


def _latest_response_data(messages: Sequence[Message]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.role == "assistant" and message.provider_data and message.provider_data.get("response_id"):
            return message.provider_data
    return None


def _prepare_continuation(messages: Sequence[Message], *, explicit_replay: bool) -> tuple[list[Message], bool]:
    """Avoid chaining to an unstored response or to an older server context the history no longer matches."""
    latest = _latest_response_data(messages)
    if not explicit_replay and (latest is None or latest.get(_STORED_KEY) is not False):
        return list(messages), False
    return [
        replace(
            message,
            provider_data={key: value for key, value in message.provider_data.items() if key != "response_id"},
        )
        if message.provider_data and "response_id" in message.provider_data
        else message
        for message in messages
    ], True


def _prompt_item(message: Message) -> dict[str, Any]:
    """Return a system or user message as a Responses input message with its images and files."""
    item: dict[str, Any] = {"role": "developer" if message.role == "system" else "user"}
    if message.content is not None:
        item["content"] = message.content
    if message.images and isinstance(message.content, str):
        item["content"] = [
            {"type": "input_text", "text": message.content},
            *(part for image in message.images if (part := responses_image_part(image)) is not None),
        ]
    if message.files:
        content = item.get("content")
        parts: list[Any] = content if isinstance(content, list) else [{"type": "input_text", "text": content or ""}]
        parts.extend(part for file in message.files if (part := responses_file_part(file)) is not None)
        item["content"] = parts
    return item


def _function_call_items(message: Message) -> list[dict[str, Any]]:
    return [
        {
            "type": "function_call",
            "id": call.get("id"),
            "call_id": call.get("call_id", call.get("id")),
            "name": call["function"]["name"],
            "arguments": call["function"]["arguments"],
            "status": "completed",
        }
        for call in message.tool_calls or ()
    ]


def _continued_messages(messages: list[Message], *, store: bool | None) -> tuple[list[Message], bool]:
    """Return the messages after the last stored response, and whether such a response continues the request."""
    if store is not False:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.role == "assistant" and message.provider_data and "response_id" in message.provider_data:
                return messages[index + 1 :], True
    return messages, False


def _call_ids(messages: Sequence[Message]) -> dict[str, str]:
    """Map each function-call item id (``fc_``) to the ``call_`` id its result must reference."""
    call_ids: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls or ():
            call_id = call.get("call_id") or call.get("id")
            if isinstance(call.get("id"), str) and isinstance(call_id, str):
                call_ids[call["id"]] = call_id
    return call_ids


def _agno_input(messages: Sequence[Message], *, store: bool | None) -> list[Any]:
    """Convert canonical messages to Responses input items, continuing after the last stored response."""
    canonical = reformat_tool_call_ids(messages, "responses")
    to_format, continued = _continued_messages(canonical, store=store)
    call_ids = _call_ids(canonical)
    items: list[Any] = []
    for message in to_format:
        if message.role in {"user", "system"}:
            items.append(_prompt_item(message))
        elif message.role == "tool":
            if message.tool_call_id and message.content is not None:
                call_id = call_ids.get(message.tool_call_id, message.tool_call_id)
                items.append({"type": "function_call_output", "call_id": call_id, "output": message.content})
        elif message.tool_calls:
            # A continued response already holds its calls; only their results are sent.
            if not continued:
                items.extend(_function_call_items(message))
        elif message.role == "assistant":
            items.append({"role": "assistant", "content": message.content if message.content is not None else ""})
            reasoning = (message.provider_data or {}).get("reasoning_output")
            if store is False and reasoning is not None:
                items.append(copy.deepcopy(reasoning))
    return items


def responses_input(
    messages: Sequence[Message],
    *,
    store: bool | None,
    portable_replay: bool,
    native_compaction: NativeCompactionReplay | None,
    cache_system_prompt: bool,
    explicit_breakpoint: bool,
) -> list[Any]:
    """Return the ``input`` items for one request.

    Explicit replay rebuilds stored tool loops from their ordered output items, so encrypted reasoning
    stays attached to the calls it produced even when the next response is stored again.
    """
    prepared, explicit = _prepare_continuation(messages, explicit_replay=portable_replay)
    route = native_compaction.route if native_compaction is not None else None
    prepared = native_replay_messages(repair_legacy_tool_calls(prepared), route)
    replay_store = False if explicit else store
    formatted = _agno_input(prepared, store=replay_store)
    # Ordered output is restored only for the spans the request still sends.
    prepared, _ = _continued_messages(prepared, store=replay_store)
    formatted = formatted_input_with_provider_items(
        prepared,
        formatted,
        native_route=route,
        replay_reasoning=replay_store is False,
    )
    if cache_system_prompt:
        formatted = formatted_input_with_shared_system_prefix(formatted, explicit_breakpoint=explicit_breakpoint)
    return formatted


# --- Request --------------------------------------------------------------------------------------------------


def _tool_dicts(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Return tools sorted by name with function definitions flattened as Responses expects."""
    functions: list[dict[str, Any]] = []
    for tool in tools:
        definition: dict[str, Any] = {**function_definition(tool), "type": "function"}
        for property_schema in definition.get("parameters", {}).get("properties", {}).values():
            if isinstance(property_schema, dict) and isinstance(property_schema.get("type"), list):
                property_schema["type"] = property_schema["type"][0]
        functions.append(definition)
    return sorted(functions, key=lambda tool: tool["name"])


def _text_format(response_format: type[BaseModel], *, strict: bool) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": response_format.__name__,
        "schema": strict_response_schema(response_format),
        "strict": strict,
    }


def _with_encrypted_reasoning(params: dict[str, Any]) -> None:
    include = list(params.get("include") or [])
    if _ENCRYPTED_REASONING not in include:
        include.append(_ENCRYPTED_REASONING)
    params["include"] = include


def _codex_params(params: dict[str, Any], settings: ResponsesSettings, wire: ResponsesWire) -> None:
    """Add what the Codex endpoint requires and drop what it rejects."""
    params.setdefault("instructions", wire.default_instructions)
    if settings.prompt_cache_key:
        params.setdefault("prompt_cache_key", settings.prompt_cache_key)
    if settings.session_id:
        params["extra_headers"] = {**codex_session_headers(settings.session_id), **(params.get("extra_headers") or {})}
    if (settings.prompt_cache_key or settings.session_id) and (
        client_metadata := codex_installation_metadata(codex_home=settings.codex_home)
    ):
        extra_body = dict(params.get("extra_body") or {})
        existing = extra_body.get("client_metadata")
        if isinstance(existing, dict):
            extra_body["client_metadata"] = {**client_metadata, **existing}
        elif existing is None:
            extra_body["client_metadata"] = client_metadata
        params["extra_body"] = extra_body
    for name in ("max_output_tokens", "temperature"):
        params.pop(name, None)


def _base_params(settings: ResponsesSettings, request: ModelRequest, store: bool | None) -> dict[str, Any]:
    """Return the configured request parameters that are set, with reasoning and the text format."""
    reasoning = dict(settings.reasoning or {})
    if settings.reasoning_effort is not None:
        reasoning["effort"] = settings.reasoning_effort
    if settings.reasoning_summary is not None:
        reasoning["summary"] = settings.reasoning_summary
    text: dict[str, Any] = {}
    if settings.verbosity is not None:
        text["verbosity"] = settings.verbosity
    # Responses takes native structured output, so a request's format is always its output model class.
    response_format = request.response_format
    if isinstance(response_format, type) and issubclass(response_format, BaseModel):
        text["format"] = _text_format(response_format, strict=settings.strict_output)
    params = {
        "include": copy.deepcopy(settings.include),
        "max_output_tokens": settings.max_output_tokens,
        "max_tool_calls": settings.max_tool_calls,
        "metadata": settings.metadata,
        "parallel_tool_calls": settings.parallel_tool_calls,
        "store": store,
        "temperature": settings.temperature,
        "top_p": settings.top_p,
        "truncation": settings.truncation,
        "user": settings.user,
        "service_tier": settings.service_tier,
        "extra_headers": settings.extra_headers,
        "extra_query": settings.extra_query,
        "extra_body": settings.extra_body,
        "reasoning": reasoning,
        "text": text or None,
    }
    return {key: value for key, value in params.items() if value is not None}


def _add_continuation(params: dict[str, Any], request: ModelRequest, store: bool | None) -> None:
    """Continue the last stored response, or replay with encrypted reasoning when nothing is stored."""
    if store is False:
        params["store"] = False
        _with_encrypted_reasoning(params)
        return
    params["store"] = True
    prepared, _ = _prepare_continuation(request.messages, explicit_replay=request.portable_replay)
    if (latest := _latest_response_data(prepared)) is not None:
        params["previous_response_id"] = latest["response_id"]


def _add_replay_policy(params: dict[str, Any], request: ModelRequest) -> None:
    if request.portable_replay:
        params.pop("previous_response_id", None)
        if isinstance(extra_body := params.get("extra_body"), dict):
            params["extra_body"] = {key: value for key, value in extra_body.items() if key != "previous_response_id"}
        _with_encrypted_reasoning(params)
    if request.native_compaction is not None:
        params["context_management"] = [
            {"type": "compaction", "compact_threshold": request.native_compaction.threshold},
        ]


def responses_request_kwargs(
    *,
    model_id: str,
    request: ModelRequest,
    settings: ResponsesSettings,
    wire: ResponsesWire,
) -> dict[str, Any]:
    """Return the ``responses.create`` keyword arguments for one request.

    Fields the SDK names are its keywords and the rest travel in ``extra_body`` (``sdk_call_kwargs``).
    """
    store = request_store(wire, request)
    params = _base_params(settings, request, store)
    if tools := _tool_dicts(request.tools):
        params["tools"] = tools
    if request.tool_choice is not None:
        params["tool_choice"] = request.tool_choice
    _add_continuation(params, request, store)
    # ``store`` in request_params already became the route's storage setting.
    params.update({key: value for key, value in (settings.request_params or {}).items() if key != "store"})
    _add_replay_policy(params, request)
    params = disable_tool_selection(request_params_with_deferred_tool_search(params, request.deferred_tool_names))
    if wire.route == "codex":
        _codex_params(params, settings, wire)
    formatted_input = responses_input(
        request.messages,
        store=store,
        portable_replay=request.portable_replay,
        native_compaction=request.native_compaction,
        cache_system_prompt=settings.cache_system_prompt,
        explicit_breakpoint=wire.explicit_breakpoint,
    )
    return sdk_call_kwargs({"model": model_id, "input": formatted_input, **params}, "responses")


# --- Responses ------------------------------------------------------------------------------------------------


def _usage(usage: ResponseUsage) -> MessageMetrics:
    metrics = MessageMetrics(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        total_tokens=usage.total_tokens or 0,
    )
    if input_details := usage.input_tokens_details:
        metrics.cache_read_tokens = input_details.cached_tokens or 0
        metrics.cache_write_tokens = input_details.cache_write_tokens or 0
    if output_details := usage.output_tokens_details:
        metrics.reasoning_tokens = output_details.reasoning_tokens or 0
    return metrics


@dataclass(slots=True, kw_only=True)
class ResponsesTurnParts:
    """One finished Responses turn before it becomes the assistant message."""

    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    provider_data: dict[str, Any] = field(default_factory=dict)
    usage: MessageMetrics | None = None
    status: str | None = None
    incomplete_reason: str | None = None


@dataclass(slots=True)
class _ProviderDataRecord:
    """The ``provider_data`` holder the shared native-compaction recorder writes into."""

    provider_data: dict[str, Any] | None


def _record_completed_output(
    parts: ResponsesTurnParts,
    items: list[dict[str, Any]],
    *,
    store: bool | None,
    portable_replay: bool,
    native_compaction: NativeCompactionReplay | None,
) -> None:
    """Record storage provenance, the native checkpoint, and ordered output when replay will need it."""
    parts.provider_data[_STORED_KEY] = store is not False
    parts.provider_data[_PORTABLE_KEY] = portable_replay
    record = _ProviderDataRecord(provider_data=parts.provider_data)
    record_native_checkpoint(record, items, native_compaction)
    parts.provider_data = record.provider_data or {}
    if (store is False or portable_replay) and any(item.get("type") == "reasoning" for item in items):
        parts.provider_data[RESPONSE_OUTPUT_KEY] = [item for item in items if item.get("type") != "compaction"]


def _record_tool_search_items(parts: ResponsesTurnParts, output_items: Sequence[Any]) -> None:
    items = [item.model_dump(exclude_none=True) for item in output_items if item.type in _TOOL_SEARCH_ITEM_TYPES]
    if items:
        parts.provider_data.setdefault(TOOL_SEARCH_ITEMS_KEY, []).extend(items)


def responses_stop_reason(status: str | None, incomplete_reason: str | None, *, tool_calls: bool) -> StopReason | None:
    """Map a response's terminal status to the normalized stop vocabulary."""
    if status == "completed":
        return "tool_use" if tool_calls else "end"
    if status == "incomplete":
        if incomplete_reason == "max_output_tokens":
            return "output_limit"
        return "content_filter" if incomplete_reason == "content_filter" else "incomplete"
    return None if status is None else "incomplete"


@dataclass(slots=True)
class ResponsesStream:
    """Fold streamed Responses events into deltas and one finished turn.

    An output item other than the model's own message, reasoning, or function call is a hosted tool at work, and
    its start is reported as such. ``text_as_reasoning`` also records the output text in the stored reasoning, in
    event order with any summary text, as Agno did for a model config that authors ``reasoning`` without
    ``reasoning_summary``.
    """

    wire: ResponsesWire
    store: bool | None
    portable_replay: bool
    native_compaction: NativeCompactionReplay | None
    text_as_reasoning: bool
    parts: ResponsesTurnParts = field(default_factory=ResponsesTurnParts)
    completed: bool = False
    _content: list[str] = field(default_factory=list)
    _reasoning: list[str] = field(default_factory=list)
    _tool_use: dict[str, Any] = field(default_factory=dict)
    _items: dict[int, dict[str, Any]] = field(default_factory=dict)

    def _failure(self, event: Any) -> ProviderError:  # noqa: ANN401 - SDK error event
        if event.type == "error":
            return _response_error(event.code, event.message, provider=self.wire.provider_name)
        failure = event.response.error
        return _response_error(
            failure.code if failure is not None else None,
            failure.message if failure is not None else None,
            provider=self.wire.provider_name,
        )

    def add(self, event: ResponseStreamEvent) -> list[StreamEvent]:  # noqa: C901 - one branch per event type
        """Fold one event and return the deltas it carried; failure events raise."""
        current = cast("Any", event)
        if event.type in {"error", "response.failed"}:
            raise self._failure(event)
        events: list[StreamEvent] = []
        if event.type == "response.output_item.added" and current.item.type not in _MODEL_ITEM_TYPES:
            events.append(hosted_tool_started(current.item.type))
        if event.type == "response.output_text.delta":
            self._content.append(current.delta)
            if self.text_as_reasoning:
                self._reasoning.append(current.delta)
            events.append(TextDelta(current.delta))
        elif event.type == "response.reasoning_summary_text.delta":
            self._reasoning.append(current.delta)
            events.append(ReasoningDelta(current.delta))
        elif event.type == "response.output_item.added" and current.item.type == "function_call":
            item = current.item
            self._tool_use = {
                "id": item.id,
                "call_id": item.call_id or item.id,
                "type": "function",
                "function": {"name": item.name, "arguments": item.arguments},
            }
        elif event.type == "response.function_call_arguments.delta" and self._tool_use:
            self._tool_use["function"]["arguments"] += current.delta
        elif event.type == "response.output_item.done":
            if self._tool_use:
                self.parts.tool_calls.append(self._tool_use)
                events.append(ToolCallReady(self._tool_use))
                self._tool_use = {}
            _record_tool_search_items(self.parts, [current.item])
            if self.store is False or self.portable_replay:
                self._items[current.output_index] = current.item.model_dump(mode="json", exclude_none=True)
        elif event.type == "response.completed":
            self._complete(current.response)
        return events

    def _complete(self, response: Response) -> None:
        self.completed = True
        if self.store is False:
            for item in response.output:
                if item.type == "reasoning":
                    self.parts.provider_data["reasoning_output"] = item.model_dump(exclude_none=True)
        if response.usage is not None:
            self.parts.usage = _usage(response.usage)
        self.parts.provider_data["response_id"] = response.id
        items = [item.model_dump(mode="json", exclude_none=True) for item in response.output] or [
            self._items[index] for index in sorted(self._items)
        ]
        _record_completed_output(
            self.parts,
            items,
            store=self.store,
            portable_replay=self.portable_replay,
            native_compaction=self.native_compaction,
        )
        self.parts.status = response.status

    def finish(self) -> ResponsesTurnParts:
        """Return the finished turn after ``response.completed``."""
        self.parts.content = "".join(self._content) or None
        self.parts.reasoning = "".join(self._reasoning) or None
        return self.parts


def _response_error(code: str | None, message: str | None, *, provider: str) -> ProviderError:
    """Return the error a failed response reports: its code names the failure, as an error type does elsewhere."""
    return classify_provider_error(message or "OpenAI Responses stream failed", provider=provider, code=code)


def usage_report(event: ResponseStreamEvent) -> UsageReported | None:
    """Return the usage a response's terminal event reports, as a partial report to publish when it arrives.

    The finished turn's final report replaces it; a stream cut off after it, such as a reply cancelled or stalled
    before the stream ends, or a failed response, still settles what the provider billed.
    """
    if event.type not in _TERMINAL_EVENT_TYPES or not (usage := cast("Any", event).response.usage):
        return None
    return UsageReported(_usage(usage), final=False)


def _reasoning_summary(response: Response) -> str:
    """Return the reasoning summary text a response carries, which a stream delivers as reasoning deltas."""
    return "".join(
        item.text
        for output in cast("list[Any]", response.output)
        if output.type == "reasoning"
        for item in output.summary
        if item.text
    )


def parse_response(
    response: Response,
    *,
    wire: ResponsesWire,
    store: bool | None,
    portable_replay: bool,
    native_compaction: NativeCompactionReplay | None,
    text_as_reasoning: bool,
) -> ResponsesTurnParts:
    """Read one non-streamed response into a finished turn.

    ``text_as_reasoning`` stores the output text as the reasoning of a response without a reasoning summary, as
    Agno did for a model config that authors ``reasoning``.
    """
    if response.error:
        raise _response_error(response.error.code, response.error.message, provider=wire.provider_name)
    parts = ResponsesTurnParts(
        provider_data={"response_id": response.id} if response.id else {},
        status=response.status,
        incomplete_reason=response.incomplete_details.reason if response.incomplete_details is not None else None,
    )
    # The SDK's output union does not narrow on ``type``.
    for output in cast("list[Any]", response.output):
        if output.type == "message":
            parts.content = response.output_text
        elif output.type == "function_call":
            parts.tool_calls.append(
                {
                    "id": output.id,
                    "call_id": output.call_id or output.id,
                    "type": "function",
                    "function": {"name": output.name, "arguments": output.arguments},
                },
            )
        elif output.type == "reasoning" and store is False:
            parts.provider_data["reasoning_output"] = output.model_dump(exclude_none=True)
    parts.reasoning = _reasoning_summary(response) or (response.output_text if text_as_reasoning else None)
    if response.usage is not None:
        parts.usage = _usage(response.usage)
    parts.provider_data[_STORED_KEY] = store is not False
    parts.provider_data["response_status"] = response.status
    parts.provider_data["incomplete_reason"] = parts.incomplete_reason
    _record_tool_search_items(parts, response.output)
    if response.status == "completed":
        _record_completed_output(
            parts,
            [item.model_dump(mode="json", exclude_none=True) for item in response.output],
            store=store,
            portable_replay=portable_replay,
            native_compaction=native_compaction,
        )
    return parts
