"""Durable, sanitized logging for tool calls."""

from __future__ import annotations

import json
import logging
import math
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from threading import Lock
from typing import TYPE_CHECKING, TypedDict, cast

from mindroom.constants import tracking_dir
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_data, redact_sensitive_text

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_TRUNCATED = "... [truncated]"
_MAX_STRING_LENGTH = 2048
_MAX_TRACEBACK_LENGTH = 4096
_MAX_COLLECTION_ITEMS = 25
_MAX_REDACTION_DEPTH = 6
_TOOL_CALL_LOG_MAX_BYTES = 10 * 1024 * 1024
_TOOL_CALL_LOG_BACKUPS = 5
_TOOL_CALL_LOGGERS: dict[Path, logging.Logger] = {}
_TOOL_CALL_LOGGER_LOCK = Lock()

type _JsonValue = None | bool | int | float | str | list["_JsonValue"] | dict[str, "_JsonValue"]


class _ToolCallRecordDict(TypedDict, total=False):
    """JSON-serializable schema for one persisted tool-call record."""

    timestamp: str
    tool_name: str
    agent_name: str | None
    channel: str | None
    room_id: str | None
    thread_id: str | None
    reply_to_event_id: str | None
    requester_id: str | None
    session_id: str | None
    correlation_id: str
    duration_ms: float
    arguments: _JsonValue
    success: bool
    result: _JsonValue
    error_type: str
    error_message: str
    traceback: str


@dataclass(frozen=True, slots=True)
class _ToolCallRecord:
    """One sanitized tool-call record ready for warning logs and JSONL persistence."""

    timestamp: str
    tool_name: str
    agent_name: str | None
    channel: str | None
    room_id: str | None
    thread_id: str | None
    reply_to_event_id: str | None
    requester_id: str | None
    session_id: str | None
    correlation_id: str
    duration_ms: float
    arguments: _JsonValue
    success: bool
    result: _JsonValue | None = None
    error_type: str | None = None
    error_message: str | None = None
    traceback: str | None = None

    def as_dict(self) -> _ToolCallRecordDict:
        """Return the record in JSON-serializable dictionary form."""
        record: dict[str, _JsonValue | str | float | bool | None] = {
            "timestamp": self.timestamp,
            "tool_name": self.tool_name,
            "reply_to_event_id": self.reply_to_event_id,
            "correlation_id": self.correlation_id,
            "duration_ms": self.duration_ms,
            "arguments": self.arguments,
            "success": self.success,
        }
        if self.agent_name is not None:
            record["agent_name"] = self.agent_name
        if self.channel is not None:
            record["channel"] = self.channel
        if self.room_id is not None:
            record["room_id"] = self.room_id
        if self.thread_id is not None:
            record["thread_id"] = self.thread_id
        if self.requester_id is not None:
            record["requester_id"] = self.requester_id
        if self.session_id is not None:
            record["session_id"] = self.session_id
        if self.success or self.result is not None:
            record["result"] = self.result
        optional_fields: tuple[tuple[str, str | None], ...] = (
            ("error_type", self.error_type),
            ("error_message", self.error_message),
            ("traceback", self.traceback),
        )
        record.update({key: value for key, value in optional_fields if value is not None})
        return cast("_ToolCallRecordDict", record)


def _unrepresentable_placeholder(value: object) -> str:
    return f"<unrepresentable: {type(value).__name__}>"


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except BaseException:
        return _unrepresentable_placeholder(value)


def sanitize_failure_text(value: str, *, max_length: int = _MAX_STRING_LENGTH) -> str:
    """Redact common secret-bearing text patterns from one failure payload."""
    return redact_sensitive_text(value, max_length=max_length)


def sanitize_failure_value(value: object, *, depth: int = 0) -> _JsonValue:
    """Recursively redact and bound one arbitrary value for durable failure logging."""
    max_depth = max(_MAX_REDACTION_DEPTH - depth, 0) if depth > 0 else _MAX_REDACTION_DEPTH
    return redact_sensitive_data(
        value,
        max_string_length=_MAX_STRING_LENGTH,
        max_collection_items=_MAX_COLLECTION_ITEMS,
        max_depth=max_depth,
    )


def _sanitize_duration_ms(duration_ms: float) -> float:
    if not math.isfinite(duration_ms):
        return 0.0
    return round(duration_ms, 2)


def _safe_error_message(error: BaseException) -> str:
    return sanitize_failure_text(_safe_str(error))


def _safe_traceback(error: BaseException) -> str:
    try:
        formatted_traceback = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    except BaseException:
        formatted_traceback = _unrepresentable_placeholder(error)
    return sanitize_failure_text(formatted_traceback, max_length=_MAX_TRACEBACK_LENGTH)


