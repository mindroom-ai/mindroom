"""
Shared audit logging utilities.
KISS principle - simple function for consistent audit logging.
"""

from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from backend.config import supabase
from fastapi import Request

logger = logging.getLogger(__name__)
REDACTED = "***redacted***"
_REDACTED_BEARER = f"bearer {REDACTED}"
_AUTHORIZATION_SCHEMES = frozenset({"basic", "bearer"})
TRUNCATED = "... [truncated]"
# Audit text is cut to this length; redaction scans a little further so a secret straddling the cut is still masked.
MAX_AUDIT_TEXT_LENGTH = 4 * 1024
_REDACTION_LOOKAHEAD_CHARS = 512
MAX_AUDIT_DEPTH = 32
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
# Keys start only at a key-character boundary and quantifiers are possessive, so the scan never restarts inside a run.
_ASSIGNMENT_PREFIX_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])[\"']?(?P<key>[A-Za-z0-9_.-]++)[\"']?\s*+[:=]\s*+")
# An unquoted value ends at a delimiter or at the next assignment; one alternation finds the nearer without rescanning.
_ASSIGNMENT_VALUE_END_PATTERN = re.compile(
    r"[\r\n,&)\]}]|(?<!\s)\s++(?:and\s++)?[\"']?[A-Za-z0-9_.-]++[\"']?\s*+[:=]", re.IGNORECASE
)
_QUOTE_PATTERN = re.compile(r"[\"']")
_TRAILING_SPACE_PATTERN = re.compile(r"\s*+\Z")
_LINE_BREAK_PATTERN = re.compile(r"[\r\n]")
_ACRONYM_BOUNDARY_PATTERN = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_CAMEL_BOUNDARY_PATTERN = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALPHANUMERIC_RUN_PATTERN = re.compile(r"[^a-z0-9]+")
_TOKEN_LIKE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?P<token>("
    r"(?:sk|pk)-[A-Za-z0-9._-]+"
    r"|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9._-]+"
    r"|xox[baprs]-[A-Za-z0-9-]+"
    r"|gh(?:p|o|u|s|r)_[A-Za-z0-9_]+"
    r"|github_pat_[A-Za-z0-9_]+"
    r"|AIza[0-9A-Za-z_-]+"
    r"))(?![A-Za-z0-9])"
)
_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "credit_card",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "set_cookie",
        "token",
    }
)
_OAUTH_QUERY_KEYS = frozenset({"code", "state"})
_URL_QUERY_SECRET_KEYS = frozenset(
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
    }
)
_QUERY_CONTAINER_KEYS = frozenset({"query", "query_params", "query_string", "callback_query"})
# Each secret key as a whole-part window (`_api_key_`) and in compact spelling (`apikey`).
_SECRET_KEY_VARIANTS = tuple((f"_{key}_", key.replace("_", "")) for key in _SECRET_KEYS)


def _normalize_key(value: object) -> str:
    key = _ACRONYM_BOUNDARY_PATTERN.sub("_", str(value).strip())
    key = _CAMEL_BOUNDARY_PATTERN.sub(r"\1_\2", key)
    return _NON_ALPHANUMERIC_RUN_PATTERN.sub("_", key.lower()).strip("_")


def _is_secret_key(value: object) -> bool:
    """Return whether a key names a secret: a secret key appears as whole parts, or its compact spelling ends one."""
    normalized = _normalize_key(value)
    parts = f"_{normalized}_"
    compact = normalized.replace("_", "")
    return any(window in parts or compact.endswith(compact_key) for window, compact_key in _SECRET_KEY_VARIANTS)


def _is_query_container(value: str) -> bool:
    return _normalize_key(value) in _QUERY_CONTAINER_KEYS


def _is_redacted_query_key(value: object) -> bool:
    normalized = _normalize_key(value)
    return _is_secret_key(value) or normalized in _OAUTH_QUERY_KEYS or normalized in _URL_QUERY_SECRET_KEYS


def _redact_matched_token(match: re.Match[str]) -> str:
    group_start, group_end = match.span("token")
    full_match = match.group(0)
    prefix_end = group_start - match.start()
    suffix_start = group_end - match.start()
    return full_match[:prefix_end] + REDACTED + full_match[suffix_start:]


@dataclass(frozen=True)
class _QuoteIndex:
    """Positions, found in one pass over a text, that bound quoted assignment values."""

    closing_quotes: dict[str, list[int]]
    line_breaks: list[int]


