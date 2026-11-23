"""Token telemetry for every provider request, and the opt-in full request log.

Each request emits one ``LLM usage`` log line once it ends, completed or not, attributed to the turn
its request context names. With ``debug.log_llm_requests`` the body each route hands its SDK
(``ModelRequest.on_send``), the model settings, and the request's usage are appended to the day's
``llm-requests-YYYY-MM-DD.jsonl``, joined by ``request_log_id`` and attributed to the same turn. A request that never
reaches its SDK, such as one whose attachment cannot be read, logs no record.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.constants import llm_request_log_dir
from mindroom.llm.provider import ReportedUsage, TurnCompleted
from mindroom.llm.request_context import REQUEST_ATTRIBUTION_FIELDS, current_llm_request_log_context, json_safe
from mindroom.logging_config import get_logger
from mindroom.model_usage import metrics_context_tokens
from mindroom.providers.names import ROUTE_PROVIDERS, is_openrouter_gemini_model
from mindroom.redaction import redact_sensitive_data
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence
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


_WRAPPED_TOOL_RESULT_KEYS = frozenset({"result", "error"})


def _is_openrouter_gemini_request(provider: str, body: Mapping[str, Any], model_id: str) -> bool:
    """Return whether the adapter wrapped this body's tool results, by the ``model`` the body sends, even null or empty."""
    effective_model = body.get("model", model_id)
    return (
        provider == ROUTE_PROVIDERS["openrouter"]
        and isinstance(effective_model, str)
        and is_openrouter_gemini_model(effective_model)
    )


def _decode_wrapped_tool_text(text: str) -> dict[str, str] | None:
    """Return the decoded ``{"result": ...}`` or ``{"error": ...}`` dict for a wrapped tool string, else None."""
    if not text.startswith("{"):
        return None
    try:
        decoded = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(decoded, dict) or len(decoded) != 1:
        return None
    ((key, inner),) = decoded.items()
    if key not in _WRAPPED_TOOL_RESULT_KEYS or not isinstance(inner, str):
        return None
    return {key: inner}


def _unwrap_tool_results_for_redaction(body: Any) -> list[tuple[int, int | None]]:  # noqa: ANN401 - json_safe copy
    """Decode JSON-wrapped tool strings in a detached ``json_safe`` body copy so redaction runs once on plain text.

    OpenRouter Gemini tool results travel as JSON-encoded ``{"result": ...}`` / ``{"error": ...}`` strings whose
    escaped quotes and newlines hide or distort credential patterns during text redaction. Decoding before
    ``redact_sensitive_data`` and re-encoding afterwards redacts once without corrupting the logged JSON envelope.
    """
    if not isinstance(body, dict):
        return []
    messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    wrapped_positions: list[tuple[int, int | None]] = []
    for msg_idx, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, str):
            if (decoded := _decode_wrapped_tool_text(content)) is not None:
                message["content"] = decoded
                wrapped_positions.append((msg_idx, None))
        elif isinstance(content, list):
            for part_idx, part in enumerate(content):
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                    and (decoded := _decode_wrapped_tool_text(part["text"])) is not None
                ):
                    part["text"] = decoded
                    wrapped_positions.append((msg_idx, part_idx))
    return wrapped_positions


def _rewrap_redacted_tool_results(payload: Any, wrapped_positions: Sequence[tuple[int, int | None]]) -> None:  # noqa: ANN401
    """Re-encode decoded tool dicts in a redacted request record back into their wire JSON-string form."""
    if not wrapped_positions or not isinstance(payload, dict):
        return
    body = payload.get("body")
    if not isinstance(body, dict):
        return
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for msg_idx, part_idx in wrapped_positions:
        if msg_idx >= len(messages) or not isinstance(message := messages[msg_idx], dict):
            continue
        if part_idx is None:
            content = message.get("content")
            if isinstance(content, dict):
                message["content"] = json.dumps(content, ensure_ascii=False)
        else:
            content = message.get("content")
            if isinstance(content, list) and part_idx < len(content) and isinstance(part := content[part_idx], dict):
                text = part.get("text")
                if isinstance(text, dict):
                    part["text"] = json.dumps(text, ensure_ascii=False)


