"""Tests for the Slack app credentials and the bot identity cache."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.credentials.manager import CredentialsManager
from mindroom.slack import app as slack_app
from mindroom.slack.api import SlackApiError
from mindroom.slack.app import (
    SlackAppCredentials,
    SlackBotIdentity,
    cached_slack_bot_identity,
    load_slack_app,
    slack_bot_identity,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_BOT_TOKEN = "xoxb-test-token"  # noqa: S105
_SIGNING_SECRET = "signing-secret"  # noqa: S105
_APP_TOKEN = "xapp-test-token"  # noqa: S105
_AUTH_TEST_BODY = {
    "ok": True,
    "url": "https://example.slack.com/",
    "team": "Example",
    "user": "mindroom",
    "team_id": "T0123ABCD",
    "user_id": "U0123ABCD",
}
_IDENTITY = SlackBotIdentity(team_id="T0123ABCD", bot_user_id="U0123ABCD", workspace_url="https://example.slack.com/")


@pytest.fixture(autouse=True)
def _empty_identity_cache() -> Iterator[None]:
    slack_app._IDENTITIES.clear()
    yield
    slack_app._IDENTITIES.clear()


def _manager(tmp_path: Path) -> CredentialsManager:
    return CredentialsManager(tmp_path / "credentials")


def _credentials(bot_token: str = _BOT_TOKEN) -> SlackAppCredentials:
    return SlackAppCredentials(bot_token=bot_token, signing_secret=_SIGNING_SECRET, app_token=None)


def _client(handler: Any) -> httpx.AsyncClient:  # noqa: ANN401
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, timeout=10.0)


def test_load_slack_app_reads_the_saved_service(tmp_path: Path) -> None:
    """The three stored values come back as typed credentials, with a missing app token as None."""
    manager = _manager(tmp_path)
    manager.save_credentials("slack_app", {"bot_token": _BOT_TOKEN, "signing_secret": _SIGNING_SECRET})

    assert load_slack_app(manager) == _credentials()

    manager.save_credentials(
        "slack_app",
        {"bot_token": f" {_BOT_TOKEN}\n", "signing_secret": _SIGNING_SECRET, "app_token": _APP_TOKEN},
    )

    assert load_slack_app(manager) == SlackAppCredentials(
        bot_token=_BOT_TOKEN,
        signing_secret=_SIGNING_SECRET,
        app_token=_APP_TOKEN,
    )


def test_load_slack_app_reads_the_shared_layer(tmp_path: Path) -> None:
    """The app belongs to the workspace, so a manager with its own layer still reads the shared one."""
    manager = CredentialsManager(tmp_path / "local", shared_base_path=tmp_path / "shared")
    manager.shared_manager().save_credentials("slack_app", {"bot_token": _BOT_TOKEN, "signing_secret": _SIGNING_SECRET})
    manager.save_credentials("slack_app", {"bot_token": "xoxb-local", "signing_secret": "local"})

    credentials = load_slack_app(manager)

    assert credentials is not None
    assert credentials.bot_token == _BOT_TOKEN


@pytest.mark.parametrize(
    "stored",
    [
        None,
        {},
        {"bot_token": _BOT_TOKEN},
        {"signing_secret": _SIGNING_SECRET},
        {"bot_token": "", "signing_secret": _SIGNING_SECRET},
        {"bot_token": _BOT_TOKEN, "signing_secret": "  "},
        {"bot_token": 12345, "signing_secret": _SIGNING_SECRET},
    ],
)
def test_load_slack_app_missing_values_returns_none(tmp_path: Path, stored: dict[str, object] | None) -> None:
    """Without a bot token and a signing secret there is no Slack app."""
    manager = _manager(tmp_path)
    if stored is not None:
        manager.save_credentials("slack_app", stored)

    assert load_slack_app(manager) is None


@pytest.mark.asyncio
async def test_bot_identity_cached_per_token() -> None:
    """One `auth.test` serves every call with the same bot token; another token looks itself up."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_AUTH_TEST_BODY)

    async with _client(handler) as client:
        first = await slack_bot_identity(client, _credentials())
        second = await slack_bot_identity(client, _credentials())

        assert first == second == _IDENTITY
        assert len(calls) == 1
        assert calls[0].url == "https://slack.com/api/auth.test"
        assert calls[0].headers["Authorization"] == f"Bearer {_BOT_TOKEN}"
        assert cached_slack_bot_identity(_credentials()) == _IDENTITY

        assert cached_slack_bot_identity(_credentials("xoxb-other-token")) is None
        await slack_bot_identity(client, _credentials("xoxb-other-token"))
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_identity_cache_never_holds_the_token() -> None:
    """The cache key is a digest, so a memory dump of the cache does not reveal the bot token."""
    async with _client(lambda _request: httpx.Response(200, json=_AUTH_TEST_BODY)) as client:
        await slack_bot_identity(client, _credentials())

    assert len(slack_app._IDENTITIES) == 1
    assert all(_BOT_TOKEN not in key and len(key) == 64 for key in slack_app._IDENTITIES)


@pytest.mark.asyncio
async def test_identity_fills_after_credentials_added_later(tmp_path: Path) -> None:
    """Credentials entered after startup work on the next request, with no restart."""
    manager = _manager(tmp_path)
    assert load_slack_app(manager) is None

    manager.save_credentials("slack_app", {"bot_token": _BOT_TOKEN, "signing_secret": _SIGNING_SECRET})
    credentials = load_slack_app(manager)
    assert credentials is not None
    assert cached_slack_bot_identity(credentials) is None

    async with _client(lambda _request: httpx.Response(200, json=_AUTH_TEST_BODY)) as client:
        assert await slack_bot_identity(client, credentials) == _IDENTITY

    assert cached_slack_bot_identity(credentials) == _IDENTITY


@pytest.mark.asyncio
async def test_failed_identity_lookup_recovers_on_next_call() -> None:
    """A failed `auth.test` raises and caches nothing, so the next call asks Slack again."""
    bodies = iter([{"ok": False, "error": "invalid_auth"}, _AUTH_TEST_BODY])
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=next(bodies))

    async with _client(handler) as client:
        with pytest.raises(SlackApiError, match="invalid_auth"):
            await slack_bot_identity(client, _credentials())
        assert cached_slack_bot_identity(_credentials()) is None

        assert await slack_bot_identity(client, _credentials()) == _IDENTITY

    assert calls == 2
