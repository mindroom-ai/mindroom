"""Credential redaction helpers for knowledge Git URLs."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

_URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")


def redact_url_credentials(value: str) -> str:
    """Redact URL credentials for any parsed URL scheme."""
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        return value

    if "@" in parsed.netloc:
        _userinfo, host = parsed.netloc.rsplit("@", 1)
        netloc = f"***@{host}"
    else:
        netloc = parsed.netloc
    return urlunparse(parsed._replace(netloc=netloc, query="", fragment=""))


def redact_credentials_in_text(value: str) -> str:
    """Redact credential-bearing URLs embedded inside free-form text."""
    return _URL_PATTERN.sub(lambda match: redact_url_credentials(match.group(0)), value)


def credential_free_url_identity(value: str) -> str:
    """Return a stable repo URL identity that never persists URL userinfo."""
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.rsplit("@", 1)[-1].lower()
        normalized = urlunparse(
            parsed._replace(
                scheme=parsed.scheme.lower(),
                netloc=host,
                params="",
                query="",
                fragment="",
            ),
        )
    else:
        normalized = value
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"repo-url-sha256:{digest}"