def _index_quotes(value: str) -> _QuoteIndex:
    """Find every quote that can close a value: one followed by a delimiter, the next assignment, or only whitespace.

    Requiring what follows the quote keeps a quote inside the value (as in `'it's'`) from ending it early,
    and indexing once keeps later lookups from rescanning the text.
    """
    closing_quotes: dict[str, list[int]] = {"'": [], '"': []}
    for match in _QUOTE_PATTERN.finditer(value):
        after = match.end()
        if _ASSIGNMENT_VALUE_END_PATTERN.match(value, after) or _TRAILING_SPACE_PATTERN.match(value, after):
            closing_quotes[match.group()].append(match.start())
    return _QuoteIndex(
        closing_quotes=closing_quotes, line_breaks=[match.start() for match in _LINE_BREAK_PATTERN.finditer(value)]
    )


def _closing_quote(quotes: _QuoteIndex, quote: str, start: int) -> int | None:
    """Return the index of the quote closing a value that starts at `start` on the same line, or None."""
    positions = quotes.closing_quotes[quote]
    candidate_index = bisect_left(positions, start)
    if candidate_index == len(positions):
        return None
    candidate = positions[candidate_index]
    line_break_index = bisect_left(quotes.line_breaks, start)
    if line_break_index < len(quotes.line_breaks) and quotes.line_breaks[line_break_index] < candidate:
        return None
    return candidate


@dataclass
class _ValueEndFinder:
    """Find where unquoted values end in one text, reusing the last result because lookups only move forward.

    A search from any position between the last search start and its match finds that same match,
    so every character is searched at most once.
    """

    text: str
    searched_from: int = -1
    found: int = -1

    def after(self, position: int) -> int:
        """Return where an unquoted value whose first character precedes `position` ends."""
        if not self.searched_from <= position <= self.found:
            match = _ASSIGNMENT_VALUE_END_PATTERN.search(self.text, position)
            self.searched_from = position
            self.found = match.start() if match else len(self.text)
        return self.found


def _assignment_value_span(
    value: str, value_start: int, quotes: _QuoteIndex, value_ends: _ValueEndFinder
) -> tuple[int, int] | None:
    """Return the span of one assigned value, or None at the end of the text.

    A quoted value ends at its closing quote.
    An unquoted or unclosed one always includes its first character, even a delimiter, and then ends at a delimiter
    or the next assignment.
    """
    if value_start >= len(value):
        return None
    if value[value_start] in {"'", '"'}:
        closing = _closing_quote(quotes, value[value_start], value_start + 1)
        if closing is not None:
            return value_start + 1, closing
    return value_start, value_ends.after(value_start + 1)


def _redact_secret_assignments(value: str) -> str:
    """Redact the values of secret key assignments in one forward scan.

    Scanning continues inside every value, kept or redacted, so assignments nested there are still found
    without recursion; overlapping redacted values merge into one.
    """
    quotes = _index_quotes(value)
    value_ends = _ValueEndFinder(value)
    spans: list[tuple[int, int]] = []
    search_start = 0
    while prefix := _ASSIGNMENT_PREFIX_PATTERN.search(value, search_start):
        search_start = prefix.end()
        key = prefix.group("key")
        if not _is_secret_key(key):
            continue
        is_authorization = _normalize_key(key) == "authorization"
        # An already redacted bearer token is kept, and scanning resumes right after it.
        if is_authorization and value[search_start : search_start + len(_REDACTED_BEARER)].lower() == _REDACTED_BEARER:
            search_start += len(_REDACTED_BEARER)
            continue
        span = _assignment_value_span(value, search_start, quotes, value_ends)
        if span is None:
            continue
        value_start, value_end = span
        # A bare, unquoted authorization scheme carries no credential.
        if (
            is_authorization
            and value_start == search_start
            and value[value_start:value_end].lower() in _AUTHORIZATION_SCHEMES
        ):
            continue
        if spans and value_start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], value_end))
        else:
            spans.append((value_start, value_end))
    parts: list[str] = []
    copied_until = 0
    for value_start, value_end in spans:
        parts.extend((value[copied_until:value_start], REDACTED))
        copied_until = value_end
    parts.append(value[copied_until:])
    return "".join(parts)


def _redaction_input(value: str) -> str:
    return value[: MAX_AUDIT_TEXT_LENGTH + _REDACTION_LOOKAHEAD_CHARS]


