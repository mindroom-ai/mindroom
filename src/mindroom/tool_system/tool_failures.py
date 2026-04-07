"""Durable, sanitized logging for tool-call failures."""

from __future__ import annotations

import json
import logging
import math
import re
import traceback
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from threading import Lock
from typing import TYPE_CHECKING, TypedDict, cast
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from mindroom.constants import tracking_dir
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_REDACTED = "***redacted***"
_TRUNCATED = "... [truncated]"
_MAX_STRING_LENGTH = 2048
_MAX_TRACEBACK_LENGTH = 4096
_MAX_COLLECTION_ITEMS = 25
_MAX_REDACTION_DEPTH = 6
_FAILURE_LOG_MAX_BYTES = 10 * 1024 * 1024
_FAILURE_LOG_BACKUPS = 5
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")
_BEARER_TOKEN_PATTERN = re.compile(
    r"(?P<prefix>(?:authorization(?:\s+header)?(?:\s*:)?\s+)?bearer(?:\s+token)?\s+)"
    r"(?P<token>[A-Za-z0-9._~+/=-]+)",
    re.IGNORECASE,
)
_API_KEY_MESSAGE_PATTERN = re.compile(
    r"(?P<prefix>(?:(?:incorrect|invalid)\s+api\s+key(?:\s+provided)?|api\s+key(?:\s+provided)?)"
    r"(?::\s*|\s+))(?P<token>[A-Za-z0-9._~+/=-]+)",
    re.IGNORECASE,
)
_TOKEN_LIKE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<token>("
    r"(?:sk|pk)-[A-Za-z0-9._-]+"
    r"|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9._-]+"
    r"|xox[baprs]-[A-Za-z0-9-]+"
    r"|gh(?:p|o|u|s|r)_[A-Za-z0-9_]+"
    r"|github_pat_[A-Za-z0-9_]+"
    r"|AIza[0-9A-Za-z_-]+"
    r"))(?![A-Za-z0-9])",
)
_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "password",
        "refresh_token",
        "secret",
        "token",
    },
)
_SECRET_KEYS_SORTED: tuple[str, ...] = tuple(sorted(_SECRET_KEYS, key=lambda secret_key: len(secret_key), reverse=True))
_SECRET_KEY_VARIANTS: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
    (key, key.replace("_", ""), tuple(key.split("_"))) for key in _SECRET_KEYS_SORTED
)
_URL_QUERY_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "sig",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
    },
)
_NEXT_ASSIGNMENT_PATTERN = r"\s+[\"']?[A-Za-z0-9_.-]+[\"']?\s*[:=]"
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<prefix>[\"']?(?P<key>[A-Za-z0-9_.-]+)[\"']?\s*[:=]\s*)"
    rf"(?:(?P<quote>[\"'])(?P<quoted_value>.*?)(?P=quote)|(?P<value>.+?))"
    rf"(?=(?:{_NEXT_ASSIGNMENT_PATTERN})|[\r\n,&)\]}}]|$)",
    re.IGNORECASE,
)
_FAILURE_LOGGERS: dict[Path, logging.Logger] = {}
_FAILURE_LOGGER_LOCK = Lock()

type JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class ToolFailureRecordDict(TypedDict):
    """JSON-serializable schema for one persisted tool failure record."""

    timestamp: str
    tool_name: str
    agent_name: str | None
    channel: str | None
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    session_id: str | None
    correlation_id: str
    duration_ms: float
    arguments: JsonValue
    error_type: str
    error_message: str
    traceback: str


@dataclass(frozen=True, slots=True)
class ToolFailureRecord:
    """One sanitized tool failure record ready for warning logs and JSONL persistence."""

    timestamp: str
    tool_name: str
    agent_name: str | None
    channel: str | None
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    session_id: str | None
    correlation_id: str
    duration_ms: float
    arguments: JsonValue
    error_type: str
    error_message: str
    traceback: str

    def as_dict(self) -> ToolFailureRecordDict:
        """Return the record in JSON-serializable dictionary form."""
        return cast(ToolFailureRecordDict, {field.name: getattr(self, field.name) for field in fields(self)})


