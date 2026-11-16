"""Token telemetry for every provider request, and the opt-in full request log.

Each request emits one ``LLM usage`` log line once it ends, completed or not, attributed to the turn
its request context names. With ``debug.log_llm_requests`` the body each route hands its SDK
(``ModelRequest.on_send``), the model settings, and the request's usage are appended to the day's
``llm-requests-YYYY-MM-DD.jsonl``, joined by ``request_log_id`` and attributed to the same turn. A request that never
reaches its SDK, such as one whose attachment cannot be read, logs no record.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.constants import llm_request_log_dir
from mindroom.llm.provider import ReportedUsage, TurnCompleted
from mindroom.llm.request_context import REQUEST_ATTRIBUTION_FIELDS, current_llm_request_log_context, json_safe
from mindroom.logging_config import get_logger
from mindroom.model_usage import metrics_context_tokens
from mindroom.redaction import redact_sensitive_data
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from mindroom.config.models import DebugConfig
    from mindroom.llm.provider import ModelProvider, ModelRequest, StopReason, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["RequestLog", "logged_stream"]

logger = get_logger(__name__)


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
    body: Mapping[str, Any],
    log: RequestLog,
    *,
    start: threading.Event,
    model_id: str,
    model_params: dict[str, Any],
    deferred_tool_names: list[str],
    request_log_id: str,
    context: Mapping[str, Any],
) -> Path | None:
    """Build and append the request record, in the thread that writes it; return its file, or None if it failed.

    Serializing the whole conversation and tool schemas takes long enough to stall the event loop, so the record is
    built in the writer thread, and long enough to delay the request, so the adapter sends the body meanwhile. It holds
    the GIL while it runs, which would slow the SDK's own serialization of the request on the loop, so it waits for
    ``start``, set once the request's task first waits, normally on the provider. Adapters and their SDKs only read the
    body they hand ``on_send``, and the caller sees nothing of the reply before the write ends, so nothing edits the
    body while it is read here.
    """
    start.wait()
    try:
        _write_line(
            path,
            {
                "timestamp": datetime.now().astimezone().isoformat(),
                "request_log_id": request_log_id,
                **context,
                "model_name": log.model_name,
                "model_id": model_id,
                "body": json_safe(body),
                "deferred_tool_names": deferred_tool_names,
                "model_params": model_params,
            },
        )
    except Exception:
        logger.exception("Failed to persist LLM request log", model_name=log.model_name, model_id=model_id)
        return None
    return path


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
    stop_reason: StopReason | None,
    context: Mapping[str, Any],
) -> None:
    identity = inner.identity
    payload: dict[str, Any] = {
        "model_name": log.model_name,
        "model_id": identity.id,
        "provider": identity.provider,
        "usage_available": usage is not None,
    }
    if served_provider is not None:
        payload["served_provider"] = served_provider
    if stop_reason is not None:
        payload["stop_reason"] = stop_reason
    payload |= _attribution(context)
    if usage is not None:
        counts = usage.tokens
        # The context fields describe what the request held, which after a Claude native compaction is only the final
        # iteration; the counters bill every iteration.
        held = metrics_context_tokens(
            usage,
            provider=identity.provider,
            configured_provider=log.configured_provider,
            model_id=identity.id,
        )
        payload |= {
            "input_tokens": counts.input_tokens,
            "context_input_tokens": held.tokens if held is not None else None,
            "output_tokens": counts.output_tokens,
            "reasoning_tokens": counts.reasoning_tokens,
            "cache_read_tokens": counts.cache_read_tokens,
            "cache_write_tokens": counts.cache_write_tokens,
            "uncached_input_tokens": held.uncached_input_tokens if held is not None else None,
            "cache_read_ratio": round(held.cache_read_input_tokens / held.tokens, 6) if held is not None else 0.0,
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
    request_log_id = uuid4().hex
    # The request record's write, handed to a worker thread as the adapter sends the request; it gives the log file once
    # the record is written, so the response record joins only a logged request. It is no task and every await shields
    # it, so neither a cancelled caller nor the loop's shutdown drops it: the loop's executor finishes it first.
    record: asyncio.Future[Path | None] | None = None
    if log.debug.log_llm_requests:
        log_path = _daily_log_path(log.debug.llm_request_log_dir, log.storage_root, datetime.now().astimezone())
        deferred_tool_names = sorted(request.deferred_tools.names)

        async def write_request_record(body: Mapping[str, Any]) -> None:
            nonlocal record
            loop = asyncio.get_running_loop()
            start = threading.Event()
            write = partial(
                _write_request_record,
                log_path,
                body,
                log,
                start=start,
                model_id=inner.identity.id,
                model_params=inner.model_params,
                deferred_tool_names=deferred_tool_names,
                request_log_id=request_log_id,
                context=context,
            )
            # The copied context keeps the request's log fields on a failure the thread reports.
            record = loop.run_in_executor(None, contextvars.copy_context().run, write)
            loop.call_soon(start.set)

        request = replace(request, on_send=write_request_record)
    reported = ReportedUsage()
    served_provider: str | None = None
    stop_reason: StopReason | None = None
    try:
        events = inner.stream(request)
        async with closing_async_stream(events):
            async for event in events:
                if record is not None:
                    # The caller sees the reply only once the request's record is written.
                    await asyncio.shield(record)
                reported.observe(event)
                if isinstance(event, TurnCompleted):
                    served_provider = _served_provider(event.message.provider_data)
                    stop_reason = event.stop_reason
                yield event
    finally:
        # A report whose counters are all zero is one the provider did not make.
        usage = received if (received := reported.received) is not None and received.reported else None
        try:
            _log_usage(inner, log, usage, served_provider, stop_reason, context)
            logged_path = None if record is None else await asyncio.shield(record)
            if logged_path is not None and usage is not None:
                response: dict[str, Any] = {
                    "timestamp": datetime.now().astimezone().isoformat(),
                    "record": "response",
                    "request_log_id": request_log_id,
                    **_attribution(context),
                    "model_name": log.model_name,
                    "model_id": inner.identity.id,
                    "usage": {
                        "input_tokens": usage.tokens.input_tokens,
                        "output_tokens": usage.tokens.output_tokens,
                        "cache_read_tokens": usage.tokens.cache_read_tokens,
                        "cache_write_tokens": usage.tokens.cache_write_tokens,
                    },
                }
                await asyncio.to_thread(_write_line, logged_path, response)
        except Exception:
            logger.exception(
                "Failed to emit LLM response telemetry",
                model_name=log.model_name,
                model_id=inner.identity.id,
            )
