"""Credential redaction helpers for knowledge Git URLs.

Deliberately the same logic as ``origin/main``, made *total*. ``urlsplit`` raises
when a netloc holds a codepoint that NFKC-normalises to a URL delimiter --
U+FF20 for ``@``, U+FF1A for ``:`` -- and these helpers were called unguarded, so
a malformed URL turned a Git failure into an unrelated ``ValueError`` while that
failure was being recorded, and left the knowledge API returning 500 for as long
as the error stayed persisted. No credential is needed to reach any of that.

Widening *what* gets redacted is a separate and larger question:
``src/mindroom/redaction.py`` is a second, older redactor wired in as a global
structlog processor, so it sees every URL this codebase logs rather than only
the knowledge paths. Changes to redaction breadth belong there, applied to both,
not here in isolation.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from base64 import b64decode
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse, urlunparse

from mindroom.git_urls import credential_free_repo_url

if TYPE_CHECKING:
    from urllib.parse import ParseResult

_URL_PATTERN: re.Pattern[str] = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+")
_AUTHORIZATION_HEADER_PATTERN: re.Pattern[str] = re.compile(
    r"\bAuthorization:\s*(Basic|Bearer)\s+([^\s'\"<>]+)",
    re.IGNORECASE,
)
#: Longest URL the ``.git/config`` write gate will decode. ``fully_unquoted`` is
#: quadratic in its input's nesting depth, and a real repository URL is orders of
#: magnitude shorter, so the gate refuses anything past this rather than decoding
#: it. Deliberately *not* applied when redacting diagnostics: nothing on that
#: path decodes, and bounding reads only costs error messages.
MAX_REDACTABLE_TOKEN_LENGTH = 2048
__all__ = [
    "MAX_REDACTABLE_TOKEN_LENGTH",
    "credential_free_repo_url",
    "credential_free_url_identity",
    "embedded_http_userinfo",
    "fully_unquoted",
    "redact_credentials_in_text",
    "redact_url_credentials",
]


def _strip_path_params(path: str) -> str:
    return path.split(";", 1)[0]


def fully_unquoted(value: str) -> str:
    """Return `value` percent-decoded to a fixed point and NFKC-normalised.

    Used by the ``.git/config`` write gate to decide whether a URL's separators
    are where they appear to be. A separator can hide under any number of
    encoding layers -- ``%40``, ``%2540``, ``%252540`` -- so checking one layer
    buys one layer, and picking a depth limit invites depth+1. Decoding to a
    fixed point has no limit to beat: every pass that changes anything replaces
    a three-character escape with one character, so the string strictly shortens
    and the loop ends with no escapes left.

    A separator can also hide as a different codepoint: U+FF20 NFKC-normalises
    to ``@``, which is why ``urlsplit`` rejects it. Normalising means the
    caller's separator count sees one ``@`` either way.
    """
    while True:
        decoded = unquote(value)
        if decoded == value:
            return unicodedata.normalize("NFKC", value)
        value = decoded


def _inspectable_url(value: str) -> ParseResult | None:
    """Return the parsed URL, or None when it cannot be inspected.

    The reason this exists: ``urlparse`` raises on a netloc holding a codepoint
    that NFKC-normalises to a delimiter, and every caller here used to let that
    propagate. Reaching it needs no credential, so an ordinary typo in
    ``repo_url`` was enough to replace a Git failure with a ``ValueError`` while
    the failure was being recorded.

    Deliberately applies no length limit. ``MAX_REDACTABLE_TOKEN_LENGTH`` exists
    for ``fully_unquoted``, which is quadratic in its input's nesting depth, and
    which only the ``.git/config`` write path calls. Bounding here instead meant
    a long but perfectly parseable URL in a Git or Git LFS error was replaced
    wholesale, losing a diagnostic that carried no credential -- a write-path
    policy applied to reads.
    """
    try:
        return urlparse(value)
    except ValueError:
        return None


def redact_url_credentials(value: str) -> str:
    """Redact URL credentials for any parsed URL scheme.

    Never raises. A URL that cannot be parsed is replaced wholesale rather than
    returned unchanged: unparseable is not the same as credential-free, because
    the thing defeating the parse can be the userinfo separator itself.
    """
    parsed = _inspectable_url(value)
    if parsed is None:
        return "***"
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
            except ValueError:
                # Covers every "not a decodable Basic token" outcome, including
                # a non-ASCII token, which makes ``b64decode`` raise a bare
                # ``ValueError`` rather than ``binascii.Error``. The header is
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
    return _URL_PATTERN.sub(lambda match: redact_url_credentials(match.group(0)), redacted)


def credential_free_url_identity(value: str) -> str:
    """Return a stable repo URL identity that never persists secret-bearing userinfo.

    Not duplication of ``mindroom/redaction.py``: this must return a *usable*
    identity for comparison, so ``***`` is not an available answer.

    Reached from ``indexing_settings_key`` on the ordinary resolve path rather
    than an error path, so it must not raise on account of a URL's contents. A
    URL ``urlparse`` refuses is hashed raw, which keeps the identity stable and
    puts nothing recoverable in the output.
    """
    parsed = _inspectable_url(value)
    if parsed is not None and parsed.scheme and parsed.netloc:
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
    """Return embedded HTTP(S) URL userinfo, if present.

    Never raises: a URL ``urlparse`` refuses has no userinfo this can use, and
    its exception message would quote the credential being looked for.
    """
    parsed = _inspectable_url(value)
    if parsed is None:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "@" not in parsed.netloc:
        return None
    if not parsed.username:
        return None
    return unquote(parsed.username), unquote(parsed.password or "")
