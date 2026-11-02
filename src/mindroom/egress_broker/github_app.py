"""Tokens for `github_app` services: GitHub App installation tokens limited to the service's repositories."""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from typing import TYPE_CHECKING

import httpx

from mindroom.egress_broker.secrets import (
    Secret,
    SecretGitHubAppUnavailable,
    SecretMissing,
    SecretRepositoryNotAllowed,
    load_github_app_credentials,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.egress_broker import EgressGitHubApp, GitHubRepositoryAccess
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.egress_broker.secrets import SecretRequest, SecretResult
    from mindroom.github_app_auth import GitHubAppTokenProvider

__all__ = ["GITHUB_APP_PERMISSIONS", "github_app_secret", "request_repository"]

logger = get_logger(__name__)

# The App permissions a token gets for each access level: enough for git clone, fetch, and push, and for reading or
# writing issues and pull requests through the API and `gh`. GitHub adds metadata read to every token anyway.
GITHUB_APP_PERMISSIONS: Mapping[GitHubRepositoryAccess, Mapping[str, str]] = MappingProxyType(
    {
        "read": MappingProxyType(
            {"contents": "read", "metadata": "read", "issues": "read", "pull_requests": "read"},
        ),
        "read_write": MappingProxyType(
            {"contents": "write", "metadata": "read", "issues": "write", "pull_requests": "write"},
        ),
    },
)
# Hosts whose repository paths start with `/repos/<owner>/<repository>`.
_API_HOSTS = frozenset({"api.github.com", "uploads.github.com"})
# The host that serves git over HTTPS at `/<owner>/<repository>[.git]`.
_GIT_HOST = "github.com"


def request_repository(host: str, path: str) -> str | None:
    """Return the `owner/repository` a GitHub request's path names, or None for one that names none.

    Such paths are `/repos/<owner>/<repository>/...` on the API and upload hosts and `/<owner>/<repository>[.git]/...`
    on `github.com`. A path such as `/graphql`, `/user`, or `/repositories/<id>` names none.
    """
    segments = path.split("/")
    if host in _API_HOSTS and len(segments) > 3 and segments[1] == "repos":
        owner, repository = segments[2], segments[3]
    elif host == _GIT_HOST and len(segments) > 2:
        owner, repository = segments[1], segments[2].removesuffix(".git")
    else:
        return None
    return f"{owner}/{repository}" if owner and repository else None


def _token_scope(app: EgressGitHubApp, repository: str | None) -> GitHubRepositoryAccess | SecretRepositoryNotAllowed:
    """Return the access level of the token a request gets: its repository's, or for none the lowest listed.

    A request that names no repository, such as GraphQL, gets the read token, which covers every listed repository,
    unless every listed repository is `read_write`.
    """
    if repository is None:
        return "read" if "read" in app.repositories.values() else "read_write"
    access = app.access(repository)
    return SecretRepositoryNotAllowed(repository=repository) if access is None else access


async def github_app_secret(
    app: EgressGitHubApp,
    request: SecretRequest,
    *,
    service: str,
    credentials_manager: CredentialsManager,
    tokens: GitHubAppTokenProvider,
) -> SecretResult:
    """Return an installation token of `app` for one request to service `service`, minted or from `tokens`' cache.

    A request for a repository the App does not list gets `SecretRepositoryNotAllowed` and no token. Otherwise the
    token has the access level `_token_scope` chooses: the `read_write` token covers the `read_write` repositories,
    and the `read` token every listed repository. GitHub refuses a token every repository it does not cover, which
    stays the boundary for requests whose repository the path does not name.
    """
    access = _token_scope(app, request_repository(request.host, request.path))
    if isinstance(access, SecretRepositoryNotAllowed):
        return access
    credentials = await asyncio.to_thread(load_github_app_credentials, credentials_manager, app)
    if credentials is None:
        return SecretMissing(github_app=True)
    owner = next(iter(app.repositories)).split("/")[0]
    repositories = [
        name.split("/")[1] for name, listed in app.repositories.items() if access == "read" or listed == "read_write"
    ]
    try:
        token = await tokens.resolve_installation_token(
            credentials,
            owner=owner,
            repositories=repositories,
            permissions=GITHUB_APP_PERMISSIONS[access],
        )
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        # The provider's messages name the installation and HTTP status, never a key, token, or response body.
        logger.warning(
            "egress_broker_github_app_token_failed",
            service=service,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return SecretGitHubAppUnavailable()
    return Secret(token.token)
