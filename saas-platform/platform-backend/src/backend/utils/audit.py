"""
Shared audit logging utilities.
KISS principle - simple function for consistent audit logging.
"""

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


def _is_query_container(value: str | None) -> bool:
    return value is not None and _normalize_key(value) in _QUERY_CONTAINER_KEYS


def _is_redacted_query_key(value: object) -> bool:
    normalized = _normalize_key(value)
    return _is_secret_key(value) or normalized in _OAUTH_QUERY_KEYS or normalized in _URL_QUERY_SECRET_KEYS


def _redact_matched_token(match: re.Match[str]) -> str:
    group_start, group_end = match.span("token")
    full_match = match.group(0)
    prefix_end = group_start - match.start()
    suffix_start = group_end - match.start()
    return full_match[:prefix_end] + REDACTED + full_match[suffix_start:]


def _closing_quote(value: str, quote: str, start: int) -> int | None:
    """Return the index of the unescaped quote closing a value on its own line, or None."""
    position = start
    while (position := value.find(quote, position)) >= 0:
        escape_start = position
        while escape_start > start and value[escape_start - 1] == "\\":
            escape_start -= 1
        if (position - escape_start) % 2 == 0:
            break
        position += 1
    if position < 0 or value.find("\n", start, position) >= 0 or value.find("\r", start, position) >= 0:
        return None
    return position


def _assignment_value_span(value: str, value_start: int) -> tuple[int, int] | None:
    """Return the span of one assigned value, or None when it is empty.

    A quoted value ends at its closing quote; an unquoted or unclosed one ends at a delimiter or the next assignment.
    """
    if value_start < len(value) and value[value_start] in {"'", '"'}:
        closing = _closing_quote(value, value[value_start], value_start + 1)
        if closing is not None:
            return value_start + 1, closing
    value_end_match = _ASSIGNMENT_VALUE_END_PATTERN.search(value, value_start)
    value_end = value_end_match.start() if value_end_match else len(value)
    return (value_start, value_end) if value_end > value_start else None


def _redact_secret_assignments(value: str) -> str:
    """Redact the values of secret key assignments in one forward scan.

    Scanning resumes inside every value it keeps, so assignments nested there are still found without recursion.
    """
    parts: list[str] = []
    copied_until = 0
    search_start = 0
    while prefix := _ASSIGNMENT_PREFIX_PATTERN.search(value, search_start):
        search_start = prefix.end()
        key = prefix.group("key")
        if not _is_secret_key(key):
            continue
        span = _assignment_value_span(value, prefix.end())
        if span is None:
            continue
        value_start, value_end = span
        assigned = value[value_start:value_end].lower()
        if _normalize_key(key) == "authorization" and (
            assigned in {"basic", "bearer"} or assigned.startswith(f"bearer {REDACTED}")
        ):
            continue
        parts.extend((value[copied_until:value_start], REDACTED))
        copied_until = search_start = value_end
    parts.append(value[copied_until:])
    return "".join(parts)


def _redaction_input(value: str) -> str:
    return value[: MAX_AUDIT_TEXT_LENGTH + _REDACTION_LOOKAHEAD_CHARS]


def _truncate_audit_text(value: str) -> str:
    if len(value) <= MAX_AUDIT_TEXT_LENGTH:
        return value
    return value[: MAX_AUDIT_TEXT_LENGTH - len(TRUNCATED)] + TRUNCATED


def _redact_url(value: str) -> str:
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


def _redact_audit_details(value: Any, parent_key: str | None, depth: int) -> Any:  # noqa: ANN401
    if depth >= MAX_AUDIT_DEPTH:
        return TRUNCATED
    if isinstance(value, dict):
        return {
            str(key): REDACTED
            if _is_secret_key(key) or (_is_query_container(parent_key) and _is_redacted_query_key(key))
            else _redact_audit_details(item, parent_key=str(key), depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_details(item, parent_key=parent_key, depth=depth + 1) for item in value]
    if isinstance(value, str):
        if _is_query_container(parent_key):
            return _redact_query_fragment(value)
        return redact_audit_text(value)
    return value


def redact_audit_details(value: Any) -> Any:  # noqa: ANN401
    """Recursively redact credential-bearing fields from audit details, bounding text length and nesting depth."""
    return _redact_audit_details(value, parent_key=None, depth=0)


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
