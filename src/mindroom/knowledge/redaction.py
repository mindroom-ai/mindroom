"""Credential redaction helpers for knowledge Git URLs."""

from __future__ import annotations

import hashlib
import re
from base64 import b64decode
from urllib.parse import ParseResult, unquote, urlparse, urlunparse

from mindroom.git_urls import credential_free_repo_url

#: One whitespace-delimited run, which is the unit everything here classifies.
#:
#: Deliberately the *only* pattern scanned over free text. Recognising URL
#: shapes with their own regexes meant several patterns each scanning from every
#: position they could start at, and one of them -- protocol-relative tokens,
#: which can begin at any ``//`` -- degraded to O(n^2): 195 KB of slashes took
#: 148 s, inline in the coroutine reading Git's stderr, which a remote controls.
#: A single alternation-free tokenizer visits each character once, and the
#: length bound below can be applied per token *before* any work happens,
#: because the token is in hand before classification starts rather than after.
#: Brackets are delimiters like quotes: Git wraps remotes in them
#: (``Submodule 'v' (git@host:org/x.git) registered``), and a token that carries
#: its closing paren does not match an anchored shape.
_TOKEN_PATTERN: re.Pattern[str] = re.compile(r"[^\s'\"<>()]+")
#: Whitespace is allowed around the colon: HTTP permits it, and a proxy or Git
#: helper that reformats a header it echoes back must not slip the credential
#: past this pattern.
_AUTHORIZATION_HEADER_PATTERN: re.Pattern[str] = re.compile(
    r"\bAuthorization\s*:\s*(Basic|Bearer)\s+([^\s'\"<>]+)",
    re.IGNORECASE,
)
#: Tokens worth classifying: an explicit transport URL, a scheme-with-colon that
#: lost its slashes, a protocol-relative URL, or scp-style SSH syntax. Anything
#: else in Git's output is left alone, which is what keeps revision syntax like
#: ``+refs/heads/main:refs/remotes/origin/@{upstream}`` readable.
_URL_LIKE_TOKEN: re.Pattern[str] = re.compile(
    r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*://|//|(?:https?|ssh|git|git\+[a-z]+|ftps?|file):)",
    re.IGNORECASE,
)
#: ``git@github.com:org/repo.git``. SSH has no URL password field, so the
#: userinfo here is a username; it is dropped anyway, and only the host and path
#: are kept so the remote stays identifiable in an error message. Underscores are
#: not valid in hostnames but ssh and Git both accept them, and internal hosts
#: use them. Userinfo is required: without it there is nothing to redact, and an
#: optional one lets this swallow ordinary ``scheme:path`` tokens.
_SCP_STYLE_REMOTE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9._-]+@(?P<rest>(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\]):[^@]*)$",
)
#: ``oauth2:glpat_XXX@gitlab.com:org/repo.git`` and
#: ``x-access-token:ghp_XXX@github.com/org/repo.git`` -- the documented GitLab
#: and GitHub credential forms with the scheme dropped, which is the likeliest
#: operator mistype. Not a URL by any syntax, so nothing above matches them.
#:
#: The password may not contain ``/`` and may not be empty, which is what keeps
#: Git revision syntax out: ``HEAD:@{upstream}`` has nothing between the colon
#: and the ``@``, and ``main:refs/remotes/origin/@{u}`` has a slash there.
_USERINFO_LIKE_TOKEN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._-]+:[^/@]+@")
__all__ = [
    "credential_free_repo_url",
    "credential_free_url_identity",
    "embedded_http_userinfo",
    "fully_unquoted",
    "redact_credentials_in_text",
    "redact_url_credentials",
]


def _strip_path_params(path: str) -> str:
    return path.split(";", 1)[0]


#: Longest token `redact_url_credentials` will inspect. Decoding to a fixed
#: point is quadratic in the nesting depth an attacker chooses -- a 64 KB
#: single-token payload of nested escapes takes ~1.8 s -- and it runs inline in
#: the coroutine that reads Git's stderr, which a remote controls via sideband
#: output. A real URL is orders of magnitude shorter, so anything past this is
#: replaced unread: fail closed, and bound the cost at the same time.
_MAX_REDACTABLE_TOKEN_LENGTH = 2048


