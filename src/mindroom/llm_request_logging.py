"""Provider-neutral LLM usage telemetry and opt-in request logging."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from agno.models.message import Message
from pydantic import BaseModel

from mindroom.constants import MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY
from mindroom.logging_config import get_logger
from mindroom.model_usage import context_input_tokens_from_counts
from mindroom.redaction import redact_sensitive_data
from mindroom.tool_system.context_bound_streams import context_bound_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Sequence

    from agno.models.base import Model
    from agno.models.message import MessageMetrics
    from agno.models.response import ModelResponse

    from mindroom.config.models import DebugConfig

_INSTALLED_ATTR = "_mindroom_llm_request_logging_installed"
logger = get_logger(__name__)


_SKIP_MODEL_PARAM_NAMES = {
    "id",
    "name",
    "provider",
    "model_type",
    "system_prompt",
    "instructions",
    "client",
    "async_client",
    "api_key",
    "auth_token",
    "organization",
    "http_client",
    "client_params",
    "default_headers",
    "default_query",
}
_NON_API_MESSAGE_FIELDS = {
    "id",
    "reasoning_content",
    "tool_name",
    "tool_args",
    "tool_call_error",
    "stop_after_tool_call",
    "add_to_agent_memory",
    "from_history",
    "references",
    "temporary",
}
type _JSONScalar = str | int | float | bool | None
type _JSONValue = _JSONScalar | list["_JSONValue"] | dict[str, "_JSONValue"]
_REQUEST_CONTEXT = ContextVar[dict[str, _JSONValue] | None]("mindroom_llm_request_log_context", default=None)
_ACTIVE_MODEL_CALLS = ContextVar[frozenset[int]]("mindroom_llm_observability_active_models", default=frozenset())


@dataclass
class _WireToolsCapture:
    """Request-local tools observed after provider wire transformations."""

    enabled: bool
    observed: bool = False
    tools: list[dict[str, _JSONValue]] | None = None


_WIRE_TOOLS_CAPTURE = ContextVar[_WireToolsCapture | None]("mindroom_llm_wire_tools_capture", default=None)


def _daily_log_path(log_dir: str | None, default_log_dir: Path, now: datetime) -> Path:
    base_dir = Path(log_dir) if log_dir else default_log_dir
    return base_dir / f"llm-requests-{now.date().isoformat()}.jsonl"


def _system_prompt(messages: Sequence[Message], model: Model) -> str:
    for message in messages:
        if message.role == "system":
            return message.get_content_string()
    return model.system_prompt or ""


def _model_params(model: Model) -> dict[str, _JSONValue]:
    if not is_dataclass(model):
        return {}
    payload: dict[str, _JSONValue] = {}
    for field in fields(model):
        if field.name in _SKIP_MODEL_PARAM_NAMES:
            continue
        value = vars(model).get(field.name)
        if value is None:
            continue
        try:
            json.dumps(value)
        except TypeError:
            continue
        payload[field.name] = value
    return payload


def _write_jsonl_line(path: Path, payload: dict[str, _JSONValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact_sensitive_data(payload)))
        handle.write("\n")


def _json_safe(value: object) -> _JSONValue:
    normalized: object = value
    if isinstance(normalized, BaseModel):
        normalized = normalized.model_dump(mode="python", exclude_none=True)
    elif not isinstance(normalized, type) and is_dataclass(normalized):
        normalized = asdict(normalized)

    if isinstance(normalized, dict):
        return {str(key): _json_safe(item) for key, item in normalized.items()}
    if isinstance(normalized, (list, tuple, set)):
        return [_json_safe(item) for item in normalized]
    if isinstance(normalized, bytes):
        return {
            "__type__": "bytes",
            "base64": base64.b64encode(normalized).decode("ascii"),
        }
    if isinstance(normalized, Path):
        return str(normalized)
    if isinstance(normalized, str | int | float | bool) or normalized is None:
        return normalized
    return repr(normalized)


def _request_message_payloads(messages: Sequence[Message]) -> list[dict[str, _JSONValue]]:
    payloads: list[dict[str, _JSONValue]] = []
    for message in messages:
        payload = message.model_dump(
            mode="python",
            exclude_none=True,
            exclude=_NON_API_MESSAGE_FIELDS,
        )
        payloads.append(cast("dict[str, _JSONValue]", _json_safe(payload)))
    return payloads


def _request_messages(value: object) -> list[Message] | None:
    if isinstance(value, list) and all(isinstance(message, Message) for message in value):
        return cast("list[Message]", value)
    return None


def _request_tools(value: object) -> list[dict[str, _JSONValue]] | None:
    if isinstance(value, list) and all(isinstance(tool, dict) for tool in value):
        return cast("list[dict[str, _JSONValue]]", value)
    return None


def record_llm_request_tools(tools: object) -> None:
    """Record the final provider tools for the active request-log scope."""
    capture = _WIRE_TOOLS_CAPTURE.get()
    if capture is None or not capture.enabled:
        return
    capture.observed = True
    normalized_tools = _json_safe(tools)
    capture.tools = _request_tools(normalized_tools)


def _normalized_string_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in normalized:
            normalized.append(value)
    return normalized


def _snapshot_request_log_context() -> dict[str, _JSONValue]:
    """Return one detached copy of the currently bound request log context."""
    return cast("dict[str, _JSONValue]", _json_safe(_REQUEST_CONTEXT.get() or {}))


def current_llm_request_log_context() -> dict[str, _JSONValue]:
    """Return the current detached request-log context for cross-sink correlation."""
    return _snapshot_request_log_context()


def model_params_payload(model: Model) -> dict[str, _JSONValue]:
    """Return JSON-safe model parameters suitable for durable request metadata."""
    return _model_params(model)


def build_llm_request_log_context(
    *,
    agent_id: str,
    session_id: str,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    requester_id: str | None,
    correlation_id: str,
    prompt: str,
    model_prompt: str | None,
    full_prompt: str,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    """Build explicit per-request log context for one provider call."""
    context: dict[str, object] = {
        "agent_id": agent_id,
        "session_id": session_id,
        "correlation_id": correlation_id,
        "current_turn_prompt": prompt,
        "full_prompt": full_prompt,
    }
    if room_id:
        context["room_id"] = room_id
    if thread_id:
        context["thread_id"] = thread_id
    if reply_to_event_id:
        context["reply_to_event_id"] = reply_to_event_id
    if requester_id is not None:
        context["requester_id"] = requester_id
    if model_prompt is not None:
        context["model_prompt"] = model_prompt
    if not metadata:
        return context

    source_event_ids = _normalized_string_list(
        [
            reply_to_event_id,
            *_normalized_string_list(metadata.get(MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)),
        ],
    )
    if source_event_ids:
        context["source_event_ids"] = source_event_ids

    raw_prompt_map = metadata.get(MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY)
    if isinstance(raw_prompt_map, dict):
        source_event_prompts = {
            event_id: event_prompt
            for event_id, event_prompt in raw_prompt_map.items()
            if isinstance(event_id, str) and event_id and isinstance(event_prompt, str)
        }
        if source_event_prompts:
            context["source_event_prompts"] = source_event_prompts

    return context


@contextmanager
def bind_llm_request_log_context(**context: object) -> Iterator[None]:
    """Bind per-run request metadata so log entries can be attributed later."""
    existing_context = _REQUEST_CONTEXT.get() or {}
    bound_context = dict(existing_context)
    for key, value in context.items():
        if value is None:
            continue
        bound_context[str(key)] = _json_safe(value)
    token = _REQUEST_CONTEXT.set(bound_context or None)
    try:
        yield
    finally:
        _REQUEST_CONTEXT.reset(token)


@contextmanager
def _model_call_scope(model: Model) -> Iterator[None]:
    """Prevent one model method delegating to another from double-counting a request."""
    active_model_calls = _ACTIVE_MODEL_CALLS.get()
    token = _ACTIVE_MODEL_CALLS.set(active_model_calls | {id(model)})
    try:
        yield
    finally:
        _ACTIVE_MODEL_CALLS.reset(token)


@contextmanager
def _model_request_scope(model: Model, wire_tools_capture: _WireToolsCapture) -> Iterator[None]:
    """Bind one model invocation and its final wire-tools capture."""
    capture_token = _WIRE_TOOLS_CAPTURE.set(wire_tools_capture)
    try:
        with _model_call_scope(model):
            yield
    finally:
        _WIRE_TOOLS_CAPTURE.reset(capture_token)


def stream_with_llm_request_log_context[StreamEventT](
    stream_generator: AsyncIterator[StreamEventT],
    *,
    request_context: dict[str, object],
) -> AsyncIterator[StreamEventT]:
    """Advance one async stream with request-log context bound per item pull."""
    return context_bound_async_stream(
        context_factory=lambda: bind_llm_request_log_context(**request_context),
        stream_factory=stream_generator.__aiter__,
    )


@dataclass(frozen=True)
class _RequestLogRef:
    """Join key and destination file of one written request record.

    The response record reuses ``log_path`` so a request/response pair always
    lands in the same daily file, even when the response arrives after
    midnight — the offline review tool reads one file at a time.
    """

    request_log_id: str
    log_path: Path


async def _write_llm_request_log(
    *,
    model: Model,
    agent_name: str,
    messages: Sequence[Message],
    tools: list[dict[str, _JSONValue]] | None,
    log_path: Path,
    request_context: dict[str, _JSONValue] | None = None,
    request_log_id: str,
) -> None:
    """Persist one request record for an LLM invocation."""
    now = datetime.now().astimezone()
    resolved_request_context = request_context if request_context is not None else _snapshot_request_log_context()
    await asyncio.to_thread(
        _write_jsonl_line,
        log_path,
        {
            "timestamp": now.isoformat(),
            "request_log_id": request_log_id,
            "agent_id": agent_name,
            **resolved_request_context,
            "model_id": model.id,
            "system_prompt": _system_prompt(messages, model),
            "messages": _request_message_payloads(messages),
            "message_count": len(messages),
            "tools": _json_safe(tools),
            "tool_count": len(tools or []),
            "model_params": model_params_payload(model),
        },
    )


def _usage_payload(usage: MessageMetrics) -> dict[str, _JSONValue]:
    """Return the token counts of one provider response as a JSON payload."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_tokens": usage.cache_write_tokens,
    }