def _build_tool_failure_record(
    *,
    tool_name: str,
    arguments: dict[str, object],
    error: BaseException,
    duration_ms: float,
    agent_name: str | None,
    channel: str | None,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    correlation_id: str,
) -> _ToolCallRecord:
    """Build one sanitized durable record for a failing tool call."""
    return _ToolCallRecord(
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        tool_name=tool_name,
        agent_name=agent_name,
        channel=channel,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        requester_id=requester_id,
        session_id=session_id,
        correlation_id=correlation_id,
        duration_ms=_sanitize_duration_ms(duration_ms),
        arguments=sanitize_failure_value(arguments),
        success=False,
        error_type=type(error).__name__,
        error_message=_safe_error_message(error),
        traceback=_safe_traceback(error),
    )


def _build_tool_success_record(
    *,
    tool_name: str,
    arguments: dict[str, object],
    result: object,
    duration_ms: float,
    agent_name: str | None,
    channel: str | None,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    correlation_id: str,
) -> _ToolCallRecord:
    """Build one sanitized durable record for a successful tool call."""
    return _ToolCallRecord(
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        tool_name=tool_name,
        agent_name=agent_name,
        channel=channel,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        requester_id=requester_id,
        session_id=session_id,
        correlation_id=correlation_id,
        duration_ms=_sanitize_duration_ms(duration_ms),
        arguments=sanitize_failure_value(arguments),
        success=True,
        result=sanitize_failure_value(result),
    )


def _tool_call_log_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / "tool_calls.jsonl"


def _tool_call_logger(path: Path) -> logging.Logger:
    with _TOOL_CALL_LOGGER_LOCK:
        cached = _TOOL_CALL_LOGGERS.get(path)
        if cached is not None:
            return cached
        path.parent.mkdir(parents=True, exist_ok=True)
        tool_call_logger = logging.getLogger(f"mindroom.tool_calls.{path}")
        tool_call_logger.handlers.clear()
        tool_call_logger.setLevel(logging.INFO)
        tool_call_logger.propagate = False
        handler = RotatingFileHandler(
            path,
            maxBytes=_TOOL_CALL_LOG_MAX_BYTES,
            backupCount=_TOOL_CALL_LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        tool_call_logger.addHandler(handler)
        _TOOL_CALL_LOGGERS[path] = tool_call_logger
        return tool_call_logger


def _append_tool_call_record(record: _ToolCallRecord, runtime_paths: RuntimePaths) -> None:
    _tool_call_logger(_tool_call_log_path(runtime_paths)).info(
        json.dumps(record.as_dict(), sort_keys=True, allow_nan=False),
    )


def record_tool_failure(
    *,
    tool_name: str,
    arguments: dict[str, object],
    error: BaseException,
    duration_ms: float,
    agent_name: str | None,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    correlation_id: str,
    execution_identity: ToolExecutionIdentity | None,
    runtime_paths: RuntimePaths | None,
) -> _ToolCallRecord:
    """Persist one sanitized tool failure record when runtime paths are available."""
    record = _build_tool_failure_record(
        tool_name=tool_name,
        arguments=arguments,
        error=error,
        duration_ms=duration_ms,
        agent_name=agent_name,
        channel=execution_identity.channel if execution_identity is not None else None,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        requester_id=requester_id,
        session_id=session_id,
        correlation_id=correlation_id,
    )
    if runtime_paths is None:
        logger.debug(
            "Skipping tool failure persistence without runtime paths",
            tool_name=tool_name,
            correlation_id=correlation_id,
        )
        return record
    try:
        _append_tool_call_record(record, runtime_paths)
    except Exception:
        logger.exception(
            "Failed to persist tool failure record",
            tool_name=tool_name,
            correlation_id=correlation_id,
        )
    return record


def record_tool_success(
    *,
    tool_name: str,
    arguments: dict[str, object],
    result: object,
    duration_ms: float,
    agent_name: str | None,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    correlation_id: str,
    execution_identity: ToolExecutionIdentity | None,
    runtime_paths: RuntimePaths | None,
) -> _ToolCallRecord:
    """Persist one sanitized tool success record when runtime paths are available."""
    record = _build_tool_success_record(
        tool_name=tool_name,
        arguments=arguments,
        result=result,
        duration_ms=duration_ms,
        agent_name=agent_name,
        channel=execution_identity.channel if execution_identity is not None else None,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        requester_id=requester_id,
        session_id=session_id,
        correlation_id=correlation_id,
    )
    if runtime_paths is None:
        logger.debug(
            "Skipping tool success persistence without runtime paths",
            tool_name=tool_name,
            correlation_id=correlation_id,
        )
        return record
    try:
        _append_tool_call_record(record, runtime_paths)
    except Exception:
        logger.exception(
            "Failed to persist tool success record",
            tool_name=tool_name,
            correlation_id=correlation_id,
        )
    return record


def _reset_tool_call_loggers_for_tests() -> None:
    for tool_call_logger in _TOOL_CALL_LOGGERS.values():
        for handler in list(tool_call_logger.handlers):
            handler.close()
            tool_call_logger.removeHandler(handler)
    _TOOL_CALL_LOGGERS.clear()
