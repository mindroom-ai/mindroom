# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Responses API request bodies and event parsing for the OpenAI and Codex routes.

Stored responses continue with ``previous_response_id``; ``store: false`` responses and portable replay
send the history explicitly, with ordered output items and encrypted reasoning so a later request can
replay a tool loop exactly. A streamed turn counts only when ``response.completed`` arrives.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.llm.provider import (
    ReasoningDelta,
    StopReason,
    TextDelta,
    ToolCallReady,
    UsageReported,
    hosted_tool_started,
)
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.native_compaction import NATIVE_CHECKPOINT_KEY, native_checkpoint_state, native_replay_messages
from mindroom.providers.errors import classify_provider_error
from mindroom.providers.openai.chat_wire import function_definition, sdk_call_kwargs
from mindroom.providers.openai.codex_auth import codex_installation_metadata, codex_session_headers
from mindroom.providers.openai.media import responses_file_part, responses_image_part
from mindroom.providers.openai.response_replay import (
    RESPONSE_OUTPUT_KEY,
    TOOL_SEARCH_ITEMS_KEY,
    formatted_input_with_provider_items,
)
from mindroom.providers.openai.schema import strict_response_schema
from mindroom.providers.openai.settings import CODEX_REJECTED_FIELDS
from mindroom.providers.openai.tool_ids import reformat_tool_call_ids
from mindroom.providers.openai.tool_search import request_params_with_deferred_tool_search
from mindroom.providers.tool_policy import disable_tool_selection
from mindroom.system_prompt import SESSION_CONTEXT_BOUNDARY

if TYPE_CHECKING:
    from collections.abc import Sequence

    from openai.types.responses import Response, ResponseStreamEvent, ResponseUsage

    from mindroom.llm.errors import ProviderError
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest, NativeCompactionReplay, StreamEvent, ToolSpec
    from mindroom.providers.openai.settings import ResponsesSettings

__all__ = [
    "ResponsesReplay",
    "ResponsesStream",
    "ResponsesTurnParts",
    "ResponsesWire",
    "parse_response",
    "request_store",
    "responses_input",
    "responses_replay",
    "responses_request_kwargs",
    "responses_stop_reason",
    "usage_report",
]

_ENCRYPTED_REASONING = "reasoning.encrypted_content"
# The events that end a response's turn, completed or incomplete; each carries the response with its usage.
_ENDED_EVENT_TYPES = frozenset({"response.completed", "response.incomplete"})
# The events that end a response, a failed one included.
_TERMINAL_EVENT_TYPES = _ENDED_EVENT_TYPES | {"response.failed"}
_TOOL_SEARCH_ITEM_TYPES = frozenset({"tool_search_call", "tool_search_output"})
# The output items the model writes itself; every other item is a hosted tool's call or output.
_MODEL_ITEM_TYPES = frozenset({"message", "reasoning", "function_call"})
_STORED_KEY = "mindroom_response_stored"
_PORTABLE_KEY = "mindroom_portable_replay"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponsesWire:
    """How one Responses route shapes requests: its storage setting, prompt-cache behavior, and Codex instructions."""

    route: Literal["openai", "codex"]
    store: bool | None
    explicit_breakpoint: bool
    default_instructions: str


def request_store(wire: ResponsesWire, request: ModelRequest) -> bool | None:
    """Return the request's storage setting; native compaction replays self-contained history, never stored."""
    return False if request.native_compaction is not None else wire.store


# --- Input ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponsesReplay:
    """How one request continues its history, decided once for both its parameters and its input.

    A request chaining to ``previous_response_id`` sends only ``history[sent_from:]``, the messages after that
    response; any other request sends all of ``history``, with its stored reasoning when ``replay_reasoning``.
    ``history`` is already projected to the latest checkpoint on ``native_route``.
    """

    history: list[Message]
    sent_from: int
    previous_response_id: str | None
    replay_reasoning: bool
    native_route: str | None


