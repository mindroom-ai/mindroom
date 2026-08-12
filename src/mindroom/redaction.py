"""Centralized credential redaction for logs and audit records."""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from functools import lru_cache
from itertools import islice
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel

REDACTED = "***redacted***"
REDACTION_FAILED = "[redaction failed]"
__all__ = [
    "REDACTED",
    "REDACTION_FAILED",
    "redact_log_event",
    "redact_sensitive_data",
    "redact_sensitive_text",
]
_TRUNCATED = "... [truncated]"
_MAX_TEXT_INPUT_LENGTH = 64 * 1024
_MAX_LOG_COLLECTION_ITEMS = 100
_MAX_DEPTH = 32
# Any scheme, not just the web ones. Userinfo credentials are a property of the
# URI grammar rather than of HTTP, and the schemes that carry the most damaging
# ones here are database URLs: `postgresql://user:password@host/db` reaches logs
# and audit records through exactly the same paths an API URL does.
_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")
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
_ASSIGNMENT_PREFIX_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"[\"']?(?P<key>[A-Za-z0-9_.-]++)[\"']?[^\S\r\n]*+(?::|=(?!=))[^\S\r\n]*+",
    re.IGNORECASE,
)
_NEXT_ASSIGNMENT_PATTERN = re.compile(
    r"(?<!\s)[^\S\r\n]++(?:and[^\S\r\n]++)?"
    r"[\"']?[A-Za-z0-9_.-]++[\"']?[^\S\r\n]*+(?::|=(?!=))",
    re.IGNORECASE,
)
_ASSIGNMENT_VALUE_TERMINATOR_PATTERN = re.compile(r"[\r\n,&)\]}\"']")
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
_TOKEN_LIKE_MARKERS = (
    "sk-",
    "pk-",
    "sk_live_",
    "sk_test_",
    "pk_live_",
    "pk_test_",
    "rk_live_",
    "rk_test_",
    "xoxb-",
    "xoxa-",
    "xoxp-",
    "xoxr-",
    "xoxs-",
    "ghp_",
    "gho_",
    "ghu_",
    "ghs_",
    "ghr_",
    "github_pat_",
    "AIza",
)
_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "api_token",
        "authentication_info",
        "authorization",
        "auth_token",
        "bearer_token",
        "client_secret",
        "cookie",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "security_token",
        "session_token",
        "set_cookie",
        "token",
        "www_authenticate",
        "x_token",
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
        "access_tokens",
        "api_keys",
        "api_tokens",
        "auth_tokens",
        "client_secrets",
        "credentials",
        "id_tokens",
        "oauth_tokens",
        "passwords",
        "refresh_tokens",
        "secrets",
        "session_tokens",
        "tokens",
    },
)
_CONTEXT_SECRET_LABEL_KEYS: frozenset[str] = frozenset(
    {
        "header",
        "key",
        "name",
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


_ACRONYM_BOUNDARY_PATTERN = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_PATTERN = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALPHANUMERIC_RUN_PATTERN = re.compile(r"[^a-z0-9]+")

# Structured logs repeat a small set of keys at very high frequency, so classifying
# each distinct key once and reusing the result removes the dominant per-event cost.
# The cache is bounded on both axes: entry count, and the key length allowed in.
# Oversized keys bypass it entirely rather than evicting real keys or pinning
# arbitrarily large strings in memory.
_KEY_CLASSIFICATION_CACHE_SIZE = 4096
_MAX_CACHED_KEY_LENGTH = 256


@dataclass(frozen=True, slots=True)
class _KeyClassification:
    """Every redaction decision that depends only on one key's normalized spelling."""

    normalized: str
    is_secret: bool
    is_secret_container: bool
    is_secret_container_suffix: bool
    is_query_container: bool
    is_redacted_query: bool
    is_context_secret_label: bool
    is_context_secret_value: bool


def _normalize_key_text(key: str) -> str:
    """Return the canonical snake_case spelling of one key."""
    collapsed = _ACRONYM_BOUNDARY_PATTERN.sub(r"\1_\2", key.strip())
    collapsed = _CAMEL_BOUNDARY_PATTERN.sub(r"\1_\2", collapsed)
    return _NON_ALPHANUMERIC_RUN_PATTERN.sub("_", collapsed.lower()).strip("_")


def _normalized_key_is_secret(normalized: str) -> bool:
    parts = tuple(part for part in normalized.split("_") if part)
    compact = normalized.replace("_", "")
    for key, compact_key, key_parts in _SECRET_KEY_VARIANTS:
        if key == "token":
            if normalized == key or compact == compact_key:
                return True
            continue
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


def _classify_key_text(key: str) -> _KeyClassification:
    """Resolve every key-derived redaction predicate in one pass."""
    normalized = _normalize_key_text(key)
    is_secret = _normalized_key_is_secret(normalized)
    is_container_suffix = normalized not in _SECRET_CONTAINER_KEYS and any(
        container_key != "tokens" and normalized.endswith(f"_{container_key}")
        for container_key in _SECRET_CONTAINER_KEYS
    )
    return _KeyClassification(
        normalized=normalized,
        is_secret=is_secret,
        is_secret_container=normalized in _SECRET_CONTAINER_KEYS or is_container_suffix,
        is_secret_container_suffix=is_container_suffix,
        is_query_container=normalized in _QUERY_CONTAINER_KEYS,
        is_redacted_query=is_secret or normalized in _OAUTH_QUERY_KEYS or normalized in _URL_QUERY_SECRET_KEYS,
        is_context_secret_label=normalized in _CONTEXT_SECRET_LABEL_KEYS,
        is_context_secret_value=normalized in _CONTEXT_SECRET_VALUE_KEYS,
    )


_classify_key_text_cached = lru_cache(maxsize=_KEY_CLASSIFICATION_CACHE_SIZE)(_classify_key_text)


def _classify_key(value: object) -> _KeyClassification:
    key = _safe_str(value)
    if len(key) > _MAX_CACHED_KEY_LENGTH:
        return _classify_key_text(key)
    return _classify_key_text_cached(key)


def _is_sensitive_key(value: object) -> bool:
    classification = _classify_key(value)
    return classification.is_secret or classification.is_secret_container


def _is_query_container(value: str | None) -> bool:
    return value is not None and _classify_key(value).is_query_container


def _is_redacted_query_key(value: object) -> bool:
    return _classify_key(value).is_redacted_query


def _is_context_secret_label_key(value: object) -> bool:
    return _classify_key(value).is_context_secret_label


def _mapping_has_secret_context_label(value: Mapping[object, object]) -> bool:
    for key, item in value.items():
        if not _is_context_secret_label_key(key):
            continue
        if isinstance(item, str) and _is_sensitive_key(item):
            return True
    return False


def _should_force_redact_container_value(value: object) -> bool:
    return value is not None and not isinstance(value, bool | int | float)


def _should_redact_value_for_key(key: object, value: object) -> bool:
    classification = _classify_key(key)
    if classification.is_secret:
        return True
    if classification.is_secret_container_suffix:
        return _should_force_redact_container_value(value)
    return classification.is_secret_container


def _redact_matched_token(match: re.Match[str], group_name: str = "token") -> str:
    group_start, group_end = match.span(group_name)
    full_match = match.group(0)
    prefix_end = group_start - match.start()
    suffix_start = group_end - match.start()
    return full_match[:prefix_end] + REDACTED + full_match[suffix_start:]


class _RedactionError(Exception):
    """Internal signal for input that cannot be redacted safely within its budget."""


def _next_assignment_value_end(value: str, value_start: int) -> int:
    literal_terminator = _ASSIGNMENT_VALUE_TERMINATOR_PATTERN.search(value, value_start)
    next_assignment = _NEXT_ASSIGNMENT_PATTERN.search(value, value_start)
    return min(match.start() if match is not None else len(value) for match in (literal_terminator, next_assignment))


def _find_unescaped_quote(value: str, quote: str, start: int, end: int) -> int:
    search_start = start
    while (position := value.find(quote, search_start, end)) >= 0:
        backslash_start = position
        while backslash_start > start and value[backslash_start - 1] == "\\":
            backslash_start -= 1
        if (position - backslash_start) % 2 == 0:
            return position
        search_start = position + 1
    return -1


def _assignment_value_span(value: str, value_start: int) -> tuple[int, int, int] | None:
    if value_start >= len(value):
        return None
    if value[value_start] in "\r\n":
        raise _RedactionError
    if value[value_start] not in {"'", '"'}:
        value_end = _next_assignment_value_end(value, value_start)
        if value_end == value_start:
            return None
        return value_start, value_end, value_end

    quote = value[value_start]
    line_end = min(
        position
        for position in (
            value.find("\r", value_start + 1),
            value.find("\n", value_start + 1),
            len(value),
        )
        if position >= 0
    )
    value_end = _find_unescaped_quote(value, quote, value_start + 1, line_end)
    if value_end < 0:
        raise _RedactionError
    return value_start + 1, value_end, value_end + 1


def _replace_spans_with_redaction(value: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return value
    parts: list[str] = []
    copied_until = 0
    for value_start, value_end in spans:
        parts.extend((value[copied_until:value_start], REDACTED))
        copied_until = value_end
    parts.append(value[copied_until:])
    return "".join(parts)


def _redact_secret_assignments(value: str) -> str:
    """Redact shallow key assignments with one forward-only scan."""
    spans: list[tuple[int, int]] = []
    search_start = 0
    while prefix_match := _ASSIGNMENT_PREFIX_PATTERN.search(value, search_start):
        search_start = prefix_match.end()
        classification = _classify_key(prefix_match.group("key"))
        if not classification.is_secret:
            continue

        value_span = _assignment_value_span(value, prefix_match.end())
        if value_span is None:
            continue
        value_start, value_end, match_end = value_span
        assignment_value = value[value_start:value_end].lower()
        if classification.normalized == "authorization" and assignment_value in {
            "basic",
            "bearer",
            f"bearer {REDACTED}",
        }:
            search_start = match_end
            continue

        spans.append((value_start, value_end))
        search_start = match_end

    return _replace_spans_with_redaction(value, spans)


def _redact_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
    if not parsed.scheme:
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
    scan_length = min(max_length + _REDACTION_LOOKAHEAD_CHARS, _MAX_TEXT_INPUT_LENGTH + 1)
    if len(value) <= scan_length:
        return value
    return value[:scan_length]


def _redact_url_match(match: re.Match[str]) -> str:
    r"""Redact one matched URL, leaving trailing backslashes untouched.

    In logged shell commands and JSON-encoded strings, a backslash right after
    the URL is escaping the next character (for example ``\\"``), not URL
    content. Absorbing it into the query re-encodes it to ``%5C`` and strips
    the escape, which corrupts the surrounding encoding.
    """
    matched_url = match.group(0)
    url = matched_url.rstrip("\\")
    trailing_backslashes = matched_url[len(url) :]
    return _redact_url(url) + trailing_backslashes


def _redact_sensitive_text(value: str, *, max_length: int | None) -> str:
    bounded_value = _bounded_redaction_input(value, max_length=max_length)
    has_assignment = "=" in bounded_value or ":" in bounded_value
    has_url = "://" in bounded_value
    lowered_value = bounded_value.lower()
    has_bearer = "bearer" in lowered_value
    has_api_key_message = "api key" in lowered_value
    has_token = any(marker in bounded_value for marker in _TOKEN_LIKE_MARKERS)
    if not any((has_assignment, has_url, has_bearer, has_api_key_message, has_token)):
        return _truncate_text(bounded_value, max_length)
    redacted = _URL_PATTERN.sub(_redact_url_match, bounded_value) if has_url else bounded_value
    if has_bearer:
        redacted = _BEARER_TOKEN_PATTERN.sub(_redact_matched_token, redacted)
    if has_api_key_message:
        redacted = _API_KEY_MESSAGE_PATTERN.sub(_redact_matched_token, redacted)
    if has_token:
        redacted = _TOKEN_LIKE_PATTERN.sub(_redact_matched_token, redacted)
    if has_assignment:
        redacted = _redact_secret_assignments(redacted)
    return _truncate_text(redacted, max_length)


def _redact_sensitive_text_fail_closed(value: str, *, max_length: int | None) -> str:
    try:
        return _redact_sensitive_text(value, max_length=max_length)
    except Exception:
        return _truncate_text(REDACTION_FAILED, max_length)


def redact_sensitive_text(value: str, *, max_length: int | None = None) -> str:
    """Redact common credential patterns without letting redaction break its caller."""
    if len(_bounded_redaction_input(value, max_length=max_length)) > _MAX_TEXT_INPUT_LENGTH:
        return _truncate_text(REDACTION_FAILED, max_length)
    return _redact_sensitive_text_fail_closed(value, max_length=max_length)


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
    mapping_is_truncated = max_collection_items is not None and len(value) > max_collection_items
    has_secret_context_label = mapping_is_truncated or _mapping_has_secret_context_label(value)
    parent_is_query_container = _is_query_container(parent_key)
    for index, (key, item) in enumerate(value.items()):
        if max_collection_items is not None and index >= max_collection_items:
            redacted["__truncated__"] = f"{len(value) - max_collection_items} more items"
            break
        key_text = _safe_str(key)
        classification = _classify_key(key)
        redact_key = (
            _should_redact_value_for_key(key, item)
            or (parent_is_query_container and classification.is_redacted_query)
            or (has_secret_context_label and classification.is_context_secret_value)
        )
        redacted[key_text] = _redact_sensitive_data(
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
    value: Collection[object],
    *,
    parent_key: str | None,
    depth: int,
    max_string_length: int | None,
    max_collection_items: int | None,
    max_depth: int | None,
    force_redact: bool,
) -> list[_RedactedValue]:
    items = list(value) if max_collection_items is None else list(islice(value, max_collection_items))
    redacted_items = [
        _redact_sensitive_data(
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
    if force_redact or (parent_key is not None and _should_redact_value_for_key(parent_key, value)):
        redacted: _RedactedValue = REDACTED
    elif isinstance(value, bytes):
        redacted = "<bytes>"
    elif isinstance(value, Path):
        redacted = str(value)
    elif isinstance(value, str):
        if _is_query_container(parent_key):
            redacted = _redact_query_fragment(value, max_length=max_string_length)
        else:
            redacted = _redact_sensitive_text_fail_closed(value, max_length=max_string_length)
    elif isinstance(value, float):
        redacted = value if math.isfinite(value) else None
    elif value is None or isinstance(value, bool | int):
        redacted = value
    else:
        redacted = _redact_sensitive_text_fail_closed(_safe_repr(value), max_length=max_string_length)
    return redacted


def _redact_sensitive_data(
    value: object,
    *,
    max_string_length: int | None = None,
    max_collection_items: int | None = None,
    max_depth: int | None = None,
    _parent_key: str | None = None,
    _depth: int = 0,
    _force_redact: bool = False,
) -> _RedactedValue:
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
            value,
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


def redact_sensitive_data(
    value: object,
    *,
    max_string_length: int | None = None,
    max_collection_items: int | None = None,
    max_depth: int | None = None,
) -> _RedactedValue:
    """Redact structured data without letting redaction break its caller."""
    collection_limit = None if max_collection_items is None else max(max_collection_items, 0)
    depth_limit = _MAX_DEPTH if max_depth is None else min(max(max_depth, 0), _MAX_DEPTH)
    try:
        return _redact_sensitive_data(
            value,
            max_string_length=max_string_length,
            max_collection_items=collection_limit,
            max_depth=depth_limit,
        )
    except Exception:
        if isinstance(value, Mapping):
            return {"__redaction_failed__": REDACTION_FAILED}
        return REDACTION_FAILED


def redact_log_event(_logger: object, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Structlog processor that redacts one structured event dictionary."""
    try:
        redacted = _redact_sensitive_data(
            event_dict,
            max_string_length=_MAX_TEXT_INPUT_LENGTH,
            max_collection_items=_MAX_LOG_COLLECTION_ITEMS,
            max_depth=_MAX_DEPTH,
        )
    except Exception:
        return {"event": REDACTION_FAILED}
    if not isinstance(redacted, dict):
        return {"event": REDACTION_FAILED}
    return cast("dict[str, Any]", redacted)
