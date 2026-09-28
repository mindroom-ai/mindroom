"""Centralized credential redaction for logs and audit records."""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, fields, is_dataclass
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
    "nests_beyond_redaction_depth",
    "redact_log_event",
    "redact_sensitive_data",
    "redact_sensitive_text",
    "truncate_review_text",
]
_TRUNCATED = "... [truncated]"
_MAX_TEXT_INPUT_LENGTH = 64 * 1024
_MAX_LOG_COLLECTION_ITEMS = 100
_MAX_DEPTH = 32
# Any scheme, not just the web ones. Userinfo credentials are a property of the
# URI grammar rather than of HTTP, and the schemes that carry the most damaging
# ones here are database URLs: `postgresql://user:password@host/db` reaches logs
# and audit records through exactly the same paths an API URL does.
# Start once per scheme-character run, avoiding quadratic suffix rescans.
# Preserve leading non-letters while still redacting embedded URLs such as
# ``123https://user:password@host`` that the unanchored scan recognized.
_URL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+.-])(?P<prefix>[0-9+.-]*+)"
    r"(?P<url>[A-Za-z][A-Za-z0-9+.-]*+://[^\s'\"<>]+)",
)
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
# Reviewer-facing copies hide only credentials in known token formats with realistic lengths, so hosts,
# paths, and short names such as `sk-deploy.sh` stay visible; a token may end a sentence but not a file
# name or host. Every hidden span is letters, digits, and `._-`; possessive quantifiers and the lookbehind
# keep matching linear on long runs of token characters.
_REVIEW_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<token>"
    r"(?:sk|pk)-[A-Za-z0-9_-]{16,}+"
    r"|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}+"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}+"
    r"|gh[pousr]_[A-Za-z0-9]{30,}+"
    r"|github_pat_[A-Za-z0-9_]{30,}+"
    r"|AIza[0-9A-Za-z_-]{30,}+"
    r"|eyJ[A-Za-z0-9_-]{8,}+\.eyJ[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]*+"
    r")(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])",
)
# Only the `sk-` and `pk-` prefixes also start ordinary kebab-case names such as pod, image, or branch
# names; a generated key has a long run mixing character classes, so those names stay visible.
_KEBAB_PREFIXES = ("sk-", "pk-")
_RANDOM_RUN_PATTERN = re.compile(r"[A-Za-z0-9]{12,}")
# Unpaired surrogates survive JSON parsing but cannot be encoded as UTF-8 or displayed.
_LONE_SURROGATE_PATTERN = re.compile("[\ud800-\udfff]")
_PLACEHOLDER_OPEN = "\u27e6"
_PLACEHOLDER_CLOSE = "\u27e7"
# Matrix canonical JSON, required for unencrypted room events, rejects floats and integers outside this range.
_MAX_CANONICAL_JSON_INTEGER = 2**53 - 1
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


_ACRONYM_BOUNDARY_PATTERN = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
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
    collapsed = _ACRONYM_BOUNDARY_PATTERN.sub("_", key.strip())
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
    matched_url = match.group("url")
    url = matched_url.rstrip("\\")
    trailing_backslashes = matched_url[len(url) :]
    return match.group("prefix") + _redact_url(url) + trailing_backslashes


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


def _character_classes(run: str) -> int:
    return len({"digit" if character.isdigit() else "upper" if character.isupper() else "lower" for character in run})


def _looks_generated(token: str) -> bool:
    if not token.startswith(_KEBAB_PREFIXES):
        return True
    return any(_character_classes(run) >= 2 for run in _RANDOM_RUN_PATTERN.findall(token))


def truncate_review_text(value: str, max_length: int | None) -> str:
    """Shorten reviewer-facing text without cutting a placeholder in half."""
    if max_length is None or len(value) <= max_length:
        return value
    head = value[: max_length - len(_TRUNCATED)]
    opening = head.rfind(_PLACEHOLDER_OPEN)
    if opening > head.rfind(_PLACEHOLDER_CLOSE) and head[opening + 1 : opening + 2] != "=":
        head = head[:opening]
    return head + _TRUNCATED


