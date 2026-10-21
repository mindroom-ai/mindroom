"""Token telemetry for every provider request, and the opt-in full request log.

Each request emits one ``LLM usage`` log line once it ends, completed or not. With
``debug.log_llm_requests`` the request (messages, tools, model settings) and its usage are appended
to the day's ``llm-requests-YYYY-MM-DD.jsonl``, joined by ``request_log_id``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import uuid4

from mindroom.llm.provider import TurnCompleted, UsageReported
from mindroom.llm.request_context import current_llm_request_log_context, json_safe
from mindroom.logging_config import get_logger
from mindroom.model_usage import context_input_tokens_from_counts
from mindroom.providers.pipeline.wrapped import bare_adapter
from mindroom.redaction import redact_sensitive_data
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from mindroom.config.models import DebugConfig
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["RequestLog", "logged_stream", "model_params", "provider_model_params"]

logger = get_logger(__name__)

# Message fields that never reach a provider, left out of the request record.
_NON_API_MESSAGE_FIELDS = frozenset(
    {"id", "reasoning_content", "tool_name", "tool_args", "tool_call_error", "stop_after_tool_call", "from_history"},
)
# Settings that hold credentials or clients rather than request parameters.
_SECRET_SETTINGS = frozenset(
    {"api_key", "azure_ad_token", "azure_ad_token_provider", "client_params", "default_headers", "default_query"},
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestLog:
    """Where and how one model's requests are logged.

    ``model_name`` is the config key, ``configured_provider`` the authored ``provider`` value, and
    ``settings`` the route's parsed ``extra_kwargs`` dataclass, recorded as the request's model params.
    """

    model_name: str
    configured_provider: str
    debug: DebugConfig
    default_log_dir: Path
    settings: object | None = None


@runtime_checkable
class _RouteSettings(Protocol):
    """An adapter that carries its route's parsed ``extra_kwargs`` settings."""

    @property
    def settings(self) -> object: ...


def provider_model_params(provider: ModelProvider) -> dict[str, Any]:
    """Return the JSON-safe route settings of ``provider``'s adapter, as run metadata records them."""
    adapter = bare_adapter(provider)
    return model_params(adapter.settings) if isinstance(adapter, _RouteSettings) else {}


def model_params(settings: object) -> dict[str, Any]:
    """Return the JSON-safe, non-secret fields of a route's settings dataclass; anything else gives ``{}``."""
    if settings is None or isinstance(settings, type) or not is_dataclass(settings):
        return {}
    params: dict[str, Any] = {}
    for name, value in asdict(settings).items():
        if name in _SECRET_SETTINGS or value is None:
            continue
        try:
            json.dumps(value)
        except TypeError:
            continue
        params[name] = value
    return params


def _system_prompt(messages: Sequence[Message]) -> str:
    return next((message.get_content_string() for message in messages if message.role == "system"), "")


def _request_record(
    request: ModelRequest,
    log: RequestLog,
    *,
    model_id: str,
    request_log_id: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    messages = [
        {key: value for key, value in message.to_dict().items() if key not in _NON_API_MESSAGE_FIELDS}
        for message in request.messages
    ]
    tools = [*(json_safe(tool) for tool in request.native_tools), *(json_safe(tool) for tool in request.tools)]
    return {
        "timestamp": datetime.now().astimezone().isoformat(),
        "request_log_id": request_log_id,
        "agent_id": log.model_name,
        **context,
        "model_id": model_id,
        "system_prompt": _system_prompt(request.messages),
        "messages": json_safe(messages),
        "message_count": len(messages),
        "tools": tools,
        "tool_count": len(tools),
        "model_params": model_params(log.settings),
    }


def _write_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact_sensitive_data(payload)))
        handle.write("\n")


def _log_usage(
    inner: ModelProvider,
    log: RequestLog,
    usage: MessageMetrics | None,
    served_provider: str | None,
    context: Mapping[str, Any],
) -> None:
    identity = inner.identity
    payload: dict[str, Any] = {
        "model_name": log.model_name,
        "model_id": identity.id,
        "provider": identity.provider or log.configured_provider,
        "usage_available": usage is not None,
    }
    if served_provider is not None:
        payload["served_provider"] = served_provider
    if (correlation_id := context.get("correlation_id")) is not None:
        payload["correlation_id"] = correlation_id
    if usage is not None:
        context_tokens = context_input_tokens_from_counts(
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            provider=identity.provider,
            configured_provider=log.configured_provider,
            model_id=identity.id,
        )
        payload |= {
            "input_tokens": usage.input_tokens,
            "context_input_tokens": context_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "uncached_input_tokens": context_tokens - usage.cache_read_tokens if context_tokens is not None else None,
            "cache_read_ratio": round(usage.cache_read_tokens / context_tokens, 6) if context_tokens else 0.0,
        }
    logger.info("LLM usage", **payload)


def _served_provider(provider_data: Mapping[str, Any] | None) -> str | None:
    """Return the upstream OpenRouter reports serving one response, such as ``DeepSeek``."""
    model_extra = (provider_data or {}).get("model_extra")
    served = model_extra.get("provider") if isinstance(model_extra, dict) else None
    return served if isinstance(served, str) else None


async def logged_stream(inner: ModelProvider, request: ModelRequest, *, log: RequestLog) -> AsyncIterator[StreamEvent]:
    """Stream ``request``, emitting usage telemetry when it ends and the request record when enabled."""
    context = current_llm_request_log_context()
    log_path: Path | None = None
    request_log_id = uuid4().hex
    if log.debug.log_llm_requests:
        base_dir = Path(log.debug.llm_request_log_dir) if log.debug.llm_request_log_dir else log.default_log_dir
        log_path = base_dir / f"llm-requests-{datetime.now().astimezone().date().isoformat()}.jsonl"
        try:
            record = _request_record(
                request,
                log,
                model_id=inner.identity.id,
                request_log_id=request_log_id,
                context=context,
            )
            await asyncio.to_thread(_write_line, log_path, record)
        except Exception:
            logger.exception("Failed to persist LLM request log", agent_name=log.model_name, model_id=inner.identity.id)
            log_path = None
    usage: MessageMetrics | None = None
    served_provider: str | None = None
    try:
        events = inner.stream(request)
        async with closing_async_stream(events):
            async for event in events:
                if isinstance(event, UsageReported) and event.final:
                    usage = event.usage
                elif isinstance(event, TurnCompleted):
                    served_provider = _served_provider(event.message.provider_data)
                yield event
    finally:
        try:
            _log_usage(inner, log, usage, served_provider, context)
            if log_path is not None and usage is not None:
                response: dict[str, Any] = {
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "record": "response",
                    "request_log_id": request_log_id,
                    "agent_id": log.model_name,
                    "model_id": inner.identity.id,
                    "usage": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_read_tokens": usage.cache_read_tokens,
                        "cache_write_tokens": usage.cache_write_tokens,
                    },
                }
                if (correlation_id := context.get("correlation_id")) is not None:
                    response["correlation_id"] = correlation_id
                await asyncio.to_thread(_write_line, log_path, response)
        except Exception:
            logger.exception(
                "Failed to emit LLM response telemetry",
                agent_name=log.model_name,
                model_id=inner.identity.id,
            )
