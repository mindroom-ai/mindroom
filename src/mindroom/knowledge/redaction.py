"""Credential redaction helpers for knowledge Git URLs."""

from __future__ import annotations

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