def fully_unquoted(value: str) -> str:
    """Percent-decode `value` repeatedly until it stops changing.

    A separator can be hidden under any number of encoding layers -- ``%40``,
    ``%2540``, ``%252540`` -- so testing one layer only buys one layer, and
    picking a depth limit just invites depth+1. Decoding to a fixed point has no
    limit to beat: every pass that changes anything replaces a three-character
    escape with one character, so the string strictly shortens and the loop
    terminates with no escapes left to hide behind.
    """
    while True:
        decoded = unquote(value)
        if decoded == value:
            return value
        value = decoded


def _inspectable_url(value: str) -> ParseResult | None:
    """Return the parsed token, or None when it must be dropped unread."""
    if len(value) > _MAX_REDACTABLE_TOKEN_LENGTH:
        return None
    try:
        return urlparse(value)
    except ValueError:
        return None


def redact_url_credentials(value: str) -> str:
    """Return one token with any credential material removed.

    Fails closed, and the branch order is the point. Three shapes are
    recognised; anything else is replaced wholesale, so a shape nobody
    anticipated is redacted *by default* rather than preserved by default.
    Every leak found in this helper came from the opposite arrangement -- an
    exemption that returned a token unchanged because it did not look like it
    carried a credential -- so recognising safety rather than danger is what
    stops the next unanticipated shape.

    Never raises. Callers redact free-form Git output, so the input is whatever
    a remote or a local ``git`` chose to print, and a token that merely looks
    like a URL can be unparseable (an unterminated IPv6 literal, say). Raising
    there would replace the real Git failure with an unrelated ``ValueError``
    and destroy the diagnostic the caller was trying to report.
    """
    parsed = _inspectable_url(value)
    if parsed is None:
        # Too long to inspect, or unparseable. Unparseable is not the same as
        # credential-free -- userinfo can sit behind the part that failed to
        # parse -- and this text reaches users and is persisted as
        # ``last_error``, so drop the whole token rather than risk leaking a
        # secret. The surrounding message survives intact.
        return "***"

    # Every separator test below runs on the fully decoded form, so an encoded
    # separator is caught at any depth rather than one layer at a time.
    normalized = fully_unquoted(value)

    if "@" not in normalized:
        # Shape 1: no userinfo separator anywhere, so there is nothing to hide.
        # Bare Git arguments and credential-free URLs both land here.
        if not parsed.scheme or not parsed.netloc:
            return value
        return urlunparse(
            parsed._replace(path=_strip_path_params(parsed.path), params="", query="", fragment=""),
        )

    scp_style = _SCP_STYLE_REMOTE.match(value)
    if scp_style is not None:
        # Shape 2: an scp-style SSH remote. Keeping host and path keeps the
        # remote identifiable; the userinfo goes regardless of what it holds.
        return f"***@{scp_style.group('rest')}"

    if normalized.count("@") == 1 and parsed.netloc.count("@") == 1:
        # Shape 3: the one separator in the token is the one splitting userinfo
        # from host in the authority ``urlparse`` found, so the split is
        # trustworthy and the host can be kept. Counting on the decoded form but
        # locating on the raw authority also rejects a URL whose authority only
        # looks credential-free because its separator is still encoded.
        _userinfo, host = parsed.netloc.rsplit("@", 1)
        return urlunparse(
            parsed._replace(
                netloc=f"***@{host}",
                path=_strip_path_params(parsed.path),
                params="",
                query="",
                fragment="",
            ),
        )

    # Userinfo somewhere this cannot account for: an empty or ambiguous
    # authority, a nested URL, a percent-encoded separator, or a shape not yet
    # seen. None of them can be partially redacted safely.
    return "***"


def _redact_token(match: re.Match[str]) -> str:
    """Redact one whitespace-delimited token from free-form Git output.

    URL shapes are tested first so a real URL is never mistaken for scp syntax.
    scp tokens go through the same helper the API surface uses, so the two
    cannot give opposite answers about one string; that costs the SSH username
    in diagnostics, which beats leaking a token pasted into that position.
    """
    token = match.group(0)
    if len(token) > _MAX_REDACTABLE_TOKEN_LENGTH:
        # Bounded here, where the token is already in hand, so no amount of
        # remote-supplied text can make classification expensive.
        return "***"
    if _URL_LIKE_TOKEN.match(token) or _SCP_STYLE_REMOTE.match(token) or _USERINFO_LIKE_TOKEN.match(token):
        return redact_url_credentials(token)
    return token


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

    return _TOKEN_PATTERN.sub(_redact_token, redacted)


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