def _log_llm_usage(
    *,
    model: Model,
    model_name: str,
    configured_provider: str | None,
    usage: MessageMetrics | None,
    request_context: dict[str, _JSONValue],
) -> None:
    """Emit privacy-safe token and cache telemetry for one provider response."""
    payload: dict[str, _JSONValue] = {
        "model_name": model_name,
        "model_id": model.id,
        "provider": model.provider or configured_provider,
        "usage_available": usage is not None,
    }
    correlation_id = request_context.get("correlation_id")
    if correlation_id is not None:
        payload["correlation_id"] = correlation_id
    if usage is None:
        logger.info("LLM usage", **payload)
        return
    context_input_tokens = context_input_tokens_from_counts(
        input_tokens=usage.input_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        provider=model.provider,
        configured_provider=configured_provider,
        model_id=model.id,
    )
    uncached_input_tokens = context_input_tokens - usage.cache_read_tokens if context_input_tokens is not None else None
    cache_read_ratio = (
        usage.cache_read_tokens / context_input_tokens
        if context_input_tokens is not None and context_input_tokens > 0
        else 0.0
    )
    payload.update(
        {
            "input_tokens": usage.input_tokens,
            "context_input_tokens": context_input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "uncached_input_tokens": uncached_input_tokens,
            "cache_read_ratio": round(cache_read_ratio, 6),
        },
    )
    logger.info("LLM usage", **payload)


