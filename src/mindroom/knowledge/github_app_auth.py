"""Short-lived GitHub App credentials for Git knowledge sources."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt

_GITHUB_API_ROOT = "https://api.github.com"
_GITHUB_API_VERSION = "2022-11-28"
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
_REPOSITORY_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class _GitHubAppCredentials:
    app_id: int
    installation_id: int
    private_key_file: Path


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: datetime


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        parsed = 0
    if parsed <= 0:
        msg = f"GitHub App credential field '{field_name}' must be a positive integer"
        raise ValueError(msg)
    return parsed


def _parse_credentials(credentials: Mapping[str, Any]) -> _GitHubAppCredentials:
    if credentials.get("auth_type") != "github_app":
        msg = "GitHub App credentials require auth_type 'github_app'"
        raise ValueError(msg)

    app_id = _positive_int(credentials.get("app_id"), field_name="app_id")
    installation_id = _positive_int(credentials.get("installation_id"), field_name="installation_id")
    raw_private_key_file = credentials.get("private_key_file")
    if not isinstance(raw_private_key_file, str) or not raw_private_key_file.strip():
        msg = "GitHub App credential field 'private_key_file' must be a non-empty absolute path"
        raise ValueError(msg)
    private_key_file = Path(raw_private_key_file)
    if not private_key_file.is_absolute():
        msg = "GitHub App credential field 'private_key_file' must be an absolute path"
        raise ValueError(msg)
    return _GitHubAppCredentials(
        app_id=app_id,
        installation_id=installation_id,
        private_key_file=private_key_file,
    )


def _github_repository(repo_url: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(repo_url)
        port = parsed.port
    except ValueError as exc:
        msg = "GitHub App credentials require a canonical https://github.com/<owner>/<repository> remote"
        raise ValueError(msg) from exc

    parts = [part for part in parsed.path.split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
    ):
        msg = "GitHub App credentials require a canonical https://github.com/<owner>/<repository> remote"
        raise ValueError(msg)

    owner, repository = parts
    repository = repository.removesuffix(".git")
    if (
        not owner
        or not repository
        or not _REPOSITORY_COMPONENT.fullmatch(owner)
        or not _REPOSITORY_COMPONENT.fullmatch(repository)
    ):
        msg = "GitHub App credentials require a canonical https://github.com/<owner>/<repository> remote"
        raise ValueError(msg)
    return owner, repository


def _parse_expiry(value: object) -> datetime:
    if not isinstance(value, str):
        msg = "expires_at must be a string"
        raise TypeError(msg)
    try:
        expires_at = datetime.fromisoformat(value)
    except ValueError as exc:
        msg = "expires_at must be an RFC3339 timestamp"
        raise ValueError(msg) from exc
    if expires_at.tzinfo is None:
        msg = "expires_at must include a timezone"
        raise ValueError(msg)
    return expires_at.astimezone(UTC)


class GitHubAppTokenProvider:
    """Mint and cache repository-scoped GitHub App installation tokens."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._cache: dict[tuple[int, int, str, str, Path], _CachedToken] = {}
        self._refresh_lock = asyncio.Lock()

    async def resolve(self, repo_url: str, credentials: Mapping[str, Any]) -> tuple[str, str]:
        """Return HTTP Basic userinfo for one GitHub repository."""
        parsed_credentials = _parse_credentials(credentials)
        owner, repository = _github_repository(repo_url)
        cache_key = (
            parsed_credentials.app_id,
            parsed_credentials.installation_id,
            owner.casefold(),
            repository.casefold(),
            parsed_credentials.private_key_file,
        )
        cached = self._cache.get(cache_key)
        now = self._now().astimezone(UTC)
        if cached is not None and now < cached.expires_at - _TOKEN_REFRESH_MARGIN:
            return "x-access-token", cached.token

        async with self._refresh_lock:
            cached = self._cache.get(cache_key)
            now = self._now().astimezone(UTC)
            if cached is not None and now < cached.expires_at - _TOKEN_REFRESH_MARGIN:
                return "x-access-token", cached.token
            minted = await self._mint_token(
                parsed_credentials,
                repository=repository,
                now=now,
            )
            self._cache[cache_key] = minted
            return "x-access-token", minted.token

    async def _mint_token(
        self,
        credentials: _GitHubAppCredentials,
        *,
        repository: str,
        now: datetime,
    ) -> _CachedToken:
        try:
            private_key = credentials.private_key_file.read_text(encoding="utf-8")
        except OSError as exc:
            msg = "GitHub App private_key_file could not be read"
            raise ValueError(msg) from exc
        try:
            app_jwt = jwt.encode(
                {
                    "iat": int(now.timestamp()) - 60,
                    "exp": int(now.timestamp()) + 540,
                    "iss": str(credentials.app_id),
                },
                private_key,
                algorithm="RS256",
            )
        except (TypeError, ValueError) as exc:
            msg = "GitHub App private_key_file does not contain a usable RSA private key"
            raise ValueError(msg) from exc

        url = f"{_GITHUB_API_ROOT}/app/installations/{credentials.installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }
        payload = {
            "repositories": [repository],
            "permissions": {"contents": "read"},
        }
        if self._client is not None:
            response = await self._client.post(url, headers=headers, json=payload)
        else:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(url, headers=headers, json=payload)
        if response.status_code != httpx.codes.CREATED:
            msg = (
                f"GitHub App token request for installation {credentials.installation_id} "
                f"failed with HTTP {response.status_code}"
            )
            raise RuntimeError(msg)
        try:
            response_payload = response.json()
        except ValueError as exc:
            msg = "GitHub returned an invalid token response for the App installation"
            raise RuntimeError(msg) from exc
        token = response_payload.get("token") if isinstance(response_payload, Mapping) else None
        expires_at_value = response_payload.get("expires_at") if isinstance(response_payload, Mapping) else None
        if not isinstance(token, str) or not token:
            msg = "GitHub returned an invalid token response for the App installation"
            raise RuntimeError(msg)
        try:
            expires_at = _parse_expiry(expires_at_value)
        except (TypeError, ValueError) as exc:
            msg = "GitHub returned an invalid token response for the App installation"
            raise RuntimeError(msg) from exc
        return _CachedToken(token=token, expires_at=expires_at)
