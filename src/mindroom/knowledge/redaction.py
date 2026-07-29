"""Credential redaction helpers for knowledge Git URLs."""

from __future__ import annotations

import hashlib
import re
from base64 import b64decode
from urllib.parse import unquote, urlparse, urlunparse

from mindroom.git_urls import credential_free_repo_url

_URL_PATTERN: re.Pattern[str] = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
#: Scheme-prefixed tokens that are missing the ``//`` and so never reach
#: ``_URL_PATTERN`` -- ``https:/user:secret@host/x``, ``https:@host/u:secret@h``.
#: Restricted to tokens that actually contain an ``@`` so ordinary Git output
#: (``fatal: ...``, ``main:refs/heads/main``) is not swept up; the token still
#: has to convince ``redact_url_credentials`` before anything is replaced.
_SLASHLESS_CREDENTIAL_URL_PATTERN: re.Pattern[str] = re.compile(
    r"[a-zA-Z][a-zA-Z0-9+.-]*:(?!//)[^\s'\"<>]*@[^\s'\"<>]*",
)
#: Whitespace is allowed around the colon: HTTP permits it, and a proxy or Git
#: helper that reformats a header it echoes back must not slip the credential
#: past this pattern.
_AUTHORIZATION_HEADER_PATTERN: re.Pattern[str] = re.compile(
    r"\bAuthorization\s*:\s*(Basic|Bearer)\s+([^\s'\"<>]+)",
    re.IGNORECASE,
)
#: Percent-encoded ``@``. Its presence means the authority ``urlparse`` reported
#: is not the authority a client will use, so the userinfo split cannot be trusted.
_ENCODED_USERINFO_SEPARATOR = "%40"
__all__ = [
    "credential_free_repo_url",
    "credential_free_url_identity",
    "embedded_http_userinfo",
    "redact_credentials_in_text",
    "redact_url_credentials",
]


def _strip_path_params(path: str) -> str:
    return path.split(";", 1)[0]


def redact_url_credentials(value: str) -> str:
    """Redact URL credentials for any parsed URL scheme.

    Never raises. Callers redact free-form Git output, so the input is whatever
    a remote or a local ``git`` chose to print, and a token that merely looks
    like a URL can be unparseable (an unterminated IPv6 literal, say). Raising
    there would replace the real Git failure with an unrelated ``ValueError``
    and destroy the diagnostic the caller was trying to report.
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        # Unparseable is not the same as credential-free: userinfo can sit
        # behind the part that failed to parse. This text reaches users and is
        # persisted as ``last_error``, so drop the whole token rather than risk
        # leaking a secret; the surrounding message survives intact.
        return "***"

    if not parsed.scheme:
        # Not a URL. Bare Git arguments and scp-style remotes (``git@host:path``)
        # reach this helper too; neither carries a password, so keep them readable.
        return value

    # Everything below assumes userinfo lives where ``urlparse`` says it does.
    # When it does not, redacting the authority leaves the secret in the text,
    # so these shapes are dropped wholesale instead:
    #   https:///user:secret@host/repo   empty authority, credentials in the path
    #   https://host/https://u:s@in/x    a second URL nested in the path
    #   https://u%3As%40host/repo        the separator is percent-encoded
    #   https://a@b@host/x               which ``@`` splits userinfo is ambiguous
    authority_separators = parsed.netloc.count("@")
    if (
        _ENCODED_USERINFO_SEPARATOR in value.lower()
        or value.count("@") != authority_separators
        or authority_separators > 1
    ):
        return "***"

    if not parsed.netloc:
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
            except ValueError:
                # Covers every "this is not a decodable Basic token" outcome,
                # including the non-ASCII token that makes b64decode raise a
                # bare ValueError rather than binascii.Error. The header is
                # redacted either way; only the decoded secret is unavailable.
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

    def _redact_url(match: re.Match[str]) -> str:
        return redact_url_credentials(match.group(0))

    redacted = _URL_PATTERN.sub(_redact_url, redacted)
    return _SLASHLESS_CREDENTIAL_URL_PATTERN.sub(_redact_url, redacted)


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