async def _write_llm_response_log(
    *,
    model: Model,
    agent_name: str,
    configured_provider: str | None = None,
    request_log_ref: _RequestLogRef | None,
    usage: MessageMetrics | None,
    request_context: dict[str, _JSONValue],
) -> None:
    """Emit usage telemetry and optionally persist the provider-reported usage.

    Durable logging is skipped when no request record exists.
    """
    _log_llm_usage(
        model=model,
        model_name=agent_name,
        configured_provider=configured_provider,
        usage=usage,
        request_context=request_context,
    )
    if request_log_ref is None or usage is None:
        return
    now = datetime.now().astimezone()
    payload: dict[str, _JSONValue] = {
        "timestamp": now.isoformat(),
        "record": "response",
        "request_log_id": request_log_ref.request_log_id,
        "agent_id": agent_name,
        "model_id": model.id,
        "usage": _usage_payload(usage),
    }
    correlation_id = request_context.get("correlation_id")
    if correlation_id is not None:
        payload["correlation_id"] = correlation_id
    await asyncio.to_thread(_write_jsonl_line, request_log_ref.log_path, payload)


async def _write_llm_request_log_if_present(
    *,
    model: Model,
    agent_name: str,
    kwargs: dict[str, object],
    log_dir: str | None,
    default_log_dir: Path,
    request_context: dict[str, _JSONValue],
    wire_tools_capture: _WireToolsCapture,
) -> _RequestLogRef | None:
    """Write one request log entry when provider kwargs include API request messages.

    Returns the written record's join key and file so the matching response
    record can be joined to it later, in the same daily file.
    """
    messages = _request_messages(kwargs.get("messages"))
    if messages is None:
        return None
    request_log_ref = _RequestLogRef(
        request_log_id=uuid4().hex,
        log_path=_daily_log_path(log_dir, default_log_dir, datetime.now().astimezone()),
    )
    await _write_llm_request_log(
        model=model,
        agent_name=agent_name,
        messages=messages,
        tools=(wire_tools_capture.tools if wire_tools_capture.observed else _request_tools(kwargs.get("tools"))),
        log_path=request_log_ref.log_path,
        request_context=request_context,
        request_log_id=request_log_ref.request_log_id,
    )
    return request_log_ref


