"""Tests for `github_app` services: which repository a request names and which App token it gets."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from structlog.testing import capture_logs

from mindroom.config.egress_broker import EgressGitHubApp
from mindroom.credentials.manager import CredentialsManager
from mindroom.egress_broker.github_app import GITHUB_APP_PERMISSIONS, github_app_secret, request_repository
from mindroom.egress_broker.secrets import (
    Secret,
    SecretGitHubAppUnavailable,
    SecretMissing,
    SecretRepositoryNotAllowed,
    SecretRequest,
)
from mindroom.github_app_auth import GitHubAppTokenProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from mindroom.egress_broker.secrets import SecretResult

_NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
_MIXED = EgressGitHubApp(
    credentials_service="acme_app",
    repositories={"acme/api": "read_write", "acme/web": "read_write", "acme/docs": "read"},
)


@pytest.mark.parametrize(
    ("host", "path", "repository"),
    [
        ("api.github.com", "/repos/acme/api", "acme/api"),
        ("api.github.com", "/repos/acme/api/pulls/1", "acme/api"),
        ("uploads.github.com", "/repos/acme/api/releases/1/assets", "acme/api"),
        ("github.com", "/acme/api.git/info/refs", "acme/api"),
        ("github.com", "/acme/api/git-upload-pack", "acme/api"),
        ("github.com", "/acme/api.git", "acme/api"),
        ("api.github.com", "/graphql", None),
        ("api.github.com", "/user", None),
        ("api.github.com", "/user/repos", None),
        ("api.github.com", "/repositories/42", None),
        ("api.github.com", "/repos/acme", None),
        ("api.github.com", "/repos/acme/", None),
        ("github.com", "/acme", None),
        ("github.com", "/", None),
        ("example.com", "/repos/acme/api", None),
    ],
)
def test_request_repository_reads_github_repository_paths(host: str, path: str, repository: str | None) -> None:
    """API paths name `/repos/<owner>/<repository>`, git paths `/<owner>/<repository>[.git]`; others name none."""
    assert request_repository(host, path) == repository


def test_read_write_permissions_can_push_and_write_pull_requests() -> None:
    """The documented permission mapping: reads for `read`, writes for `read_write`, metadata read for both."""
    assert GITHUB_APP_PERMISSIONS["read"] == {
        "contents": "read",
        "metadata": "read",
        "issues": "read",
        "pull_requests": "read",
    }
    assert GITHUB_APP_PERMISSIONS["read_write"] == {
        "contents": "write",
        "metadata": "read",
        "issues": "write",
        "pull_requests": "write",
    }


class _GitHub:
    """A fake token endpoint that records each mint and answers with a numbered token, or a fixed status."""

    def __init__(self) -> None:
        self.mints: list[dict[str, object]] = []
        self.status = 201

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.github.com/app/installations/67890/access_tokens"
        self.mints.append(json.loads(request.read()))
        if self.status != 201:
            return httpx.Response(self.status, json={"message": "nope"})
        return httpx.Response(
            201,
            json={
                "token": f"app-token-{len(self.mints)}",
                "expires_at": (_NOW + timedelta(hours=1)).isoformat(),
            },
        )


@pytest.fixture
def manager(tmp_path: Path) -> CredentialsManager:
    """Return a primary credentials manager holding the App credential `acme_app`, with a real RSA key file."""
    key_file = tmp_path / "app.pem"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    manager = CredentialsManager(tmp_path / "credentials")
    manager.save_credentials(
        "acme_app",
        {"auth_type": "github_app", "app_id": 12345, "installation_id": 67890, "private_key_file": str(key_file)},
    )
    return manager


@pytest_asyncio.fixture
async def github() -> AsyncIterator[tuple[_GitHub, GitHubAppTokenProvider]]:
    """Return the fake token endpoint and a token provider that mints through it."""
    fake = _GitHub()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as client:
        yield fake, GitHubAppTokenProvider(client=client, now=lambda: _NOW)


def _resolver(
    manager: CredentialsManager,
    tokens: GitHubAppTokenProvider,
    app: EgressGitHubApp = _MIXED,
) -> Callable[..., Awaitable[SecretResult]]:
    async def resolve(host: str, path: str, method: str = "GET") -> SecretResult:
        return await github_app_secret(
            app,
            SecretRequest(method=method, host=host, path=path),
            service="acme",
            credentials_manager=manager,
            tokens=tokens,
        )

    return resolve


@pytest.mark.asyncio
async def test_each_repository_gets_the_token_of_its_access_level(
    manager: CredentialsManager,
    github: tuple[_GitHub, GitHubAppTokenProvider],
) -> None:
    """A read_write repository gets the write token, a read one the read token, each minted once and then cached."""
    fake, tokens = github
    resolve = _resolver(manager, tokens)

    pushed = await resolve("github.com", "/acme/api.git/git-receive-pack", "POST")
    commented = await resolve("api.github.com", "/repos/Acme/Web/issues/1/comments", "POST")
    read = await resolve("api.github.com", "/repos/acme/docs/contents/README.md")
    cloned = await resolve("github.com", "/acme/docs.git/info/refs")

    assert pushed == Secret("app-token-1")
    assert commented == Secret("app-token-1")
    assert read == Secret("app-token-2")
    assert cloned == Secret("app-token-2")
    assert fake.mints == [
        {"repositories": ["api", "web"], "permissions": dict(GITHUB_APP_PERMISSIONS["read_write"])},
        {"repositories": ["api", "web", "docs"], "permissions": dict(GITHUB_APP_PERMISSIONS["read"])},
    ]


@pytest.mark.asyncio
async def test_a_request_naming_no_repository_gets_the_read_token_unless_every_repository_is_writable(
    manager: CredentialsManager,
    github: tuple[_GitHub, GitHubAppTokenProvider],
) -> None:
    """GraphQL and other repository-less paths get read access to every listed repository, or write when all allow it."""
    fake, tokens = github
    writable = EgressGitHubApp(credentials_service="acme_app", repositories={"acme/api": "read_write"})

    assert await _resolver(manager, tokens)("api.github.com", "/graphql", "POST") == Secret("app-token-1")
    assert await _resolver(manager, tokens, writable)("api.github.com", "/graphql", "POST") == Secret("app-token-2")
    assert [mint["permissions"] for mint in fake.mints] == [
        dict(GITHUB_APP_PERMISSIONS["read"]),
        dict(GITHUB_APP_PERMISSIONS["read_write"]),
    ]
    assert fake.mints[0]["repositories"] == ["api", "web", "docs"]
    assert fake.mints[1]["repositories"] == ["api"]


@pytest.mark.parametrize(
    ("host", "path"),
    [("api.github.com", "/repos/acme/other/pulls"), ("github.com", "/acme/other.git/info/refs")],
)
@pytest.mark.asyncio
async def test_a_request_for_an_unlisted_repository_gets_no_token(
    manager: CredentialsManager,
    github: tuple[_GitHub, GitHubAppTokenProvider],
    host: str,
    path: str,
) -> None:
    """A repository the App does not list is refused by name before any credential is read or token minted."""
    fake, tokens = github

    assert await _resolver(manager, tokens)(host, path) == SecretRepositoryNotAllowed(repository="acme/other")
    assert fake.mints == []


@pytest.mark.parametrize(
    "stored",
    [None, {"username": "x-access-token", "token": "a-static-token"}],
    ids=["missing", "not-an-app"],
)
@pytest.mark.asyncio
async def test_an_unstored_app_credential_is_missing(
    tmp_path: Path,
    github: tuple[_GitHub, GitHubAppTokenProvider],
    stored: dict[str, str] | None,
) -> None:
    """Without a stored `auth_type: github_app` credential nothing is minted and the refusal names the App."""
    fake, tokens = github
    manager = CredentialsManager(tmp_path / "credentials")
    if stored is not None:
        manager.save_credentials("acme_app", stored)

    assert await _resolver(manager, tokens)("api.github.com", "/repos/acme/api") == SecretMissing(github_app=True)
    assert fake.mints == []


@pytest.mark.asyncio
async def test_a_failed_mint_is_unavailable_and_logged_without_the_response(
    manager: CredentialsManager,
    github: tuple[_GitHub, GitHubAppTokenProvider],
) -> None:
    """GitHub refusing the token request is a retryable failure whose log names the status, never GitHub's body."""
    fake, tokens = github
    fake.status = 422

    with capture_logs() as logs:
        result = await _resolver(manager, tokens)("api.github.com", "/repos/acme/api")

    assert result == SecretGitHubAppUnavailable()
    [log] = [entry for entry in logs if entry["event"] == "egress_broker_github_app_token_failed"]
    assert log["service"] == "acme"
    assert "HTTP 422" in log["error"]
    assert "nope" not in repr(logs)
