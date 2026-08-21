"""GitHub App installation authentication for Git knowledge sources."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from mindroom.knowledge.github_app_auth import GitHubAppTokenProvider

if TYPE_CHECKING:
    from pathlib import Path


def _private_key(path: Path) -> rsa.RSAPrivateKey:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    return key


def _credentials(default_private_key_file: Path, **overrides: object) -> dict[str, object]:
    credentials: dict[str, object] = {
        "auth_type": "github_app",
        "app_id": 12345,
        "installation_id": 67890,
        "private_key_file": str(default_private_key_file),
    }
    credentials.update(overrides)
    return credentials


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"app_id": 0}, "app_id"),
        ({"app_id": "not-an-id"}, "app_id"),
        ({"installation_id": -1}, "installation_id"),
        ({"private_key_file": "relative.pem"}, "private_key_file"),
        ({"private_key_file": ""}, "private_key_file"),
    ],
)
@pytest.mark.asyncio
async def test_github_app_credentials_fail_closed_for_invalid_fields(
    tmp_path: Path,
    overrides: dict[str, object],
    match: str,
) -> None:
    """Invalid App credential fields must fail before any network request."""
    key_path = tmp_path / "private-key.pem"
    _private_key(key_path)

    with pytest.raises(ValueError, match=match):
        await GitHubAppTokenProvider().resolve(
            "https://github.com/example/private.git",
            _credentials(key_path, **overrides),
        )


@pytest.mark.parametrize(
    "repo_url",
    [
        "http://github.com/example/private.git",
        "https://gitlab.com/example/private.git",
        "https://user@github.com/example/private.git",
        "https://github.com/example/private.git?token=secret",
        "https://github.com/example/private.git#secret",
        "https://github.com/example",
        "https://github.com/example/private/extra",
    ],
)
@pytest.mark.asyncio
async def test_github_app_credentials_reject_noncanonical_github_remotes(
    tmp_path: Path,
    repo_url: str,
) -> None:
    """App auth must reject remotes whose repository identity is ambiguous."""
    key_path = tmp_path / "private-key.pem"
    _private_key(key_path)

    with pytest.raises(ValueError, match="GitHub App credentials require") as exc_info:
        await GitHubAppTokenProvider().resolve(repo_url, _credentials(key_path))

    assert "secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_github_app_token_is_repository_scoped_read_only_cached_and_rotates_key(tmp_path: Path) -> None:
    """Mint least-privilege tokens, cache safely, and reread rotated keys."""
    key_path = tmp_path / "private-key.pem"
    first_key = _private_key(key_path)
    current_time = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    requests: list[httpx.Request] = []

    def _now() -> datetime:
        return current_time

    async def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "token": f"installation-token-{len(requests)}",
                "expires_at": (current_time + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                "permissions": {"contents": "read"},
                "repository_selection": "selected",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        provider = GitHubAppTokenProvider(client=client, now=_now)
        first = await provider.resolve("https://github.com/Example/Private.git", _credentials(key_path))
        cached = await provider.resolve("https://github.com/Example/Private.git", _credentials(key_path))

        assert first == ("x-access-token", "installation-token-1")
        assert cached == first
        assert len(requests) == 1

        request = requests[0]
        assert request.url == "https://api.github.com/app/installations/67890/access_tokens"
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["x-github-api-version"] == "2022-11-28"
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.read() == b'{"repositories":["Private"],"permissions":{"contents":"read"}}'

        encoded_jwt = request.headers["authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            encoded_jwt,
            first_key.public_key(),
            algorithms=["RS256"],
            audience=None,
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims == {
            "iat": int(current_time.timestamp()) - 60,
            "exp": int(current_time.timestamp()) + 540,
            "iss": "12345",
        }

        current_time += timedelta(minutes=56)
        second_key = _private_key(key_path)
        refreshed = await provider.resolve("https://github.com/Example/Private.git", _credentials(key_path))

    assert refreshed == ("x-access-token", "installation-token-2")
    assert len(requests) == 2
    refreshed_jwt = requests[1].headers["authorization"].removeprefix("Bearer ")
    refreshed_claims = jwt.decode(
        refreshed_jwt,
        second_key.public_key(),
        algorithms=["RS256"],
        audience=None,
        options={"verify_exp": False, "verify_iat": False},
    )
    assert refreshed_claims["iat"] == int(current_time.timestamp()) - 60


@pytest.mark.asyncio
async def test_concurrent_github_app_token_requests_share_one_refresh(tmp_path: Path) -> None:
    """Concurrent Git operations must coalesce one installation-token refresh."""
    key_path = tmp_path / "private-key.pem"
    _private_key(key_path)
    current_time = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
    request_count = 0

    async def _handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        await asyncio.sleep(0.01)
        return httpx.Response(
            201,
            json={
                "token": "shared-token",
                "expires_at": (current_time + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        provider = GitHubAppTokenProvider(client=client, now=lambda: current_time)
        results = await asyncio.gather(
            *(
                provider.resolve("https://github.com/example/private.git", _credentials(key_path))
                for _index in range(10)
            ),
        )

    assert results == [("x-access-token", "shared-token")] * 10
    assert request_count == 1


@pytest.mark.asyncio
async def test_github_app_token_endpoint_error_does_not_include_response_body(tmp_path: Path) -> None:
    """GitHub error bodies must not be copied into operator-facing errors."""
    key_path = tmp_path / "private-key.pem"
    _private_key(key_path)

    async def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "secret response detail"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        provider = GitHubAppTokenProvider(
            client=client,
            now=lambda: datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        )
        with pytest.raises(RuntimeError, match=r"installation 67890.*HTTP 403") as exc_info:
            await provider.resolve("https://github.com/example/private.git", _credentials(key_path))

    assert "secret response detail" not in str(exc_info.value)


@pytest.mark.parametrize(
    "response_json",
    [
        {"expires_at": "2026-08-21T13:00:00Z"},
        {"token": "secret-token"},
        {"token": "secret-token", "expires_at": "not-a-date"},
    ],
)
@pytest.mark.asyncio
async def test_github_app_token_endpoint_rejects_invalid_success_payload_without_leaking_token(
    tmp_path: Path,
    response_json: dict[str, Any],
) -> None:
    """Malformed success payloads must fail without exposing returned tokens."""
    key_path = tmp_path / "private-key.pem"
    _private_key(key_path)

    async def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=response_json)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        provider = GitHubAppTokenProvider(
            client=client,
            now=lambda: datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        )
        with pytest.raises(RuntimeError, match="invalid token response") as exc_info:
            await provider.resolve("https://github.com/example/private.git", _credentials(key_path))

    assert "secret-token" not in str(exc_info.value)