def _redact_review_tokens(value: str, *, max_length: int | None, placeholders: dict[str, str]) -> str:
    """Replace each distinct credential in a known token format with its own numbered placeholder.

    Equal tokens share a placeholder and different tokens get different ones, so the copy is
    the original text with tokens renamed and loses only the characters of each hidden token.
    A literal placeholder opening in the text is shown as ``\u27e6=``, so it cannot pass for a
    placeholder, and a token that contains ``--``, which starts a comment in SQL, says so.
    Tokens are replaced before shortening, so a shortened copy never numbers a cut-off token.
    """

    def placeholder(match: re.Match[str]) -> str:
        token = match.group("token")
        if not _looks_generated(token):
            return token
        if token not in placeholders:
            comment = " --" if "--" in token else ""
            placeholders[token] = f"{_PLACEHOLDER_OPEN}secret-{len(placeholders) + 1}{comment}{_PLACEHOLDER_CLOSE}"
        return placeholders[token]

    text = _LONE_SURROGATE_PATTERN.sub("\ufffd", value).replace(_PLACEHOLDER_OPEN, f"{_PLACEHOLDER_OPEN}=")
    return truncate_review_text(_REVIEW_TOKEN_PATTERN.sub(placeholder, text), max_length)


def _nests_beyond_depth(value: object, depth: int) -> bool:
    if depth >= _MAX_DEPTH:
        return True
    if isinstance(value, Mapping):
        return any(_nests_beyond_depth(item, depth + 1) for item in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return any(_nests_beyond_depth(item, depth + 1) for item in value)
    return False


def nests_beyond_redaction_depth(value: object) -> bool:
    """Return whether redaction would cut part of ``value`` off for nesting too deeply."""
    return _nests_beyond_depth(value, 0)


def _normalized_structured_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", exclude_none=True)
    if not isinstance(value, type) and is_dataclass(value):
        # Let redaction own recursion and limits without deep-copying live transports.
        return {field.name: getattr(value, field.name) for field in fields(value)}
    return value


def _is_structured_mapping_key(value: object) -> bool:
    if type(value) is str:
        return False
    return isinstance(value, BaseModel | Mapping | list | tuple | set | frozenset) or (
        not isinstance(value, type) and is_dataclass(value)
    )


def _redact_mapping(
    value: Mapping[object, object],
    *,
    parent_key: str | None,
    depth: int,
    max_string_length: int | None,
    max_collection_items: int | None,
    max_depth: int | None,
    force_redact: bool,
    token_placeholders: dict[str, str] | None,
    ancestor_ids: frozenset[int],
) -> dict[str, _RedactedValue]:
    redacted: dict[str, _RedactedValue] = {}
    mapping_is_truncated = max_collection_items is not None and len(value) > max_collection_items
    has_secret_context_label = mapping_is_truncated or _mapping_has_secret_context_label(value)
    parent_is_query_container = _is_query_container(parent_key)
    items = list(value.items()) if max_collection_items is None else list(islice(value.items(), max_collection_items))
    key_texts = [_safe_str(key) for key, _ in items]
    reserved_keys: set[str] | None = None
    for index, (key, item) in enumerate(items):
        key_text = key_texts[index]
        classification = _classify_key(key)
        redact_key = (
            _should_redact_value_for_key(key, item)
            or (parent_is_query_container and classification.is_redacted_query)
            or (has_secret_context_label and classification.is_context_secret_value)
        )
        # Structured keys may hide secrets behind custom displays; keep them opaque.
        if _is_structured_mapping_key(key):
            if reserved_keys is None:
                reserved_keys = {
                    key_texts[item_index]
                    for item_index, (item_key, _) in enumerate(items)
                    if not _is_structured_mapping_key(item_key)
                }
            label_index = index
            redacted_key = f"<redacted structured key {label_index}>"
            while redacted_key in reserved_keys or redacted_key in redacted:
                label_index += 1
                redacted_key = f"<redacted structured key {label_index}>"
        elif token_placeholders is not None:
            # Keys get the same review treatment as text; keys that become equal stay distinct.
            redacted_key = _redact_review_tokens(key_text, max_length=None, placeholders=token_placeholders)
            while redacted_key in redacted:
                redacted_key += "\ufffd"
        else:
            redacted_key = key_text
        redacted[redacted_key] = _redact_sensitive_data(
            item,
            max_string_length=max_string_length,
            max_collection_items=max_collection_items,
            max_depth=max_depth,
            _parent_key=key_text,
            _depth=depth + 1,
            _force_redact=force_redact or redact_key,
            _ancestor_ids=ancestor_ids,
            token_placeholders=token_placeholders,
        )
    if mapping_is_truncated:
        redacted["__truncated__"] = f"{len(value) - len(items)} more items"
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
    token_placeholders: dict[str, str] | None,
    ancestor_ids: frozenset[int],
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
            _ancestor_ids=ancestor_ids,
            token_placeholders=token_placeholders,
        )
        for item in items
    ]
    if max_collection_items is not None and len(value) > max_collection_items:
        redacted_items.append(_TRUNCATED)
    return redacted_items