def _truncate_audit_text(value: str) -> str:
    if len(value) <= MAX_AUDIT_TEXT_LENGTH:
        return value
    return value[: MAX_AUDIT_TEXT_LENGTH - len(TRUNCATED)] + TRUNCATED


def _redact_url(value: str) -> str:
    try:
        parsed = urlparse(value)
    except ValueError:
        return REDACTED
    if parsed.scheme not in {"http", "https"}:
        return value

    netloc = parsed.netloc
    query = parsed.query
    changed = False
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        netloc = f"{userinfo.split(':', 1)[0]}:***@{host}" if ":" in userinfo else f"***@{host}"
        changed = True

    if not query:
        return urlunparse(parsed._replace(netloc=netloc)) if changed else value

    query_items: list[tuple[str, str]] = []
    for key, item in parse_qsl(query, keep_blank_values=True):
        if _is_redacted_query_key(key):
            query_items.append((key, REDACTED))
            changed = True
        else:
            query_items.append((key, item))
    if not changed:
        return value
    return urlunparse(parsed._replace(netloc=netloc, query=urlencode(query_items, doseq=True, safe="*")))


def _redact_query_fragment(value: str) -> str:
    query_items: list[tuple[str, str]] = []
    changed = False
    for key, item in parse_qsl(_redaction_input(value), keep_blank_values=True):
        if _is_redacted_query_key(key):
            query_items.append((key, REDACTED))
            changed = True
        else:
            query_items.append((key, item))
    if not changed:
        return redact_audit_text(value)
    return _truncate_audit_text(urlencode(query_items, doseq=True, safe="*"))


def redact_audit_text(value: str) -> str:
    """Redact credential-bearing values from free-form audit text and cut it to `MAX_AUDIT_TEXT_LENGTH`."""
    redacted = _URL_PATTERN.sub(lambda match: _redact_url(match.group(0)), _redaction_input(value))
    redacted = _BEARER_TOKEN_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _API_KEY_MESSAGE_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _TOKEN_LIKE_PATTERN.sub(_redact_matched_token, redacted)
    return _truncate_audit_text(_redact_secret_assignments(redacted))


def _redact_audit_details(value: Any, *, in_query_container: bool, depth: int) -> Any:  # noqa: ANN401
    if depth >= MAX_AUDIT_DEPTH:
        return TRUNCATED
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_secret_key(key_text) or (in_query_container and _is_redacted_query_key(key_text)):
                redacted_item = REDACTED
            else:
                redacted_item = _redact_audit_details(
                    item, in_query_container=_is_query_container(key_text), depth=depth + 1
                )
            redacted[_truncate_audit_text(key_text)] = redacted_item
        return redacted
    if isinstance(value, list):
        return [_redact_audit_details(item, in_query_container=in_query_container, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return _redact_query_fragment(value) if in_query_container else redact_audit_text(value)
    return value


def redact_audit_details(value: Any) -> Any:  # noqa: ANN401
    """Recursively redact credential-bearing fields from audit details, bounding text length and nesting depth."""
    return _redact_audit_details(value, in_query_container=False, depth=0)


@dataclass(frozen=True)
class AuditActor:
    """Authenticated account an audited request is attributed to."""

    account_id: str
    email: str | None


def record_audit_actor(request: Request, actor: AuditActor) -> None:
    """Attribute the audit row of the current request to an authenticated account."""
    request.state.audit_actor = actor


def create_audit_log(
    action: str,
    resource_type: str,
    account_id: str = None,
    resource_id: str = None,
    details: dict = None,
    ip_address: str = None,
    success: bool = True,
) -> None:
    """
    Create an audit log entry in the database.

    Args:
        action: The action being performed (e.g., "auth_failed", "ip_blocked")
        resource_type: Type of resource (e.g., "authentication", "security")
        account_id: ID of the account performing the action
        resource_id: ID of the specific resource being acted upon
        details: Additional details about the action
        ip_address: IP address of the request
        success: Whether the action was successful
    """
    try:
        if not supabase:
            return

        log_entry = {
            "account_id": account_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "details": redact_audit_details(details),
            "ip_address": ip_address,
            "success": success,
            "created_at": datetime.now(UTC).isoformat(),
        }

        supabase.table("audit_logs").insert(log_entry).execute()
    except Exception as e:
        # Audit logging is best-effort, don't fail the main operation
        logger.error(f"Failed to create audit log: {e}")
