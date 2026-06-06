"""Centralized credential redaction for logs and audit records."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel

REDACTED = "***redacted***"
__all__ = ["REDACTED", "redact_log_event", "redact_sensitive_data", "redact_sensitive_text"]
_TRUNCATED = "... [truncated]"
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
_NEXT_ASSIGNMENT_PATTERN = r"\s+(?:and\s+)?[\"']?[A-Za-z0-9_.-]+[\"']?\s*[:=]"
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<prefix>[\"']?(?P<key>[A-Za-z0-9_.-]+)[\"']?\s*[:=]\s*)"
    rf"(?:(?P<quote>[\"'])(?P<quoted_value>.*?)(?P=quote)|(?P<value>.+?))"
    rf"(?=(?:{_NEXT_ASSIGNMENT_PATTERN})|[\r\n,&)\]}}]|$)",
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
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
    },
)
_OAUTH_QUERY_KEYS: frozenset[str] = frozenset({"code", "state"})
_URL_QUERY_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "aws_access_key_id",
        "awsaccesskeyid",
        "google_access_id",
        "googleaccessid",
        "sig",
        "signature",
        "x_amz_credential",
        "x_amz_security_token",
        "x_amz_signature",
        "x_goog_credential",
        "x_goog_signature",
    },
)
_QUERY_CONTAINER_KEYS: frozenset[str] = frozenset({"query", "query_params", "query_string", "callback_query"})
_SECRET_KEYS_SORTED = cast("tuple[str, ...]", tuple(sorted(_SECRET_KEYS, key=len, reverse=True)))
_SECRET_KEY_VARIANTS: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
    (key, key.replace("_", ""), tuple(key.split("_"))) for key in _SECRET_KEYS_SORTED
)
_SECRET_CONTAINER_KEYS: frozenset[str] = frozenset(
    {
        "api_keys",
        "client_secrets",
        "credentials",
        "id_tokens",
        "passwords",
        "refresh_tokens",
        "secrets",
        "tokens",
    },
)
_NON_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "accepted_prediction_tokens",
        "audio_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cached_tokens",
        "completion_tokens",
        "continuation_token",
        "input_tokens",
        "max_tokens",
        "next_token",
        "output_tokens",
        "page_token",
        "pagination_token",
        "prompt_tokens",
        "reasoning_tokens",
        "rejected_prediction_tokens",
        "token_budget",
        "token_count",
        "tokens_used",
        "total_tokens",
    },
)
_CONTEXT_SECRET_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "field",
        "field_name",
        "header",
        "key",
        "name",
        "param",
        "parameter",
        "variable",
    },
)
_CONTEXT_SECRET_VALUE_KEYS: frozenset[str] = frozenset(
    {
        "default",
        "raw_value",
        "secret_value",
        "value",
    },
)
_REDACTION_LOOKAHEAD_CHARS = 512

type _RedactedValue = None | bool | int | float | str | list["_RedactedValue"] | dict[str, "_RedactedValue"]


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except BaseException:
        return f"<unrepresentable: {type(value).__name__}>"


def _safe_repr(value: object) -> str:
    try:
        return repr(value)
    except BaseException:
        return f"<unrepresentable: {type(value).__name__}>"


def _normalize_key(value: object) -> str:
    key = _safe_str(value)
    key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key.strip())
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _is_secret_key(value: object) -> bool:
    normalized = _normalize_key(value)
    if normalized in _NON_SECRET_KEYS:
        return False
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


def _is_secret_container_key(value: object) -> bool:
    normalized = _normalize_key(value)
    if normalized in _NON_SECRET_KEYS:
        return False
    if normalized in _SECRET_CONTAINER_KEYS:
        return True
    return any(normalized.endswith(f"_{key}") for key in _SECRET_CONTAINER_KEYS)


def _is_sensitive_key(value: object) -> bool:
    return _is_secret_key(value) or _is_secret_container_key(value)


def _is_query_container(value: str | None) -> bool:
    return value is not None and _normalize_key(value) in _QUERY_CONTAINER_KEYS


def _is_redacted_query_key(value: object) -> bool:
    normalized = _normalize_key(value)
    return _is_secret_key(value) or normalized in _OAUTH_QUERY_KEYS or normalized in _URL_QUERY_SECRET_KEYS


def _is_context_secret_label_key(value: object) -> bool:
    return _normalize_key(value) in _CONTEXT_SECRET_LABEL_KEYS


def _is_context_secret_value_key(value: object) -> bool:
    return _normalize_key(value) in _CONTEXT_SECRET_VALUE_KEYS


def _mapping_has_secret_context_label(value: Mapping[object, object]) -> bool:
    for key, item in value.items():
        if not _is_context_secret_label_key(key):
            continue
        if isinstance(item, str) and _is_sensitive_key(item):
            return True
    return False


def _redact_matched_token(match: re.Match[str], group_name: str = "token") -> str:
    group_start, group_end = match.span(group_name)
    full_match = match.group(0)
    prefix_end = group_start - match.start()
    suffix_start = group_end - match.start()
    return full_match[:prefix_end] + REDACTED + full_match[suffix_start:]


def _redact_nested_assignment_value(match: re.Match[str]) -> str:
    quote = match.group("quote")
    if quote is not None:
        quoted_value = match.group("quoted_value")
        if quoted_value is None:
            return match.group(0)
        return f"{match.group('prefix')}{quote}{redact_sensitive_text(quoted_value)}{quote}"
    value = match.group("value")
    if value is None:
        return match.group(0)
    return match.group("prefix") + redact_sensitive_text(value)


def _redact_secret_assignment(match: re.Match[str]) -> str:
    key = match.group("key")
    normalized_key = _normalize_key(key)
    if not _is_secret_key(key):
        return _redact_nested_assignment_value(match)
    value = match.group("value")
    if (
        normalized_key == "authorization"
        and value is not None
        and (value.lower() in {"basic", "bearer"} or value.lower().startswith(f"bearer {REDACTED}"))
    ):
        return match.group(0)
    quote = match.group("quote")
    if quote is not None:
        return f"{match.group('prefix')}{quote}{REDACTED}{quote}"
    return match.group("prefix") + REDACTED


def _redact_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
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
        query_items: list[tuple[str, str]] = []
        query_changed = False
        for key, item in parse_qsl(query, keep_blank_values=True):
            if _is_redacted_query_key(key):
                query_items.append((key, REDACTED))
                query_changed = True
            else:
                query_items.append((key, item))
        if query_changed:
            query = urlencode(query_items, doseq=True, safe="*")
            changed = True

    if not changed:
        return value
    return urlunparse(parsed._replace(netloc=netloc, query=query))


def _redact_query_fragment(value: str, *, max_length: int | None) -> str:
    query_items: list[tuple[str, str]] = []
    changed = False
    for key, item in parse_qsl(value, keep_blank_values=True):
        if _is_redacted_query_key(key):
            query_items.append((key, REDACTED))
            changed = True
        else:
            query_items.append((key, item))
    if not changed:
        return redact_sensitive_text(value, max_length=max_length)
    return _truncate_text(urlencode(query_items, doseq=True, safe="*"), max_length)


def _truncate_text(value: str, max_length: int | None) -> str:
    if max_length is None or len(value) <= max_length:
        return value
    return value[: max_length - len(_TRUNCATED)] + _TRUNCATED


def _bounded_redaction_input(value: str, *, max_length: int | None) -> str:
    if max_length is None:
        return value
    scan_length = max_length + _REDACTION_LOOKAHEAD_CHARS
    if len(value) <= scan_length:
        return value
    return value[:scan_length]


def redact_sensitive_text(value: str, *, max_length: int | None = None) -> str:
    """Redact common credential and bearer-token patterns from free-form text."""
    bounded_value = _bounded_redaction_input(value, max_length=max_length)
    redacted = _URL_PATTERN.sub(lambda match: _redact_url(match.group(0)), bounded_value)
    redacted = _BEARER_TOKEN_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _API_KEY_MESSAGE_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _TOKEN_LIKE_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(_redact_secret_assignment, redacted)
    return _truncate_text(redacted, max_length)


def _normalized_structured_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", exclude_none=True)
    if not isinstance(value, type) and is_dataclass(value):
        return asdict(value)
    return value


def _redact_mapping(
    value: Mapping[object, object],
    *,
    parent_key: str | None,
    depth: int,
    max_string_length: int | None,
    max_collection_items: int | None,
    max_depth: int | None,
    force_redact: bool,
) -> dict[str, _RedactedValue]:
    redacted: dict[str, _RedactedValue] = {}
    has_secret_context_label = _mapping_has_secret_context_label(value)
    for index, (key, item) in enumerate(value.items()):
        if max_collection_items is not None and index >= max_collection_items:
            redacted["__truncated__"] = f"{len(value) - max_collection_items} more items"
            break
        key_text = _safe_str(key)
        redact_key = (
            _is_sensitive_key(key)
            or (_is_query_container(parent_key) and _is_redacted_query_key(key))
            or (has_secret_context_label and _is_context_secret_value_key(key))
        )
        redacted[key_text] = redact_sensitive_data(
            item,
            max_string_length=max_string_length,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
            _parent_key=key_text,
            _depth=depth + 1,
            _force_redact=force_redact or redact_key,
        )
    return redacted


def _redact_sequence(
    value: list[object],
    *,
    parent_key: str | None,
    depth: int,
    max_string_length: int | None,
    max_collection_items: int | None,
    max_depth: int | None,
    force_redact: bool,
) -> list[_RedactedValue]:
    items = value if max_collection_items is None else value[:max_collection_items]
    redacted_items = [
        redact_sensitive_data(
            item,
            max_string_length=max_string_length,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
            _parent_key=parent_key,
            _depth=depth + 1,
            _force_redact=force_redact,
        )
        for item in items
    ]
    if max_collection_items is not None and len(value) > max_collection_items:
        redacted_items.append(_TRUNCATED)
    return redacted_items


def _redact_scalar_value(
    value: object,
    *,
    parent_key: str | None,
    max_string_length: int | None,
    force_redact: bool,
) -> _RedactedValue:
    if force_redact or (parent_key is not None and _is_sensitive_key(parent_key)):
        redacted: _RedactedValue = REDACTED
    elif isinstance(value, bytes):
        redacted = "<bytes>"
    elif isinstance(value, Path):
        redacted = str(value)
    elif isinstance(value, str):
        if _is_query_container(parent_key):
            redacted = _redact_query_fragment(value, max_length=max_string_length)
        else:
            redacted = redact_sensitive_text(value, max_length=max_string_length)
    elif isinstance(value, float):
        redacted = value if math.isfinite(value) else None
    elif value is None or isinstance(value, bool | int):
        redacted = value
    else:
        redacted = redact_sensitive_text(_safe_repr(value), max_length=max_string_length)
    return redacted


def redact_sensitive_data(
    value: object,
    *,
    max_string_length: int | None = None,
    max_collection_items: int | None = None,
    max_depth: int | None = None,
    _parent_key: str | None = None,
    _depth: int = 0,
    _force_redact: bool = False,
) -> _RedactedValue:
    """Recursively redact secret-bearing fields while preserving log shape."""
    if max_depth is not None and _depth >= max_depth:
        return _TRUNCATED
    value = _normalized_structured_value(value)

    if isinstance(value, Mapping):
        redacted: _RedactedValue = _redact_mapping(
            cast("Mapping[object, object]", value),
            parent_key=_parent_key,
            depth=_depth,
            max_string_length=max_string_length,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
            force_redact=_force_redact,
        )
    elif isinstance(value, list | tuple | set | frozenset):
        redacted = _redact_sequence(
            list(value),
            parent_key=_parent_key,
            depth=_depth,
            max_string_length=max_string_length,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
            force_redact=_force_redact,
        )
    else:
        redacted = _redact_scalar_value(
            value,
            parent_key=_parent_key,
            max_string_length=max_string_length,
            force_redact=_force_redact,
        )
    return redacted


def redact_log_event(_logger: object, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Structlog processor that redacts one structured event dictionary."""
    return cast("dict[str, Any]", redact_sensitive_data(event_dict))