def _unrepresentable_placeholder(value: object) -> str:
    return f"<unrepresentable: {type(value).__name__}>"


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except Exception:
        return _unrepresentable_placeholder(value)


def _safe_repr(value: object) -> str:
    try:
        return repr(value)
    except Exception:
        return _unrepresentable_placeholder(value)


def _normalize_secret_key(value: object) -> str:
    value = _safe_str(value)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value.strip())
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _is_secret_key(value: object) -> bool:
    normalized = _normalize_secret_key(value)
    parts = tuple(part for part in normalized.split("_") if part)
    compact = normalized.replace("_", "")
    for key, compact_key, key_parts in _SECRET_KEY_VARIANTS:
        if (
            normalized == key
            or normalized.endswith(f"_{key}")
            or compact == compact_key
            or compact.endswith(compact_key)
        ):
            return True
        for start in range(len(parts) - len(key_parts) + 1):
            if parts[start : start + len(key_parts)] == key_parts:
                return True
    return False


def _is_secret_query_key(value: object) -> bool:
    normalized = _normalize_secret_key(value)
    return normalized in _URL_QUERY_SECRET_KEYS or _is_secret_key(value)


def _redact_secret_assignment(match: re.Match[str]) -> str:
    prefix = match.group("prefix")
    if not _is_secret_key(match.group("key")):
        quote = match.group("quote")
        if quote is not None:
            quoted_value = match.group("quoted_value")
            if quoted_value is None:
                return match.group(0)
            return f"{prefix}{quote}{sanitize_failure_text(quoted_value)}{quote}"
        value = match.group("value")
        if value is None:
            return match.group(0)
        return prefix + sanitize_failure_text(value)
    quote = match.group("quote")
    if quote is not None:
        return f"{prefix}{quote}{_REDACTED}{quote}"
    return prefix + _REDACTED


def _truncate_text(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - len(_TRUNCATED)] + _TRUNCATED


def _redact_matched_group(match: re.Match[str], group_name: str = "token") -> str:
    group_start, group_end = match.span(group_name)
    full_match = match.group(0)
    prefix_end = group_start - match.start()
    suffix_start = group_end - match.start()
    return full_match[:prefix_end] + _REDACTED + full_match[suffix_start:]


