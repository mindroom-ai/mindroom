"""Git URL normalization helpers."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def _strip_path_params(path: str) -> str:
    return path.split(";", 1)[0]


def credential_free_repo_url(repo_url: str) -> str:
    """Return a repository URL suitable for persistent Git config and comparison.

    Never raises. ``urlsplit`` rejects a netloc holding a codepoint that
    NFKC-normalises to a URL delimiter -- U+FF20 for ``@``, say -- and quotes
    that netloc, password included, in the exception message. Callers reach this
    from ordinary paths as well as error paths, so a raise here turns a
    misconfigured URL into a credential in whatever log or field catches it.

    An unparseable URL is returned unchanged, exactly as one with no authority
    already is: nothing can be stripped from a string this cannot read. It is
    the caller deciding whether to *persist* a URL that must reject it, and
    ``_persistable_remote_url`` does, because the authority still will not parse.
    """
    try:
        parsed = urlparse(repo_url)
    except ValueError:
        return repo_url
    if not parsed.scheme or not parsed.netloc:
        return repo_url
    path = _strip_path_params(parsed.path)
    if parsed.scheme == "ssh" and "@" in parsed.netloc and parsed.password is None:
        userinfo, host = parsed.netloc.rsplit("@", 1)
        if userinfo and ":" not in userinfo:
            return urlunparse(
                parsed._replace(
                    netloc=f"{userinfo}@{host}",
                    path=path,
                    params="",
                    query="",
                    fragment="",
                ),
            )
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunparse(
        parsed._replace(
            netloc=netloc,
            path=path,
            params="",
            query="",
            fragment="",
        ),
    )