def _latest_response(messages: Sequence[Message]) -> tuple[int, dict[str, Any]] | None:
    """Return the index and provider data of the latest assistant message that records a response id."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == "assistant" and message.provider_data and message.provider_data.get("response_id"):
            return index, message.provider_data
    return None


def responses_replay(
    messages: Sequence[Message],
    *,
    store: bool | None,
    portable_replay: bool,
    native_compaction: NativeCompactionReplay | None,
) -> ResponsesReplay:
    """Decide whether a request with storage setting ``store`` continues the latest stored response in ``messages``.

    Portable replay, an unstored request, and a history whose latest response was not stored send the history
    explicitly with its reasoning; a stored request without a stored response sends it without.
    """
    route = native_compaction.route if native_compaction is not None else None
    history = native_replay_messages(messages, route)
    latest = _latest_response(history)
    replay_reasoning = portable_replay or store is False or (latest is not None and latest[1].get(_STORED_KEY) is False)
    chained = None if replay_reasoning else latest
    return ResponsesReplay(
        history=history,
        sent_from=0 if chained is None else chained[0] + 1,
        previous_response_id=None if chained is None else chained[1]["response_id"],
        replay_reasoning=replay_reasoning,
        native_route=route,
    )


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


def _call_ids(messages: Sequence[Message]) -> dict[str, str]:
    """Map each function-call item id (``fc_``) to the ``call_`` id its result must reference."""
    call_ids: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls or ():
            call_id = call.get("call_id") or call.get("id")
            if isinstance(call.get("id"), str) and isinstance(call_id, str):
                call_ids[call["id"]] = call_id
    return call_ids


def _input_items(replay: ResponsesReplay) -> list[Any]:
    """Convert the messages a request sends to Responses input items."""
    canonical = reformat_tool_call_ids(replay.history, "responses")
    call_ids = _call_ids(canonical)
    items: list[Any] = []
    for message in canonical[replay.sent_from :]:
        if message.role in {"user", "system"}:
            items.append(_prompt_item(message))
        elif message.role == "tool":
            if message.tool_call_id and message.content is not None:
                call_id = call_ids.get(message.tool_call_id, message.tool_call_id)
                items.append({"type": "function_call_output", "call_id": call_id, "output": message.content})
        elif message.tool_calls:
            # A continued response already holds its calls; only their results are sent.
            if replay.previous_response_id is None:
                items.extend(_function_call_items(message))
        elif message.role == "assistant":
            items.append({"role": "assistant", "content": message.content if message.content is not None else ""})
            reasoning = (message.provider_data or {}).get("reasoning_output")
            if replay.replay_reasoning and reasoning is not None:
                items.append(copy.deepcopy(reasoning))
    return items


def _with_shared_system_prefix(formatted_input: list[Any], *, explicit_breakpoint: bool) -> list[Any]:
    """Split MindRoom's initial system text so the shared agent instructions stay a cacheable prefix."""
    first_message = formatted_input[0] if formatted_input else None
    if not isinstance(first_message, dict) or first_message.get("role") not in {"system", "developer"}:
        return formatted_input
    content = first_message.get("content")
    if not isinstance(content, str):
        return formatted_input
    shared_text, boundary, session_text = content.partition(SESSION_CONTEXT_BOUNDARY)
    if not boundary or not shared_text.strip():
        return formatted_input
    shared_block: dict[str, Any] = {"type": "input_text", "text": shared_text}
    if explicit_breakpoint:
        shared_block["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return [
        {**first_message, "content": [shared_block]},
        {**first_message, "content": boundary + session_text},
        *formatted_input[1:],
    ]


def responses_input(replay: ResponsesReplay, *, cache_system_prompt: bool, explicit_breakpoint: bool) -> list[Any]:
    """Return the ``input`` items for one request.

    Explicit replay rebuilds stored tool loops from their ordered output items, so encrypted reasoning
    stays attached to the calls it produced even when the next response is stored again.
    """
    # Ordered output is restored only for the spans the request still sends.
    formatted = formatted_input_with_provider_items(
        replay.history[replay.sent_from :],
        _input_items(replay),
        native_route=replay.native_route,
        replay_reasoning=replay.replay_reasoning,
    )
    if cache_system_prompt:
        formatted = _with_shared_system_prefix(formatted, explicit_breakpoint=explicit_breakpoint)
    return formatted


# --- Request --------------------------------------------------------------------------------------------------


