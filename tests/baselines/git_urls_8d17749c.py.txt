"""Git URL normalization helpers."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def _strip_path_params(path: str) -> str:
    return path.split(";", 1)[0]


def credential_free_repo_url(repo_url: str) -> str:
    """Return a repository URL suitable for persistent Git config and comparison."""
    parsed = urlparse(repo_url)
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
