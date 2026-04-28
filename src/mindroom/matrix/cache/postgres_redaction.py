"""Secret redaction helpers for PostgreSQL connection strings."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit

_REDACTED = "***"
_POSTGRES_SECRET_KEYS = frozenset({"password", "passfile", "sslpassword"})
_LIBPQ_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<prefix>(?<!\S)(?:password|passfile|sslpassword)\s*=\s*)"
    r"(?P<value>'(?:\\.|[^\\'])*'|\"(?:\\.|[^\\\"])*\"|\S*)",
    re.IGNORECASE,
)


def _redact_url_query(query: str) -> str:
    if not query:
        return query
    query_items = parse_qsl(query, keep_blank_values=True)
    redacted_parts = []
    for key, value in query_items:
        encoded_key = quote_plus(key)
        encoded_value = _REDACTED if key.lower() in _POSTGRES_SECRET_KEYS else quote_plus(value)
        redacted_parts.append(f"{encoded_key}={encoded_value}")
    return "&".join(redacted_parts)


def _redact_url_conninfo(conninfo: str) -> str:
    parts = urlsplit(conninfo)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{_REDACTED}@{netloc.rsplit('@', 1)[1]}"
    return urlunsplit(
        (
            parts.scheme,
            netloc,
            parts.path,
            _redact_url_query(parts.query),
            parts.fragment,
        ),
    )


def _redact_libpq_conninfo(conninfo: str) -> str:
    return _LIBPQ_SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group('prefix')}{_REDACTED}", conninfo)


def redact_postgres_connection_info(conninfo: str) -> str:
    """Return a log-safe PostgreSQL URL or libpq conninfo string."""
    if "://" in conninfo:
        return _redact_url_conninfo(conninfo)
    return _redact_libpq_conninfo(conninfo)