def _tool_dicts(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    """Return tools sorted by name with function definitions flattened as Responses expects."""
    functions: list[dict[str, Any]] = []
    for tool in tools:
        definition: dict[str, Any] = {**function_definition(tool), "type": "function"}
        # TODO(simplify): A top-level parameter's list `type` keeps only its first entry, so ["string", "null"] loses
        # null, while nested schemas and Chat routes send the list. Send the schema unchanged. Models.
        for property_schema in definition.get("parameters", {}).get("properties", {}).values():
            if isinstance(property_schema, dict) and isinstance(property_schema.get("type"), list):
                property_schema["type"] = property_schema["type"][0]
        functions.append(definition)
    return sorted(functions, key=lambda tool: tool["name"])


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
    # A request's temperature override and raw ``request_params`` reach here; config load retires the authored settings.
    for name in CODEX_REJECTED_FIELDS:
        params.pop(name, None)


def _base_params(settings: ResponsesSettings, request: ModelRequest) -> dict[str, Any]:
    """Return the configured request parameters that are set, with reasoning and the text format."""
    reasoning = dict(settings.reasoning or {})
    if settings.reasoning_effort is not None:
        reasoning["effort"] = settings.reasoning_effort
    if settings.reasoning_summary is not None:
        reasoning["summary"] = settings.reasoning_summary
    text: dict[str, Any] = {}
    if settings.verbosity is not None:
        text["verbosity"] = settings.verbosity
    if (schema := request.output_schema) is not None:
        text["format"] = {
            "type": "json_schema",
            "name": schema.__name__,
            "schema": strict_response_schema(schema),
            "strict": settings.strict_output,
        }
    params = {
        **settings.request_fields(),
        "include": copy.deepcopy(settings.include),
        "reasoning": reasoning,
        "text": text or None,
    }
    return {key: value for key, value in params.items() if value is not None}


def _add_continuation(params: dict[str, Any], store: bool | None, replay: ResponsesReplay) -> None:
    """Continue the last stored response, or replay with encrypted reasoning when nothing is stored."""
    params["store"] = store is not False
    if store is False:
        _with_encrypted_reasoning(params)
    elif replay.previous_response_id is not None:
        params["previous_response_id"] = replay.previous_response_id


def _add_replay_policy(params: dict[str, Any], request: ModelRequest) -> None:
    if request.portable_replay:
        # An authored ``previous_response_id`` would chain a request that replays its history explicitly.
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
    replay = responses_replay(
        request.messages,
        store=store,
        portable_replay=request.portable_replay,
        native_compaction=request.native_compaction,
    )
    params = _base_params(settings, request)
    if tools := _tool_dicts(request.tools):
        params["tools"] = tools
    if request.tool_choice is not None:
        params["tool_choice"] = request.tool_choice
    _add_continuation(params, store, replay)
    # ``store`` in request_params already became the route's storage setting.
    params.update({key: value for key, value in (settings.request_params or {}).items() if key != "store"})
    _add_replay_policy(params, request)
    params = disable_tool_selection(request_params_with_deferred_tool_search(params, request.deferred_tools.names))
    if wire.route == "codex":
        _codex_params(params, settings, wire)
    formatted_input = responses_input(
        replay,
        cache_system_prompt=settings.cache_system_prompt,
        explicit_breakpoint=wire.explicit_breakpoint,
    )
    return sdk_call_kwargs({"model": model_id, "input": formatted_input, **params}, "responses")


# --- Responses ------------------------------------------------------------------------------------------------


def _usage(usage: ResponseUsage) -> MessageMetrics:
    input_details, output_details = usage.input_tokens_details, usage.output_tokens_details
    counts = TokenCounts(
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        # The total is the one the provider reports.
        total_tokens=usage.total_tokens or 0,
        cache_read_tokens=(input_details.cached_tokens or 0) if input_details else 0,
        cache_write_tokens=(input_details.cache_write_tokens or 0) if input_details else 0,
        reasoning_tokens=(output_details.reasoning_tokens or 0) if output_details else 0,
    )
    return MessageMetrics(tokens=counts)


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


def _record_tool_search_items(parts: ResponsesTurnParts, output_items: Sequence[Any]) -> None:
    items = [item.model_dump(exclude_none=True) for item in output_items if item.type in _TOOL_SEARCH_ITEM_TYPES]
    if items:
        parts.provider_data.setdefault(TOOL_SEARCH_ITEMS_KEY, []).extend(items)


def _incomplete_reason(response: Response) -> str | None:
    return None if response.incomplete_details is None else response.incomplete_details.reason


def _read_ended_response(
    parts: ResponsesTurnParts,
    response: Response,
    items: list[dict[str, Any]],
    *,
    store: bool | None,
    portable_replay: bool,
    native_compaction: NativeCompactionReplay | None,
) -> None:
    """Record what a response that ended says about its turn, the same whether it streamed or arrived whole.

    A completed and an incomplete response both end the turn, and ``responses_stop_reason`` reads its stop from the
    status and the incomplete reason. Both record the response id, its storage provenance, and, when unstored, its
    reasoning; only a completed response's output is recorded for replay.
    """
    parts.status = response.status
    parts.incomplete_reason = _incomplete_reason(response)
    if response.usage is not None:
        parts.usage = _usage(response.usage)
    if response.id:
        parts.provider_data["response_id"] = response.id
    if store is False:
        for output in response.output:
            if output.type == "reasoning":
                parts.provider_data["reasoning_output"] = output.model_dump(exclude_none=True)
    parts.provider_data[_STORED_KEY] = store is not False
    if response.status != "completed":
        return
    parts.provider_data[_PORTABLE_KEY] = portable_replay
    parts.provider_data[NATIVE_CHECKPOINT_KEY] = native_checkpoint_state(items, native_compaction)
    if (store is False or portable_replay) and any(item.get("type") == "reasoning" for item in items):
        parts.provider_data[RESPONSE_OUTPUT_KEY] = [item for item in items if item.get("type") != "compaction"]


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
    event order with any summary text, kept as main behaves. ``provider`` is the metrics provider name a failed
    response's error carries.
    """

    provider: str
    store: bool | None
    portable_replay: bool
    native_compaction: NativeCompactionReplay | None
    text_as_reasoning: bool
    parts: ResponsesTurnParts = field(default_factory=ResponsesTurnParts)
    # Whether the response ended, completed or incomplete; a stream that stops before then did not finish the turn.
    ended: bool = False
    _content: list[str] = field(default_factory=list)
    _reasoning: list[str] = field(default_factory=list)
    _tool_use: dict[str, Any] = field(default_factory=dict)
    _items: dict[int, dict[str, Any]] = field(default_factory=dict)

    def _failure(self, event: Any) -> ProviderError:  # noqa: ANN401 - SDK error event
        if event.type == "error":
            return _response_error(event.code, event.message, provider=self.provider)
        failure = event.response.error
        return _response_error(
            failure.code if failure is not None else None,
            failure.message if failure is not None else None,
            provider=self.provider,
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
            events.extend(self._ready_call())
            _record_tool_search_items(self.parts, [current.item])
            if self.store is False or self.portable_replay:
                self._items[current.output_index] = current.item.model_dump(mode="json", exclude_none=True)
        elif event.type in _ENDED_EVENT_TYPES:
            # A call the response ended in the middle of is kept, as a blocking response keeps every call it lists.
            events.extend(self._ready_call())
            self._end(current.response)
        return events

    def _ready_call(self) -> list[StreamEvent]:
        """Add the function call in progress, if any, to the turn and return its ready event."""
        if not self._tool_use:
            return []
        call, self._tool_use = self._tool_use, {}
        self.parts.tool_calls.append(call)
        return [ToolCallReady(call)]

    def _end(self, response: Response) -> None:
        self.ended = True
        items = [item.model_dump(mode="json", exclude_none=True) for item in response.output] or [
            self._items[index] for index in sorted(self._items)
        ]
        _read_ended_response(
            self.parts,
            response,
            items,
            store=self.store,
            portable_replay=self.portable_replay,
            native_compaction=self.native_compaction,
        )

    def finish(self) -> ResponsesTurnParts:
        """Return the finished turn after the response ended."""
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
    provider: str,
    store: bool | None,
    portable_replay: bool,
    native_compaction: NativeCompactionReplay | None,
    text_as_reasoning: bool,
) -> ResponsesTurnParts:
    """Read one non-streamed response into a finished turn; ``provider`` is the metrics provider name of its error.

    ``text_as_reasoning`` stores the output text as the reasoning of a response without a reasoning summary, kept as
    main behaves.
    """
    if response.error:
        raise _response_error(response.error.code, response.error.message, provider=provider)
    parts = ResponsesTurnParts()
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
    parts.reasoning = _reasoning_summary(response) or (response.output_text if text_as_reasoning else None)
    # TODO(simplify): Only a blocking response stores response_status and incomplete_reason in provider_data, and
    # nothing reads them. Store them on neither path. Stored data.
    parts.provider_data["response_status"] = response.status
    parts.provider_data["incomplete_reason"] = _incomplete_reason(response)
    _record_tool_search_items(parts, response.output)
    _read_ended_response(
        parts,
        response,
        [item.model_dump(mode="json", exclude_none=True) for item in response.output],
        store=store,
        portable_replay=portable_replay,
        native_compaction=native_compaction,
    )
    return parts