class _LoggedRequestRecord(dict[str, Any]):
    """Request-log dict carrying positions of decoded tool envelopes to re-encode after redaction.

    Carrying them on the record keeps ``_write_line(path, payload)`` at the two arguments test monkeypatches replace.
    """

    def __init__(
        self,
        data: Mapping[str, Any],
        *,
        wrapped_tool_positions: Sequence[tuple[int, int | None]],
    ) -> None:
        super().__init__(data)
        self.wrapped_tool_positions = tuple(wrapped_tool_positions)


def _write_request_record(
    path: Path,
    body: Mapping[str, Any],
    log: RequestLog,
    *,
    provider: str,
    model_id: str,
    model_params: dict[str, Any],
    deferred_tool_names: list[str],
    request_log_id: str,
    context: Mapping[str, Any],
) -> None:
    """Build and append the request record, in the thread that writes it.

    Serializing the whole conversation and tool schemas takes long enough to stall the event loop, so the record is
    built in the writer thread; the adapter sends the body only after the write, so nothing edits it meanwhile.
    """
    safe_body = json_safe(body)
    wrapped_positions = (
        _unwrap_tool_results_for_redaction(safe_body) if _is_openrouter_gemini_request(provider, body, model_id) else []
    )
    record: dict[str, Any] = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "request_log_id": request_log_id,
        **context,
        "model_name": log.model_name,
        "model_id": model_id,
        "body": safe_body,
        "deferred_tool_names": deferred_tool_names,
        "model_params": model_params,
    }
    if wrapped_positions:
        record = _LoggedRequestRecord(record, wrapped_tool_positions=wrapped_positions)
    _write_line(path, record)


def _daily_log_path(log_dir: str | None, storage_root: Path, now: datetime) -> Path:
    """Return the day's request-log file in the request-log directory."""
    return llm_request_log_dir(log_dir, storage_root) / f"llm-requests-{now.date().isoformat()}.jsonl"


def _write_line(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    redacted = redact_sensitive_data(payload)
    if isinstance(payload, _LoggedRequestRecord) and payload.wrapped_tool_positions:
        _rewrap_redacted_tool_results(redacted, payload.wrapped_tool_positions)
    # One write per record: with a separate newline write, a large record reaches the file in more than one
    # write(2) (text chunks of about 8 KiB before Python 3.14, the 128 KiB buffer on 3.14), so another model call's
    # record could land between them on the same line. Each record gets its own O_APPEND handle, and on a local
    # file system the kernel appends each write(2) whole, so one write per record keeps every record on its own line.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(redacted) + "\n")


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
    # Set once the request record is written, so the response record joins only a logged request.
    logged_path: Path | None = None
    if log.debug.log_llm_requests:
        log_path = _daily_log_path(log.debug.llm_request_log_dir, log.storage_root, datetime.now().astimezone())
        deferred_tool_names = sorted(request.deferred_tools.names)

        async def write_request_record(body: Mapping[str, Any]) -> None:
            nonlocal logged_path
            try:
                await asyncio.to_thread(
                    _write_request_record,
                    log_path,
                    body,
                    log,
                    provider=inner.identity.provider,
                    model_id=inner.identity.id,
                    model_params=inner.model_params,
                    deferred_tool_names=deferred_tool_names,
                    request_log_id=request_log_id,
                    context=context,
                )
            except Exception:
                logger.exception(
                    "Failed to persist LLM request log",
                    model_name=log.model_name,
                    model_id=inner.identity.id,
                )
            else:
                logged_path = log_path

        request = replace(request, on_send=write_request_record)
    reported = ReportedUsage()
    served_provider: str | None = None
    stop_reason: StopReason | None = None
    try:
        events = inner.stream(request)
        async with closing_async_stream(events):
            async for event in events:
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