async def _write_llm_request_log_if_enabled(
    *,
    model: Model,
    agent_name: str,
    kwargs: dict[str, object],
    debug_config: DebugConfig,
    default_log_dir: Path,
    request_context: dict[str, _JSONValue],
    wire_tools_capture: _WireToolsCapture,
) -> _RequestLogRef | None:
    """Write the sensitive request record only when explicitly enabled."""
    if not debug_config.log_llm_requests:
        return None
    return await _write_llm_request_log_if_present(
        model=model,
        agent_name=agent_name,
        kwargs=kwargs,
        log_dir=debug_config.llm_request_log_dir,
        default_log_dir=default_log_dir,
        request_context=request_context,
        wire_tools_capture=wire_tools_capture,
    )


async def _write_llm_request_log_best_effort(
    *,
    model: Model,
    agent_name: str,
    kwargs: dict[str, object],
    debug_config: DebugConfig,
    default_log_dir: Path,
    request_context: dict[str, _JSONValue],
    wire_tools_capture: _WireToolsCapture,
) -> _RequestLogRef | None:
    """Persist an optional request record without changing model-call behavior."""
    try:
        return await _write_llm_request_log_if_enabled(
            model=model,
            agent_name=agent_name,
            kwargs=kwargs,
            debug_config=debug_config,
            default_log_dir=default_log_dir,
            request_context=request_context,
            wire_tools_capture=wire_tools_capture,
        )
    except Exception:
        logger.exception(
            "Failed to persist LLM request log",
            agent_name=agent_name,
            model_id=model.id,
        )
        return None


async def _write_llm_response_log_best_effort(
    *,
    model: Model,
    agent_name: str,
    configured_provider: str | None,
    request_log_ref: _RequestLogRef | None,
    usage: MessageMetrics | None,
    request_context: dict[str, _JSONValue],
) -> None:
    """Emit usage and optional response records without affecting the model call."""
    try:
        await _write_llm_response_log(
            model=model,
            agent_name=agent_name,
            configured_provider=configured_provider,
            request_log_ref=request_log_ref,
            usage=usage,
            request_context=request_context,
        )
    except Exception:
        logger.exception(
            "Failed to emit LLM response telemetry",
            agent_name=agent_name,
            model_id=model.id,
        )


async def _invoke_with_llm_request_logging(
    *,
    model: Model,
    original_ainvoke: Callable[..., Coroutine[object, object, ModelResponse]],
    args: tuple[object, ...],
    kwargs: dict[str, object],
    agent_name: str,
    debug_config: DebugConfig,
    default_log_dir: Path,
    configured_provider: str | None,
    request_context: dict[str, _JSONValue],
) -> ModelResponse:
    """Invoke one model and persist the request after wire tools are known."""
    wire_tools_capture = _WireToolsCapture(enabled=debug_config.log_llm_requests)
    try:
        with _model_request_scope(model, wire_tools_capture):
            response = await original_ainvoke(*args, **kwargs)
    finally:
        request_log_ref = await _write_llm_request_log_best_effort(
            model=model,
            agent_name=agent_name,
            kwargs=kwargs,
            debug_config=debug_config,
            default_log_dir=default_log_dir,
            request_context=request_context,
            wire_tools_capture=wire_tools_capture,
        )
    await _write_llm_response_log_best_effort(
        model=model,
        agent_name=agent_name,
        configured_provider=configured_provider,
        request_log_ref=request_log_ref,
        usage=response.response_usage,
        request_context=request_context,
    )
    return response


