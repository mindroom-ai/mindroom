"""Credential redaction helpers for knowledge Git URLs."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")


def redact_url_credentials(value: str) -> str:
    """Redact all HTTP(S) URL userinfo credentials."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or "@" not in parsed.netloc:
        return value

    _userinfo, host = parsed.netloc.rsplit("@", 1)
    return urlunparse(parsed._replace(netloc=f"***@{host}"))


def redact_credentials_in_text(value: str) -> str:
    """Redact credential-bearing URLs embedded inside free-form text."""
    return _URL_PATTERN.sub(lambda match: redact_url_credentials(match.group(0)), value)


def credential_free_url_identity(value: str) -> str:
    """Return a stable repo URL identity that never persists URL userinfo."""
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
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
