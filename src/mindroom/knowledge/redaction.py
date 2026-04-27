"""Credential redaction helpers for knowledge Git URLs."""

from __future__ import annotations

import hashlib
import re
from base64 import b64decode
from binascii import Error as BinasciiError
from urllib.parse import unquote, urlparse, urlunparse

_URL_PATTERN: re.Pattern[str] = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
_AUTHORIZATION_HEADER_PATTERN: re.Pattern[str] = re.compile(
    r"\bAuthorization:\s*(Basic|Bearer)\s+([^\s'\"<>]+)",
    re.IGNORECASE,
)


def _strip_path_params(path: str) -> str:
    return path.split(";", 1)[0]


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
    return urlunparse(
        parsed._replace(
            netloc=netloc,
            path=_strip_path_params(parsed.path),
            params="",
            query="",
            fragment="",
        ),
    )


def redact_credentials_in_text(value: str) -> str:
    """Redact credential-bearing URLs and auth headers embedded inside free-form text."""
    decoded_basic_values: list[str] = []

    def _redact_authorization_header(match: re.Match[str]) -> str:
        scheme = match.group(1)
        token = match.group(2)
        if scheme.lower() == "basic":
            try:
                decoded = b64decode(token, validate=True).decode("utf-8")
            except (BinasciiError, UnicodeDecodeError):
                pass
            else:
                if decoded:
                    decoded_basic_values.append(decoded)
                if ":" in decoded:
                    secret = decoded.split(":", 1)[1]
                    if secret:
                        decoded_basic_values.append(secret)
        return f"Authorization: {scheme} ***"

    redacted: str = _AUTHORIZATION_HEADER_PATTERN.sub(_redact_authorization_header, value)
    unique_decoded_values = list(set(decoded_basic_values))
    unique_decoded_values.sort(key=len, reverse=True)
    for decoded_value in unique_decoded_values:
        redacted = redacted.replace(decoded_value, "***")
    return _URL_PATTERN.sub(lambda match: redact_url_credentials(match.group(0)), redacted)


def credential_free_url_identity(value: str) -> str:
    """Return a stable repo URL identity that never persists secret-bearing userinfo."""
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        netloc = parsed.netloc.rsplit("@", 1)[-1].lower()
        if parsed.scheme == "ssh" and "@" in parsed.netloc and parsed.password is None:
            userinfo, host = parsed.netloc.rsplit("@", 1)
            if userinfo and ":" not in userinfo:
                netloc = f"{userinfo}@{host.lower()}"
        normalized = urlunparse(
            parsed._replace(
                scheme=parsed.scheme.lower(),
                netloc=netloc,
                path=_strip_path_params(parsed.path),
                params="",
                query="",
                fragment="",
            ),
        )
    else:
        normalized = value
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"repo-url-sha256:{digest}"


def embedded_http_userinfo(value: str) -> tuple[str, str] | None:
    """Return embedded HTTP(S) URL userinfo, if present."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "@" not in parsed.netloc:
        return None
    if not parsed.username:
        return None
    return unquote(parsed.username), unquote(parsed.password or "")