def _redact_url_credentials(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return value

    netloc = parsed.netloc
    query = parsed.query
    changed = False

    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        netloc = f"{userinfo.split(':', 1)[0]}:***@{host}" if ":" in userinfo else f"***@{host}"
        changed = True

    if query:
        redacted_query_items: list[tuple[str, str]] = []
        query_changed = False
        for key, item in parse_qsl(query, keep_blank_values=True):
            if _is_secret_query_key(key):
                redacted_query_items.append((key, _REDACTED))
                query_changed = True
                continue
            redacted_query_items.append((key, item))
        if query_changed:
            query = urlencode(redacted_query_items, doseq=True, safe="*")
            changed = True

    if not changed:
        return value
    return urlunparse(parsed._replace(netloc=netloc, query=query))


def sanitize_failure_text(value: str, *, max_length: int = _MAX_STRING_LENGTH) -> str:
    """Redact common secret-bearing text patterns from one failure payload."""
    sanitized = _URL_PATTERN.sub(lambda match: _redact_url_credentials(match.group(0)), value)
    sanitized = _BEARER_TOKEN_PATTERN.sub(_redact_matched_group, sanitized)
    sanitized = _API_KEY_MESSAGE_PATTERN.sub(_redact_matched_group, sanitized)
    sanitized = _TOKEN_LIKE_PATTERN.sub(_redact_matched_group, sanitized)
    sanitized = _SECRET_ASSIGNMENT_PATTERN.sub(_redact_secret_assignment, sanitized)
    return _truncate_text(sanitized, max_length)


def sanitize_failure_value(value: object, *, depth: int = 0) -> JsonValue:
    """Recursively redact and bound one arbitrary value for durable failure logging."""
    if depth >= _MAX_REDACTION_DEPTH:
        return _TRUNCATED
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return sanitize_failure_text(value)
    if isinstance(value, dict):
        sanitized: dict[str, JsonValue] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                sanitized["__truncated__"] = f"{len(value) - _MAX_COLLECTION_ITEMS} more items"
                break
            key_text = _safe_str(key)
            sanitized[key_text] = _REDACTED if _is_secret_key(key) else sanitize_failure_value(item, depth=depth + 1)
        return sanitized
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        sanitized_items = [sanitize_failure_value(item, depth=depth + 1) for item in items[:_MAX_COLLECTION_ITEMS]]
        if len(items) > _MAX_COLLECTION_ITEMS:
            sanitized_items.append(_TRUNCATED)
        return sanitized_items
    return sanitize_failure_text(_safe_repr(value))


def build_tool_failure_record(
    *,
    tool_name: str,
    arguments: dict[str, object],
    error: BaseException,
    duration_ms: float,
    agent_name: str | None,
    channel: str | None,
    room_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    correlation_id: str,
) -> ToolFailureRecord:
    """Build one sanitized durable record for a failing tool call."""
    return ToolFailureRecord(
        timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        tool_name=tool_name,
        agent_name=agent_name,
        channel=channel,
        room_id=room_id,
        thread_id=thread_id,
        requester_id=requester_id,
        session_id=session_id,
        correlation_id=correlation_id,
        duration_ms=round(duration_ms, 2),
        arguments=sanitize_failure_value(arguments),
        error_type=type(error).__name__,
        error_message=sanitize_failure_text(str(error)),
        traceback=sanitize_failure_text(
            "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            max_length=_MAX_TRACEBACK_LENGTH,
        ),
    )


def _failure_log_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / "tool_failures.jsonl"


def _failure_logger(path: Path) -> logging.Logger:
    with _FAILURE_LOGGER_LOCK:
        cached = _FAILURE_LOGGERS.get(path)
        if cached is not None:
            return cached
        path.parent.mkdir(parents=True, exist_ok=True)
        failure_logger = logging.getLogger(f"mindroom.tool_failures.{path}")
        failure_logger.handlers.clear()
        failure_logger.setLevel(logging.INFO)
        failure_logger.propagate = False
        handler = RotatingFileHandler(
            path,
            maxBytes=_FAILURE_LOG_MAX_BYTES,
            backupCount=_FAILURE_LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        failure_logger.addHandler(handler)
        _FAILURE_LOGGERS[path] = failure_logger
        return failure_logger


def _append_failure_record(record: ToolFailureRecord, runtime_paths: RuntimePaths) -> None:
    _failure_logger(_failure_log_path(runtime_paths)).info(json.dumps(record.as_dict(), sort_keys=True, allow_nan=False))


def record_tool_failure(
    *,
    tool_name: str,
    arguments: dict[str, object],
    error: BaseException,
    duration_ms: float,
    agent_name: str | None,
    room_id: str | None,
    thread_id: str | None,
    requester_id: str | None,
    session_id: str | None,
    correlation_id: str,
    execution_identity: ToolExecutionIdentity | None,
    runtime_paths: RuntimePaths | None,
) -> ToolFailureRecord:
    """Persist one sanitized tool failure record when runtime paths are available."""
    record = build_tool_failure_record(
        tool_name=tool_name,
        arguments=arguments,
        error=error,
        duration_ms=duration_ms,
        agent_name=agent_name,
        channel=execution_identity.channel if execution_identity is not None else None,
        room_id=room_id,
        thread_id=thread_id,
        requester_id=requester_id,
        session_id=session_id,
        correlation_id=correlation_id,
    )
    if runtime_paths is None:
        return record
    try:
        _append_failure_record(record, runtime_paths)
    except Exception:
        logger.exception(
            "Failed to persist tool failure record",
            tool_name=tool_name,
            correlation_id=correlation_id,
        )
    return record


def _reset_failure_loggers_for_tests() -> None:
    for failure_logger in _FAILURE_LOGGERS.values():
        for handler in list(failure_logger.handlers):
            handler.close()
            failure_logger.removeHandler(handler)
    _FAILURE_LOGGERS.clear()
