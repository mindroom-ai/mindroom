"""Centralized credential redaction for logs and audit records."""

from __future__ import annotations

import math
import re
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel

REDACTED = "***redacted***"
__all__ = [
    "REDACTED",
    "redact_log_event",
    "redact_sensitive_data",
    "redact_sensitive_text",
]
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
# Each inter-assignment boundary starts at the first whitespace in its run so indexed starts are unique.
# Possessive runs keep the boundary scan linear on long whitespace and key-like suffixes.
_NEXT_ASSIGNMENT_PATTERN = r"(?<!\s)\s++(?:and(?P<post_and_whitespace>\s++))?[\"']?[A-Za-z0-9_.-]++[\"']?\s*+[:=]"
_ASSIGNMENT_PREFIX_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])"
    r"[\"']?(?P<key>[A-Za-z0-9_.-]++)[\"']?\s*+[:=](?P<value_whitespace>\s*+)",
    re.IGNORECASE,
)
_NEXT_ASSIGNMENT_TERMINATOR_PATTERN = re.compile(_NEXT_ASSIGNMENT_PATTERN, re.IGNORECASE)
_ASSIGNMENT_VALUE_TERMINATORS = frozenset("\r\n,&)]}")
_ASSIGNMENT_VALUE_TERMINATOR_PATTERN = re.compile(
    f"[{re.escape(''.join(sorted(_ASSIGNMENT_VALUE_TERMINATORS)))}]",
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


@dataclass(frozen=True, slots=True)
class _AssignmentBoundaries:
    literal_terminator_starts: array[int]
    assignment_terminator_starts: array[int]
    assignment_terminator_ends: array[int]
    line_break_starts: array[int]
    single_quote_ends: array[int]
    single_quote_terminator_ends: array[int]
    double_quote_ends: array[int]
    double_quote_terminator_ends: array[int]


@dataclass(frozen=True, slots=True)
class _AssignmentValueMatch:
    value_start: int
    value_end: int
    match_end: int
    is_quoted: bool


def _append_quote_boundary(
    value: str,
    boundary_start: int,
    boundary_end: int,
    boundaries: _AssignmentBoundaries,
) -> None:
    if boundary_start == 0:
        return
    quote_end = boundary_start - 1
    if value[quote_end] == "'":
        boundaries.single_quote_ends.append(quote_end)
        boundaries.single_quote_terminator_ends.append(boundary_end)
    elif value[quote_end] == '"':
        boundaries.double_quote_ends.append(quote_end)
        boundaries.double_quote_terminator_ends.append(boundary_end)


def _index_assignment_boundaries(value: str) -> _AssignmentBoundaries:
    """Merge C-level terminator scans into compact integer boundary buffers."""
    index_type = "I" if len(value) <= (1 << 32) - 1 else "Q"
    literal_terminator_starts = array(index_type)
    assignment_terminator_starts = array(index_type)
    assignment_terminator_ends = array(index_type)
    line_break_starts = array(index_type)
    single_quote_ends = array(index_type)
    single_quote_terminator_ends = array(index_type)
    double_quote_ends = array(index_type)
    double_quote_terminator_ends = array(index_type)
    boundaries = _AssignmentBoundaries(
        literal_terminator_starts=literal_terminator_starts,
        assignment_terminator_starts=assignment_terminator_starts,
        assignment_terminator_ends=assignment_terminator_ends,
        line_break_starts=line_break_starts,
        single_quote_ends=single_quote_ends,
        single_quote_terminator_ends=single_quote_terminator_ends,
        double_quote_ends=double_quote_ends,
        double_quote_terminator_ends=double_quote_terminator_ends,
    )
    literal_matches = iter(_ASSIGNMENT_VALUE_TERMINATOR_PATTERN.finditer(value))
    literal_match = next(literal_matches, None)
    assignment_matches = iter(_NEXT_ASSIGNMENT_TERMINATOR_PATTERN.finditer(value))
    assignment_match = next(assignment_matches, None)

    while literal_match is not None or assignment_match is not None:
        literal_start = literal_match.start() if literal_match is not None else len(value)
        assignment_start = assignment_match.start() if assignment_match is not None else len(value)
        boundary_start = min(literal_start, assignment_start)
        boundary_end = len(value)

        if literal_start == boundary_start:
            literal_terminator_starts.append(literal_start)
            if literal_match is not None and literal_match.group() in "\r\n":
                line_break_starts.append(literal_start)
            boundary_end = literal_start + 1
            literal_match = next(literal_matches, None)

        if assignment_start == boundary_start:
            assert assignment_match is not None
            assignment_end = assignment_match.end()
            assignment_terminator_starts.append(assignment_start)
            assignment_terminator_ends.append(assignment_end)
            boundary_end = min(boundary_end, assignment_end)
            post_and_boundary = assignment_match.start("post_and_whitespace")
            if post_and_boundary >= 0:
                assignment_terminator_starts.append(post_and_boundary)
                assignment_terminator_ends.append(assignment_end)
            assignment_match = next(assignment_matches, None)

        _append_quote_boundary(value, boundary_start, boundary_end, boundaries)

    _append_quote_boundary(value, len(value), len(value), boundaries)
    return boundaries


def _next_position_before(
    positions: array[int],
    start: int,
    region_end: int,
) -> int | None:
    candidate_index = bisect_left(positions, start)
    if candidate_index >= len(positions):
        return None
    candidate = positions[candidate_index]
    return candidate if candidate < region_end else None


def _next_literal_terminator(
    boundaries: _AssignmentBoundaries,
    value_start: int,
    region_end: int,
) -> int:
    literal_terminator = _next_position_before(
        boundaries.literal_terminator_starts,
        value_start,
        region_end,
    )
    return region_end if literal_terminator is None else literal_terminator


def _next_assignment_terminator(
    boundaries: _AssignmentBoundaries,
    value_start: int,
    region_end: int,
) -> int:
    literal_terminator = _next_literal_terminator(boundaries, value_start, region_end)
    assignment_index = bisect_right(boundaries.assignment_terminator_starts, value_start)
    assignment_terminator: int | None = None
    while assignment_index < len(boundaries.assignment_terminator_starts):
        candidate = boundaries.assignment_terminator_starts[assignment_index]
        if candidate >= region_end:
            break
        if boundaries.assignment_terminator_ends[assignment_index] <= region_end:
            assignment_terminator = candidate
            break
        assignment_index += 1
    return min(
        candidate for candidate in (literal_terminator, assignment_terminator, region_end) if candidate is not None
    )


def _first_line_break(
    boundaries: _AssignmentBoundaries,
    value_start: int,
    region_end: int,
) -> int | None:
    return _next_position_before(boundaries.line_break_starts, value_start, region_end)


def _quoted_assignment_end(
    value: str,
    value_start: int,
    region_end: int,
    boundaries: _AssignmentBoundaries,
) -> int | None:
    """Return the first valid closing quote without scanning the remaining value."""
    if value_start >= region_end or value[value_start] not in {"'", '"'}:
        return None
    quote = value[value_start]
    if quote == "'":
        candidates = boundaries.single_quote_ends
        terminator_ends = boundaries.single_quote_terminator_ends
    else:
        candidates = boundaries.double_quote_ends
        terminator_ends = boundaries.double_quote_terminator_ends
    line_break = _first_line_break(boundaries, value_start, region_end)
    quoted_region_end = region_end if line_break is None else line_break
    candidate_index = bisect_right(candidates, value_start)
    while candidate_index < len(candidates):
        candidate = candidates[candidate_index]
        if candidate >= quoted_region_end:
            break
        if terminator_ends[candidate_index] <= region_end:
            return candidate
        candidate_index += 1
    local_end = region_end - 1
    if quoted_region_end == region_end and local_end > value_start and value[local_end] == quote:
        return local_end
    return None


def _multiline_quoted_assignment_end(
    value: str,
    value_start: int,
    region_end: int,
    boundaries: _AssignmentBoundaries,
) -> int | None:
    if value_start >= region_end or value[value_start] not in {"'", '"'}:
        return None
    quote = value[value_start]
    line_break = _first_line_break(boundaries, value_start, region_end)
    quoted_region_end = region_end if line_break is None else line_break
    quote_end = value.find(quote, value_start + 1, quoted_region_end)
    while quote_end >= 0:
        boundary_start = quote_end + 1
        if (
            boundary_start == quoted_region_end
            or value[boundary_start].isspace()
            or value[boundary_start] in _ASSIGNMENT_VALUE_TERMINATORS
        ):
            return quote_end
        quote_end = value.find(quote, boundary_start, quoted_region_end)
    return None


def _trailing_whitespace_value_span(
    value: str,
    prefix_match: re.Match[str],
) -> tuple[int, int] | None:
    whitespace_start, whitespace_end = prefix_match.span("value_whitespace")
    carriage_return = value.find("\r", whitespace_start, whitespace_end)
    line_feed = value.find("\n", whitespace_start, whitespace_end)
    first_line_break = min(position for position in (carriage_return, line_feed, whitespace_end) if position >= 0)
    candidate = first_line_break - 1
    if candidate >= whitespace_start and value[candidate].isspace():
        return candidate, first_line_break
    return None


def _line_indentation(value: str, position: int) -> int:
    line_start = max(value.rfind("\r", 0, position), value.rfind("\n", 0, position)) + 1
    leading_text = value[line_start:position]
    return len(leading_text) if not leading_text.strip(" \t") else 0


def _assignment_continuation_indentation(
    value: str,
    prefix_match: re.Match[str],
) -> tuple[int, int] | None:
    whitespace_start, whitespace_end = prefix_match.span("value_whitespace")
    line_break = max(
        value.rfind("\r", whitespace_start, whitespace_end),
        value.rfind("\n", whitespace_start, whitespace_end),
    )
    if line_break < 0:
        return None
    key_indentation = _line_indentation(value, prefix_match.start())
    value_indentation = prefix_match.end() - line_break - 1
    return key_indentation, value_indentation


def _assignment_value_match(
    value: str,
    prefix_match: re.Match[str],
    region_end: int,
    boundaries: _AssignmentBoundaries,
) -> _AssignmentValueMatch | None:
    value_start = prefix_match.end()
    if value_start == region_end:
        trailing_value_span = _trailing_whitespace_value_span(value, prefix_match)
        if trailing_value_span is None:
            return None
        trailing_value_start, trailing_value_end = trailing_value_span
        return _AssignmentValueMatch(
            value_start=trailing_value_start,
            value_end=trailing_value_end,
            match_end=trailing_value_end,
            is_quoted=False,
        )

    continuation_indentation = _assignment_continuation_indentation(value, prefix_match)
    if (
        continuation_indentation is not None
        and continuation_indentation[1] <= continuation_indentation[0]
        and _ASSIGNMENT_PREFIX_PATTERN.match(value, value_start, region_end) is not None
    ):
        return None

    quoted_end = (
        _multiline_quoted_assignment_end(value, value_start, region_end, boundaries)
        if continuation_indentation is not None
        else _quoted_assignment_end(value, value_start, region_end, boundaries)
    )
    if quoted_end is not None:
        return _AssignmentValueMatch(
            value_start=value_start + 1,
            value_end=quoted_end,
            match_end=quoted_end + 1,
            is_quoted=True,
        )
    value_end = (
        _next_literal_terminator(boundaries, value_start, region_end)
        if continuation_indentation is not None
        else _next_assignment_terminator(boundaries, value_start, region_end)
    )
    return _AssignmentValueMatch(
        value_start=value_start,
        value_end=value_end,
        match_end=value_end,
        is_quoted=False,
    )


def _is_preserved_authorization_assignment(
    classification: _KeyClassification,
    value: str,
    match: _AssignmentValueMatch,
) -> bool:
    if classification.normalized != "authorization" or match.is_quoted:
        return False
    assignment_value = value[match.value_start : match.value_end].lower()
    return assignment_value in {"basic", "bearer"} or assignment_value.startswith(
        f"bearer {REDACTED}",
    )


def _replace_spans_with_redaction(value: str, spans: list[tuple[int, int]]) -> str:
    if not spans:
        return value
    parts: list[str] = []
    copied_until = 0
    for value_start, value_end in spans:
        assert copied_until <= value_start <= value_end
        parts.extend((value[copied_until:value_start], REDACTED))
        copied_until = value_end
    parts.append(value[copied_until:])
    return "".join(parts)


def _nested_assignment_regions(
    value: str,
    match: _AssignmentValueMatch,
    region_end: int,
) -> list[tuple[int, int]]:
    """Partition nested scans without separating a trailing prefix from its continuation."""
    if not match.is_quoted and (
        match.match_end == region_end or value[match.match_end] not in _ASSIGNMENT_VALUE_TERMINATORS
    ):
        return []
    if (
        not match.is_quoted
        and value[match.value_start] not in {"'", '"'}
        and value[match.match_end] in "\r\n"
        and any(
            prefix_match.end() == match.value_end
            for prefix_match in _ASSIGNMENT_PREFIX_PATTERN.finditer(value, match.value_start, match.value_end)
        )
    ):
        return [(match.value_start, region_end)]
    regions: list[tuple[int, int]] = []
    if match.match_end < region_end:
        regions.append((match.match_end, region_end))
    if match.value_start < match.value_end:
        regions.append((match.value_start, match.value_end))
    return regions


def _redact_secret_assignments(value: str) -> str:
    """Redact nested assignment values with a forward-only region scan.

    Pending regions are disjoint slices scheduled left-to-right after every parent prefix already searched.
    Compact integer buffers index assignment terminators and valid global closing quotes only once.
    Value matches are bounded before spans are recorded, so replacement cannot consume a terminator or unrelated suffix.
    Accepted secret spans are disjoint because their regions resume after the complete match.
    """
    if "=" not in value and ":" not in value:
        return value
    if not any(
        _classify_key(prefix_match.group("key")).is_secret
        for prefix_match in _ASSIGNMENT_PREFIX_PATTERN.finditer(value)
    ):
        return value

    boundaries = _index_assignment_boundaries(value)
    redacted_spans: list[tuple[int, int]] = []
    regions = [(0, len(value))]
    while regions:
        region_start, region_end = regions.pop()
        search_start = region_start
        while prefix_match := _ASSIGNMENT_PREFIX_PATTERN.search(value, search_start, region_end):
            search_start = prefix_match.end()
            classification = _classify_key(prefix_match.group("key"))
            assignment_match = _assignment_value_match(value, prefix_match, region_end, boundaries)
            if assignment_match is None:
                continue
            if not classification.is_secret:
                nested_regions = _nested_assignment_regions(value, assignment_match, region_end)
                if not nested_regions:
                    continue
                regions.extend(nested_regions)
                break
            if _is_preserved_authorization_assignment(classification, value, assignment_match):
                search_start = assignment_match.match_end
                continue

            redacted_spans.append((assignment_match.value_start, assignment_match.value_end))
            search_start = assignment_match.match_end

    return _replace_spans_with_redaction(value, redacted_spans)


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


def redact_sensitive_text(value: str, *, max_length: int | None = None) -> str:
    """Redact common credential and bearer-token patterns from free-form text."""
    bounded_value = _bounded_redaction_input(value, max_length=max_length)
    redacted = _URL_PATTERN.sub(_redact_url_match, bounded_value)
    redacted = _BEARER_TOKEN_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _API_KEY_MESSAGE_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _TOKEN_LIKE_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _redact_secret_assignments(redacted)
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
