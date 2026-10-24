"""Token telemetry for every provider request, and the opt-in full request log.

Each request emits one ``LLM usage`` log line once it ends, completed or not, attributed to the turn
its request context names. With ``debug.log_llm_requests`` the request (messages, tools, model
settings) and its usage are appended to the day's ``llm-requests-YYYY-MM-DD.jsonl``, joined by
``request_log_id`` and attributed to the same turn.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.constants import llm_request_log_dir
from mindroom.llm.provider import ReportedUsage, TurnCompleted
from mindroom.llm.request_context import REQUEST_ATTRIBUTION_FIELDS, current_llm_request_log_context, json_safe
from mindroom.logging_config import get_logger
from mindroom.model_usage import context_input_tokens_from_counts
from mindroom.redaction import redact_sensitive_data
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from mindroom.config.models import DebugConfig
    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["RequestLog", "logged_stream"]

logger = get_logger(__name__)

# Message fields the request record leaves out.
# TODO(simplify): the record leaves out `reasoning_content`, which the DeepSeek wire sends back and the Claude wire
# replays as a signed thinking block, so their records omit part of the request. Keep it in the record. Operators
# reading the request log would notice.
_NON_API_MESSAGE_FIELDS = frozenset(
    {"id", "reasoning_content", "tool_name", "tool_args", "tool_call_error", "stop_after_tool_call", "from_history"},
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestLog:
    """Where and how one model's requests are logged.

    ``model_name`` is the config key and ``configured_provider`` the authored ``provider`` value.
    """

    model_name: str
    configured_provider: str
    debug: DebugConfig
    storage_root: Path


def _attribution(context: Mapping[str, Any]) -> dict[str, Any]:
    """Return the fields of the request context that attribute a request to its turn, leaving its prompts out."""
    return {name: context[name] for name in REQUEST_ATTRIBUTION_FIELDS if name in context}


def _write_request_record(
    path: Path,
    request: ModelRequest,
    log: RequestLog,
    *,
    model_id: str,
    model_params: dict[str, Any],
    request_log_id: str,
    context: Mapping[str, Any],
) -> None:
    """Build and append the request record, in the thread that writes it.

    Serializing the whole conversation and tool schemas takes long enough to stall the event loop, so the record is
    built in the writer thread; the request waits for the write, so nothing edits its messages meanwhile.
    """
    messages = [
        {key: value for key, value in message.to_dict().items() if key not in _NON_API_MESSAGE_FIELDS}
        for message in request.messages
    ]
    tools = [json_safe(tool) for tool in request.tools]
    system_prompt = next((message.get_content_string() for message in request.messages if message.role == "system"), "")
    _write_line(
        path,
        {
            "timestamp": datetime.now().astimezone().isoformat(),
            "request_log_id": request_log_id,
            # TODO(simplify): A request whose log context binds no `agent_id` records the model config name as its
            # `agent_id`, here and in the response record. Record that name as `model_name` and take `agent_id` only
            # from the context. Operators reading the request log would notice.
            "agent_id": log.model_name,
            **context,
            "model_id": model_id,
            "system_prompt": system_prompt,
            "messages": json_safe(messages),
            "message_count": len(messages),
            "tools": tools,
            "tool_count": len(tools),
            "deferred_tool_names": sorted(request.deferred_tools.names),
            "model_params": model_params,
        },
    )


def _daily_log_path(log_dir: str | None, storage_root: Path, now: datetime) -> Path:
    """Return the day's request-log file in the request-log directory."""
    return llm_request_log_dir(log_dir, storage_root) / f"llm-requests-{now.date().isoformat()}.jsonl"


def _write_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # One write per record: with a separate newline write, a large record reaches the file in more than one
    # write(2) (text chunks of about 8 KiB before Python 3.14, the 128 KiB buffer on 3.14), so another model call's
    # record could land between them on the same line. Each record gets its own O_APPEND handle, and on a local
    # file system the kernel appends each write(2) whole, so one write per record keeps every record on its own line.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redact_sensitive_data(payload)) + "\n")


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
    payload |= _attribution(context)
    if usage is not None:
        counts = usage.tokens
        context_tokens = context_input_tokens_from_counts(
            input_tokens=counts.input_tokens,
            cache_read_tokens=counts.cache_read_tokens,
            cache_write_tokens=counts.cache_write_tokens,
            provider=identity.provider,
            configured_provider=log.configured_provider,
            model_id=identity.id,
        )
        payload |= {
            "input_tokens": counts.input_tokens,
            "context_input_tokens": context_tokens,
            "output_tokens": counts.output_tokens,
            "reasoning_tokens": counts.reasoning_tokens,
            "cache_read_tokens": counts.cache_read_tokens,
            "cache_write_tokens": counts.cache_write_tokens,
            "uncached_input_tokens": context_tokens - counts.cache_read_tokens if context_tokens is not None else None,
            "cache_read_ratio": round(counts.cache_read_tokens / context_tokens, 6) if context_tokens else 0.0,
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
        log_path = _daily_log_path(log.debug.llm_request_log_dir, log.storage_root, datetime.now().astimezone())
        try:
            await asyncio.to_thread(
                _write_request_record,
                log_path,
                request,
                log,
                model_id=inner.identity.id,
                model_params=inner.model_params,
                request_log_id=request_log_id,
                context=context,
            )
        except Exception:
            logger.exception("Failed to persist LLM request log", agent_name=log.model_name, model_id=inner.identity.id)
            log_path = None
    reported = ReportedUsage()
    served_provider: str | None = None
    try:
        events = inner.stream(request)
        async with closing_async_stream(events):
            async for event in events:
                reported.observe(event)
                if isinstance(event, TurnCompleted):
                    served_provider = _served_provider(event.message.provider_data)
                yield event
    finally:
        usage = reported.received
        try:
            _log_usage(inner, log, usage, served_provider, context)
            if log_path is not None and usage is not None:
                response: dict[str, Any] = {
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "record": "response",
                    "request_log_id": request_log_id,
                    "agent_id": log.model_name,
                    **_attribution(context),
                    "model_id": inner.identity.id,
                    "usage": {
                        "input_tokens": usage.tokens.input_tokens,
                        "output_tokens": usage.tokens.output_tokens,
                        "cache_read_tokens": usage.tokens.cache_read_tokens,
                        "cache_write_tokens": usage.tokens.cache_write_tokens,
                    },
                }
                await asyncio.to_thread(_write_line, log_path, response)
        except Exception:
            logger.exception(
                "Failed to emit LLM response telemetry",
                agent_name=log.model_name,
                model_id=inner.identity.id,
            )