def _review_scalar_value(value: object, *, max_length: int | None, placeholders: dict[str, str]) -> _RedactedValue:
    """Return a reviewer-facing copy of one scalar that Matrix can deliver unchanged."""
    if isinstance(value, bytes):
        return "<bytes>"
    if isinstance(value, str | Path):
        return _redact_review_tokens(str(value), max_length=max_length, placeholders=placeholders)
    if isinstance(value, float):
        # Matrix events cannot carry floats, so a reviewer-facing copy shows every float as text.
        return str(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > _MAX_CANONICAL_JSON_INTEGER else value
    return _redact_review_tokens(_safe_repr(value), max_length=max_length, placeholders=placeholders)


def _redact_scalar_value(
    value: object,
    *,
    parent_key: str | None,
    max_string_length: int | None,
    force_redact: bool,
    token_placeholders: dict[str, str] | None,
) -> _RedactedValue:
    if force_redact or (parent_key is not None and _should_redact_value_for_key(parent_key, value)):
        return REDACTED
    if token_placeholders is not None:
        return _review_scalar_value(value, max_length=max_string_length, placeholders=token_placeholders)
    if isinstance(value, bytes):
        redacted: _RedactedValue = "<bytes>"
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
    token_placeholders: dict[str, str] | None = None,
    _parent_key: str | None = None,
    _depth: int = 0,
    _force_redact: bool = False,
    _ancestor_ids: frozenset[int] = frozenset(),
) -> _RedactedValue:
    if max_depth is not None and _depth >= max_depth:
        return _TRUNCATED
    value_type = type(value)
    # Built-in scalars cannot recurse; subclasses may still have structured fields.
    if (
        value_type is str
        or value_type is int
        or value_type is float
        or value_type is bool
        or value_type is bytes
        or value is None
    ):
        return _redact_scalar_value(
            value,
            parent_key=_parent_key,
            max_string_length=max_string_length,
            force_redact=_force_redact,
            token_placeholders=token_placeholders,
        )
    value_id = id(value)
    if value_id in _ancestor_ids:
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
            ancestor_ids=_ancestor_ids | {value_id},
            token_placeholders=token_placeholders,
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
            ancestor_ids=_ancestor_ids | {value_id},
            token_placeholders=token_placeholders,
        )
    else:
        redacted = _redact_scalar_value(
            value,
            parent_key=_parent_key,
            max_string_length=max_string_length,
            force_redact=_force_redact,
            token_placeholders=token_placeholders,
        )
    return redacted


def redact_sensitive_data(
    value: object,
    *,
    max_string_length: int | None = None,
    max_collection_items: int | None = None,
    max_depth: int | None = None,
    token_placeholders: dict[str, str] | None = None,
) -> _RedactedValue:
    """Redact structured data without letting redaction break its caller.

    With ``token_placeholders``, every field whose name marks it as secret is still hidden
    whole, but inside text only credentials in known token formats are hidden, each distinct
    token as a numbered placeholder recorded in the mapping. Reviewer-facing copies such as
    tool approval cards use it, because hiding more of free text can hide what would run.
    """
    collection_limit = None if max_collection_items is None else max(max_collection_items, 0)
    depth_limit = _MAX_DEPTH if max_depth is None else min(max(max_depth, 0), _MAX_DEPTH)
    try:
        return _redact_sensitive_data(
            value,
            max_string_length=max_string_length,
            max_collection_items=collection_limit,
            max_depth=depth_limit,
            token_placeholders=token_placeholders,
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