def _stream_with_llm_request_logging(
    *,
    model: Model,
    original_ainvoke_stream: Callable[..., AsyncIterator[ModelResponse]],
    args: tuple[object, ...],
    kwargs: dict[str, object],
    agent_name: str,
    debug_config: DebugConfig,
    default_log_dir: Path,
    configured_provider: str | None,
    request_context: dict[str, _JSONValue],
) -> AsyncIterator[ModelResponse]:
    """Stream one model response and persist its final wire tools once."""

    async def _stream() -> AsyncIterator[ModelResponse]:
        wire_tools_capture = _WireToolsCapture(enabled=debug_config.log_llm_requests)
        request_log_ref: _RequestLogRef | None = None
        request_logged = False
        last_usage: MessageMetrics | None = None

        async def _write_request_once() -> None:
            nonlocal request_log_ref, request_logged
            if request_logged:
                return
            request_log_ref = await _write_llm_request_log_best_effort(
                model=model,
                agent_name=agent_name,
                kwargs=kwargs,
                debug_config=debug_config,
                default_log_dir=default_log_dir,
                request_context=request_context,
                wire_tools_capture=wire_tools_capture,
            )
            request_logged = True

        # finally: an abandoned stream (consumer break -> aclose()) must
        # still record any usage already reported; awaiting during aclose()
        # is allowed for async generators as long as nothing is yielded.
        try:
            scoped_stream = context_bound_async_stream(
                context_factory=lambda: _model_request_scope(model, wire_tools_capture),
                stream_factory=lambda: original_ainvoke_stream(*args, **kwargs),
            )
            async for chunk in scoped_stream:
                await _write_request_once()
                if chunk.response_usage is not None:
                    last_usage = chunk.response_usage
                yield chunk
        finally:
            await _write_request_once()
            await _write_llm_response_log_best_effort(
                model=model,
                agent_name=agent_name,
                configured_provider=configured_provider,
                request_log_ref=request_log_ref,
                usage=last_usage,
                request_context=request_context,
            )

    return _stream()


def install_llm_request_logging(
    model: Model,
    *,
    agent_name: str,
    debug_config: DebugConfig,
    default_log_dir: Path,
    configured_provider: str | None = None,
) -> None:
    """Wrap one model for usage telemetry and optional full request logging."""
    model_dict = vars(model)
    if model_dict.get(_INSTALLED_ATTR) is True:
        return

    original_ainvoke = model.ainvoke
    original_ainvoke_stream = model.ainvoke_stream

    def _logged_ainvoke(*args: object, **kwargs: object) -> Coroutine[object, object, ModelResponse]:
        if id(model) in _ACTIVE_MODEL_CALLS.get():
            return original_ainvoke(*args, **kwargs)
        return _invoke_with_llm_request_logging(
            model=model,
            original_ainvoke=original_ainvoke,
            args=args,
            kwargs=kwargs,
            agent_name=agent_name,
            debug_config=debug_config,
            default_log_dir=default_log_dir,
            configured_provider=configured_provider,
            request_context=_snapshot_request_log_context(),
        )

    def _logged_ainvoke_stream(*args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        if id(model) in _ACTIVE_MODEL_CALLS.get():
            return original_ainvoke_stream(*args, **kwargs)
        return _stream_with_llm_request_logging(
            model=model,
            original_ainvoke_stream=original_ainvoke_stream,
            args=args,
            kwargs=kwargs,
            agent_name=agent_name,
            debug_config=debug_config,
            default_log_dir=default_log_dir,
            configured_provider=configured_provider,
            request_context=_snapshot_request_log_context(),
        )

    model_dict["ainvoke"] = _logged_ainvoke
    model_dict["ainvoke_stream"] = _logged_ainvoke_stream
    model_dict[_INSTALLED_ATTR] = True
