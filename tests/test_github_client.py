"""Tests for shared owner-scoped GitHub credentials and fixed-host HTTP helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.credentials.manager import CredentialsUnreadableError, get_runtime_credentials_manager
from mindroom.github_client import (
    GITHUB_API_VERSION,
    GITHUB_DEFAULT_ACCEPT,
    GITHUB_USER_TOKEN_SERVICE,
    GitHubClientError,
    build_user_github_oauth_context,
    github_get_json,
    github_user_token_store,
    parse_retry_after_seconds,
    resolve_owner_github_token,
)
from tests.oauth_test_utils import publish_oauth_credentials

if TYPE_CHECKING:
    from pathlib import Path

_ALICE = "@alice:example.org"
_BOB = "@bob:example.org"
_OAUTH_CRED = "ghu_alice_oauth"
_PAT_CRED = "github_pat_alice_123"
_REQ_CRED = "test-bearer-cred"


def _runtime_paths(tmp_path: Path, *, env: dict[str, str] | None = None) -> RuntimePaths:
    paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env=env or {},
    )
    manager = get_runtime_credentials_manager(paths)
    manager.save_credentials("github_oauth_client", {"client_id": "gh-client", "client_secret": "gh-secret"})
    manager.save_credentials("github", {"access_token": "shared-tool-token-never-use"})
    return paths


def _connect_oauth(paths: RuntimePaths, user_id: str, *, access_token: str) -> None:
    context = build_user_github_oauth_context(user_id, Config(), paths)
    assert context is not None
    publish_oauth_credentials(
        context.provider,
        {
            "token": access_token,
            "refresh_token": "gh-refresh",
            "client_id": "gh-client",
            "token_uri": "https://github.com/login/oauth/access_token",
            "scopes": [],
            "expires_at": 4_102_444_800.0,
            "_source": "oauth",
            "_oauth_provider": "github",
        },
        credentials_manager=context.credentials_manager,
        worker_target=context.worker_target,
    )


def _save_pat(paths: RuntimePaths, user_id: str, *, pat: str, login: str = "alice-gh") -> None:
    context = build_user_github_oauth_context(user_id, Config(), paths)
    assert context is not None
    github_user_token_store(context).save_credentials(
        GITHUB_USER_TOKEN_SERVICE,
        {"token": pat, "login": login},
        replace_unreadable=True,
    )


@pytest.mark.asyncio
async def test_resolve_owner_github_token_priority_isolation_and_unreadable_pat(tmp_path: Path) -> None:
    """Owner resolution checks PAT -> OAuth -> anonymous, isolates users, and refuses fallback on unreadable PAT."""
    paths = _runtime_paths(tmp_path, env={"GITHUB_TOKEN": "env-token-must-never-be-used"})
    config = Config()

    # Unconnected user is anonymous even when env GITHUB_TOKEN and shared `github` tool token exist.
    assert await resolve_owner_github_token(_ALICE, config, paths) == (None, "anonymous")
    assert await resolve_owner_github_token("", config, paths) == (None, "anonymous")

    # Connecting GitHub App OAuth resolves to ("app") for Alice only.
    _connect_oauth(paths, _ALICE, access_token=_OAUTH_CRED)
    assert await resolve_owner_github_token(_ALICE, config, paths) == (_OAUTH_CRED, "app")
    assert await resolve_owner_github_token(_BOB, config, paths) == (None, "anonymous")

    # Saving a personal access token takes priority over Alice's GitHub App OAuth.
    _save_pat(paths, _ALICE, pat=_PAT_CRED)
    assert await resolve_owner_github_token(_ALICE, config, paths) == (_PAT_CRED, "token")
    assert await resolve_owner_github_token(_BOB, config, paths) == (None, "anonymous")

    # Corrupting Alice's saved PAT raises CredentialsUnreadableError and never falls back to OAuth or anonymous.
    context = build_user_github_oauth_context(_ALICE, config, paths)
    assert context is not None
    github_user_token_store(context).save_credentials(
        GITHUB_USER_TOKEN_SERVICE,
        {"token": "bad token with spaces", "login": "alice-gh"},
        replace_unreadable=True,
    )
    with pytest.raises(CredentialsUnreadableError):
        await resolve_owner_github_token(_ALICE, config, paths)


def test_parse_retry_after_seconds_variants() -> None:
    """Retry-After (seconds and HTTP-date) and X-RateLimit-Reset parse positive waits only."""
    now = 1_795_600_000.0
    future_http_date = format_datetime(datetime.fromtimestamp(now + 75, tz=UTC), usegmt=True)
    past_http_date = format_datetime(datetime.fromtimestamp(now - 30, tz=UTC), usegmt=True)

    assert parse_retry_after_seconds({"Retry-After": "42"}, now=now) == 42.0
    assert parse_retry_after_seconds({"retry-after": future_http_date}, now=now) == pytest.approx(75.0, abs=1.0)
    assert parse_retry_after_seconds({"Retry-After": past_http_date}, now=now) is None
    assert parse_retry_after_seconds({"Retry-After": "0"}, now=now) is None
    assert parse_retry_after_seconds({"Retry-After": "-5"}, now=now) is None
    assert parse_retry_after_seconds({"Retry-After": "NaN"}, now=now) is None
    assert parse_retry_after_seconds({"Retry-After": "not-a-date"}, now=now) is None

    assert parse_retry_after_seconds(
        {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(now + 90))},
        now=now,
    ) == pytest.approx(90.0, abs=1.0)
    assert (
        parse_retry_after_seconds(
            {"X-RateLimit-Remaining": "5", "X-RateLimit-Reset": str(int(now + 90))},
            now=now,
        )
        is None
    )
    assert (
        parse_retry_after_seconds(
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(now - 5))},
            now=now,
        )
        is None
    )
    # Retry-After takes precedence over X-RateLimit-Reset.
    assert (
        parse_retry_after_seconds(
            {
                "Retry-After": "15",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(now + 90)),
            },
            now=now,
        )
        == 15.0
    )


@pytest.mark.asyncio
async def test_github_get_json_200_304_and_link_next_page() -> None:
    """200 parses JSON + Link next_page; 304 preserves ETag and returns data=None."""
    seen_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if request.headers.get("if-none-match") == '"etag-1"':
            return httpx.Response(304, headers={"X-RateLimit-Remaining": "4999"})
        if request.url.path.endswith("/evil-link"):
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"Link": '<https://evil.example/api?page=2>; rel="next"'},
            )
        if request.url.path.endswith("/bad-page"):
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"Link": '<https://api.github.com/repos/o/r/issues?page=0>; rel="next"'},
            )
        return httpx.Response(
            200,
            json={"number": 42},
            headers={
                "ETag": '"etag-1"',
                "Link": (
                    '<https://api.github.com/repositories/1/issues/42/timeline?per_page=100&page=3>; rel="next", '
                    '<https://api.github.com/repositories/1/issues/42/timeline?per_page=100&page=5>; rel="last"'
                ),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        res_200 = await github_get_json("/repos/o/r/pulls/42", token=_REQ_CRED, client=client)
        assert res_200.status_code == 200
        assert res_200.etag == '"etag-1"'
        assert res_200.data == {"number": 42}
        assert res_200.rate_limited is False
        assert res_200.next_page == 3
        first_headers = seen_requests[0].headers
        assert first_headers["accept"] == GITHUB_DEFAULT_ACCEPT
        assert first_headers["x-github-api-version"] == GITHUB_API_VERSION
        assert first_headers["user-agent"] == "mindroom"
        assert first_headers["authorization"] == f"Bearer {_REQ_CRED}"
        assert "if-none-match" not in first_headers

        res_304 = await github_get_json("/repos/o/r/pulls/42", token=None, etag='"etag-1"', client=client)
        assert res_304.status_code == 304
        assert res_304.etag == '"etag-1"'
        assert res_304.data is None
        assert res_304.next_page is None
        assert "authorization" not in seen_requests[1].headers
        assert seen_requests[1].headers["if-none-match"] == '"etag-1"'

        res_evil = await github_get_json("/repos/o/r/evil-link", token=None, client=client)
        assert res_evil.next_page is None

        res_bad_page = await github_get_json("/repos/o/r/bad-page", token=None, client=client)
        assert res_bad_page.next_page is None


@pytest.mark.asyncio
async def test_github_get_json_redirects() -> None:
    """Redirects stay strictly on api.github.com and stop after 3 hops."""
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        path = request.url.path
        if path == "/repos/old/repo/pulls/1":
            return httpx.Response(301, headers={"Location": "https://api.github.com/repositories/99/pulls/1"})
        if path == "/repositories/99/pulls/1":
            assert request.headers["authorization"] == f"Bearer {_REQ_CRED}"
            return httpx.Response(200, json={"moved": True})
        if path == "/repos/o/r/cross-host":
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        step = int(path.rsplit("/", 1)[1])
        return httpx.Response(302, headers={"Location": f"https://api.github.com/repos/o/r/loop/{step + 1}"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        moved = await github_get_json("/repos/old/repo/pulls/1", token=_REQ_CRED, client=client)
        assert moved.status_code == 200
        assert moved.data == {"moved": True}

        seen_urls.clear()
        cross = await github_get_json("/repos/o/r/cross-host", token=_REQ_CRED, client=client)
        assert cross.status_code == 302
        assert seen_urls == ["https://api.github.com/repos/o/r/cross-host"]

        seen_urls.clear()
        looped = await github_get_json("/repos/o/r/loop/0", token=None, client=client)
        assert looped.status_code == 302
        assert len(seen_urls) == 4


@pytest.mark.asyncio
async def test_github_get_json_bounds_errors_and_rate_limits() -> None:
    """Body/JSON bounds raise GitHubClientError, rate limits parse cleanly, and bad paths raise ValueError."""
    responses = {
        "/repos/o/r/huge": httpx.Response(200, content=b"{" + b"a" * 200 + b"}"),
        "/repos/o/r/invalid-json": httpx.Response(200, content=b"not-json"),
        "/repos/o/r/rate-429": httpx.Response(
            429,
            content=json.dumps({"message": "secondary rate limit"}).encode("utf-8"),
            headers={"Retry-After": "60"},
        ),
        "/repos/o/r/rate-403": httpx.Response(
            403,
            content=json.dumps({"message": "API rate limit exceeded"}).encode("utf-8"),
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "4102444800"},
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.get(request.url.path, httpx.Response(404, json={"message": "Not Found"}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubClientError, match="exceeded"):
            await github_get_json("/repos/o/r/huge", token=None, max_bytes=64, client=client)

        with pytest.raises(GitHubClientError, match="invalid JSON"):
            await github_get_json("/repos/o/r/invalid-json", token=None, client=client)

        rl_429 = await github_get_json("/repos/o/r/rate-429", token=None, client=client)
        assert rl_429.status_code == 429
        assert rl_429.rate_limited is True
        assert rl_429.retry_after_seconds == 60.0
        assert rl_429.data == {"message": "secondary rate limit"}

        rl_403 = await github_get_json("/repos/o/r/rate-403", token=None, client=client)
        assert rl_403.status_code == 403
        assert rl_403.rate_limited is True
        assert rl_403.retry_after_seconds is not None
        assert rl_403.retry_after_seconds > 0
        assert rl_403.data == {"message": "API rate limit exceeded"}

        for bad_path in ("repos/o/r", "//evil.example/repos/o/r", "/repos/o/r\r\nX-Injected: 1"):
            with pytest.raises(ValueError, match="GitHub API path"):
                await github_get_json(bad_path, token=None, client=client)
