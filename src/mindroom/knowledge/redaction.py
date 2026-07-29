"""Credential redaction helpers for knowledge Git URLs."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from base64 import b64decode
from urllib.parse import ParseResult, unquote, urlparse, urlunparse

from mindroom.git_urls import credential_free_repo_url

#: Whitespace is allowed around the colon: HTTP permits it, and a proxy or Git
#: helper that reformats a header it echoes back must not slip the credential
#: past this pattern.
_AUTHORIZATION_HEADER_PATTERN: re.Pattern[str] = re.compile(
    r"\bAuthorization\s*:\s*(Basic|Bearer)\s+([^\s'\"<>]+)",
    re.IGNORECASE,
)
#: Every credential-bearing substructure, located wherever it appears rather
#: than only at the start of a whitespace-delimited run.
#:
#: Searching is the point. Git frames URLs constantly -- ``Endpoint=https://…``
#: in LFS output, ``remote.origin.url=https://…`` from ``config --list``,
#: brackets, backticks -- and an anchored test sees the frame, not the URL, so
#: it declines to classify and the credential survives. Trailing frames are
#: harmless; a leading one is what breaks an anchor.
#:
#: Every branch is written so it cannot fail *after* consuming, because that is
#: what makes a scan quadratic: ``re.sub`` retries at the next position, so a
#: branch that consumes a long run and then demands a character it never finds
#: costs O(n) per position. The protocol-relative branch therefore consumes and
#: lets the classifier decide, and the leading look-behind stops any branch
#: being retried in the middle of an identifier it already rejected.
#:
#: The ``user:password@host`` branch is the one that must still demand a
#: character, so it is bounded twice. Excluding ``:`` from the password stops it
#: crossing the next separator on colon-dense input -- ``a:a:a:…`` cost 36 s at
#: 195 KB when it did -- and the length cap makes the per-position work constant
#: regardless. The cap is ``_MAX_REDACTABLE_TOKEN_LENGTH``, so a run long enough
#: to exceed it would be dropped unread anyway.
#:
#: The cost is precise, and smaller than "only the username survives": matching
#: resumes after each colon, so a *schemeless* multi-colon userinfo keeps its
#: leading segments. What goes is the last **two** colon-separated segments,
#: because the match is ``word:password@`` -- ``user:pa:ss:secret@host`` becomes
#: ``user:pa:***``, losing ``ss`` as well as ``secret``. A password of two
#: segments or fewer is therefore removed entirely, and the surviving fraction
#: of a longer one approaches but never reaches the whole. The baseline
#: preserved the entire string in every one of these cases.
#:
#: A URL *with* a scheme is unaffected: the ``scheme://`` branch consumes it
#: first and its authority is redacted whole, identically to the baseline. Two
#: schemeless shapes are redacted by neither: a password beginning with a colon,
#: and one containing ``/``.
_CREDENTIAL_CANDIDATE_PATTERN: re.Pattern[str] = re.compile(
    r"""(?<![A-Za-z0-9+._-])(?:
          [a-zA-Z][a-zA-Z0-9+.-]*://[^\s'"<>]*        # scheme://host/path
        | //[^\s'"<>]*                                # protocol-relative
        | (?:https?|ssh|git|git\+[a-z]+|ftps?|file):[^\s'"<>]*   # transport, no //
        | [A-Za-z0-9._-]+:[^/@:\s'"<>]{1,2048}+@[^\s'"<>]*  # user:password@host
        | [A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[^@\s'"<>]+ # scp-style user@host:path
    )""",
    re.VERBOSE | re.IGNORECASE,
)
#: ``git@github.com:org/repo.git``. SSH has no URL password field, so the
#: userinfo here is a username; it is dropped anyway, and only the host and path
#: are kept so the remote stays identifiable in an error message. Underscores are
#: not valid in hostnames but ssh and Git both accept them, and internal hosts
#: use them.
#:
#: The path after the colon must be non-empty, which is what keeps
#: ``git@github.com: Permission denied (publickey).`` -- a sentence, not a
#: remote -- out of this.
_SCP_STYLE_REMOTE: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9._-]+@(?P<rest>(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\]):[^@\s]+)$",
)
#: A ``user:password@`` pair sitting in a path rather than an authority. An
#: ``@`` in a path is ordinarily just a character (``/@scope/pkg``,
#: ``repo.git@v1``), so finding one is not grounds to redact -- but a nested URL
#: or a userinfo pair there is a credential the authority check cannot see.
_USERINFO_IN_PATH: re.Pattern[str] = re.compile(r"[^/\s:@]+:[^/\s@]+@")
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
    """Return `value` percent-decoded to a fixed point and NFKC-normalised.

    A separator can be hidden under any number of encoding layers -- ``%40``,
    ``%2540``, ``%252540`` -- so testing one layer only buys one layer, and
    picking a depth limit just invites depth+1. Decoding to a fixed point has no
    limit to beat: every pass that changes anything replaces a three-character
    escape with one character, so the string strictly shortens and the loop
    terminates with no escapes left to hide behind.

    It can also be hidden as a *different codepoint*: U+FF20 and U+FE6B both
    NFKC-normalise to ``@``, which is why ``urlsplit`` rejects them outright.
    Normalising here means the separator counts below see one ``@`` either way,
    so such a URL is judged as the credential-bearing thing it is rather than as
    an unrecognised token.
    """
    while True:
        decoded = unquote(value)
        if decoded == value:
            return unicodedata.normalize("NFKC", value)
        value = decoded


def _inspectable_url(value: str) -> ParseResult | None:
    """Return the parsed token, or None when it must be dropped unread."""
    if len(value) > _MAX_REDACTABLE_TOKEN_LENGTH:
        return None
    try:
        return urlparse(value)
    except ValueError:
        return None


def _url_with_authority_kept(parsed: ParseResult, value: str) -> str:
    """Return the URL unchanged apart from dropping params, query and fragment."""
    if not parsed.scheme or not parsed.netloc:
        return value
    return urlunparse(
        parsed._replace(path=_strip_path_params(parsed.path), params="", query="", fragment=""),
    )


def _redact_recognised_shape(parsed: ParseResult, value: str, normalized: str) -> str:
    """Redact a URL known to contain a separator, or drop it when unrecognised."""
    scp_style = _SCP_STYLE_REMOTE.match(value)
    if scp_style is not None:
        # Shape 2: an scp-style SSH remote. Keeping host and path keeps the
        # remote identifiable; the userinfo goes regardless of what it holds.
        return f"***@{scp_style.group('rest')}"

    if parsed.netloc and "@" not in parsed.netloc:
        # Shape 3: the authority carries no userinfo, so every ``@`` here is an
        # ordinary path character -- ``/@scope/pkg``, ``repo.git@v1``, ``/a@b``.
        # Redacting those was destroying diagnostics for no gain.
        #
        # Except that a path can also hide a whole second URL, whose credential
        # this authority check cannot see. That is the one case left where the
        # classifier must fail closed rather than reason positively.
        remainder = value[value.index(parsed.netloc) + len(parsed.netloc) :]
        if "://" in remainder or _USERINFO_IN_PATH.search(remainder):
            return "***"
        return _url_with_authority_kept(parsed, value)

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
        return _url_with_authority_kept(parsed, value)

    if normalized.count("@") != value.count("@"):
        # An encoded separator: the authority only looks credential-free because
        # its ``@`` is still written ``%40``, at whatever nesting depth. Checked
        # before anything reasons about where the ``@`` sits, because until this
        # passes the parse does not describe the string a client will use.
        return "***"

    return _redact_recognised_shape(parsed, value, normalized)


def _redact_candidate(match: re.Match[str]) -> str:
    """Redact one located credential-bearing substructure.

    The match is the URL itself, not the run of text framing it, so a leading
    ``Endpoint=`` or ``[`` no longer hides it. scp remotes go through the same
    helper the API surface uses, so the two cannot give opposite answers about
    one string.
    """
    candidate = match.group(0)
    if len(candidate) > _MAX_REDACTABLE_TOKEN_LENGTH:
        # Bounded before classification, so no amount of remote-supplied text
        # can make the work per match unbounded.
        return "***"
    return redact_url_credentials(candidate)


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

    return _CREDENTIAL_CANDIDATE_PATTERN.sub(_redact_candidate, redacted)


def credential_free_url_identity(value: str) -> str:
    """Return a stable repo URL identity that never persists secret-bearing userinfo.

    Reached from ``indexing_settings_key`` on the ordinary resolve path, not an
    error path, so it must not raise on account of a URL's *contents*:
    ``urlsplit`` rejects a netloc holding a codepoint that NFKC-normalises to a
    delimiter, and puts that netloc -- password included -- into the exception
    message. Hashing the raw string instead keeps the identity stable and puts
    nothing recoverable in the output.

    A lone surrogate still raises ``UnicodeEncodeError`` from the hash, which is
    left alone deliberately: the message names no URL, and a config string that
    cannot be UTF-8 encoded is broken in a way worth surfacing rather than
    hashing into a stable-looking identity.
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        return f"repo-url-sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
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
    """Return embedded HTTP(S) URL userinfo, if present.

    Never raises: a URL ``urlsplit`` refuses has no userinfo this can use, and
    its exception message would carry the very credential being looked for.
    """
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or "@" not in parsed.netloc:
        return None
    if not parsed.username:
        return None
    return unquote(parsed.username), unquote(parsed.password or "")
