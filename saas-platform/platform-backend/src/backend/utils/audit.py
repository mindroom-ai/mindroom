"""
Shared audit logging utilities.
KISS principle - simple function for consistent audit logging.
"""

from datetime import UTC, datetime
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from backend.config import supabase

logger = logging.getLogger(__name__)
REDACTED = "***redacted***"
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
_SECRET_KEY_VARIANTS = tuple(
    (key, key.replace("_", ""), tuple(key.split("_"))) for key in sorted(_SECRET_KEYS, key=len, reverse=True)
)


def _normalize_key(value: object) -> str:
    key = str(value).strip()
    key = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def _is_secret_key(value: object) -> bool:
    normalized = _normalize_key(value)
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


def _redact_nested_assignment_value(match: re.Match[str]) -> str:
    quote = match.group("quote")
    if quote is not None:
        quoted_value = match.group("quoted_value")
        if quoted_value is None:
            return match.group(0)
        return f"{match.group('prefix')}{quote}{redact_audit_text(quoted_value)}{quote}"
    value = match.group("value")
    if value is None:
        return match.group(0)
    return match.group("prefix") + redact_audit_text(value)


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
    for key, item in parse_qsl(value, keep_blank_values=True):
        if _is_redacted_query_key(key):
            query_items.append((key, REDACTED))
            changed = True
        else:
            query_items.append((key, item))
    if not changed:
        return redact_audit_text(value)
    return urlencode(query_items, doseq=True, safe="*")


def redact_audit_text(value: str) -> str:
    """Redact credential-bearing values from free-form audit text."""
    redacted = _URL_PATTERN.sub(lambda match: _redact_url(match.group(0)), value)
    redacted = _BEARER_TOKEN_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _API_KEY_MESSAGE_PATTERN.sub(_redact_matched_token, redacted)
    redacted = _TOKEN_LIKE_PATTERN.sub(_redact_matched_token, redacted)
    return _SECRET_ASSIGNMENT_PATTERN.sub(_redact_secret_assignment, redacted)


def _redact_audit_details(value: Any, parent_key: str | None) -> Any:  # noqa: ANN401
    if isinstance(value, dict):
        return {
            str(key): REDACTED
            if _is_secret_key(key) or (_is_query_container(parent_key) and _is_redacted_query_key(key))
            else _redact_audit_details(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_details(item, parent_key=parent_key) for item in value]
    if isinstance(value, str):
        if _is_query_container(parent_key):
            return _redact_query_fragment(value)
        return redact_audit_text(value)
    return value


def redact_audit_details(value: Any) -> Any:  # noqa: ANN401
    """Recursively redact credential-bearing fields from audit details."""
    return _redact_audit_details(value, parent_key=None)


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
