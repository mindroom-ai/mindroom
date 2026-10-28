"""Tests for mapping a Slack author to a MindRoom account by email."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
import pytest

from mindroom.config.slack import SlackConfig
from mindroom.slack import identity
from mindroom.slack.api import SlackApiError
from mindroom.slack.identity import SlackAuthor, slack_author

if TYPE_CHECKING:
    from collections.abc import Iterator

_BOT_TOKEN = "xoxb-test-bot-token"  # noqa: S105
_TEAM = "T0123ABCD"
_USER_ID = "U0123ABCD"
_CONFIG = SlackConfig(email_domain="example.com", matrix_user_id_template="@{localpart}:example.org")


@pytest.fixture(autouse=True)
def _empty_author_cache() -> Iterator[None]:
    identity._AUTHORS.clear()
    yield
    identity._AUTHORS.clear()


def _user(email: str | None = "ada@example.com", **overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    profile = {"display_name": "Ada", "real_name": "Ada Lovelace"}
    if email is not None:
        profile["email"] = email
    return {"id": _USER_ID, "team_id": _TEAM, "name": "ada", "profile": profile, **overrides}


class _FakeSlack:
    """Answers `users.info` with a fixed body and records each request."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle), follow_redirects=False, timeout=10.0)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.body)


def _slack(user: dict[str, Any]) -> _FakeSlack:
    return _FakeSlack({"ok": True, "user": user})


async def _author(slack: _FakeSlack, config: SlackConfig = _CONFIG, **kwargs: Any) -> SlackAuthor:  # noqa: ANN401
    async with slack.client() as client:
        return await slack_author(client, _BOT_TOKEN, _TEAM, _USER_ID, config, **kwargs)


@pytest.mark.asyncio
async def test_maps_member_email() -> None:
    """A workspace member whose email is in the domain gets the templated Matrix ID and their Slack display name."""
    slack = _slack(_user())

    assert await _author(slack) == SlackAuthor(_USER_ID, "Ada", "@ada:example.org")
    (request,) = slack.requests
    assert request.url.path == "/api/users.info"
    assert request.headers["Authorization"] == f"Bearer {_BOT_TOKEN}"
    assert parse_qs(request.content.decode()) == {"user": [_USER_ID]}


@pytest.mark.parametrize(
    "overrides",
    [
        {"deleted": True},
        {"is_bot": True},
        {"is_app_user": True},
        {"is_restricted": True},
        {"is_ultra_restricted": True},
        {"team_id": "T0OTHER99"},
        {"team_id": None},
    ],
)
@pytest.mark.asyncio
async def test_rejects_guest_bot_deleted_other_team(overrides: dict[str, Any]) -> None:
    """Accounts that are not full members of our workspace have no Matrix ID, but keep their display name."""
    author = await _author(_slack(_user(**overrides)))

    assert author == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.parametrize("email", ["ada@other.example", "ada@example.com.evil.example", "ada@sub.example.com", "ada"])
@pytest.mark.asyncio
async def test_rejects_other_domain(email: str) -> None:
    """An email outside the configured domain does not map."""
    assert await _author(_slack(_user(email))) == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.asyncio
async def test_email_domain_is_case_insensitive() -> None:
    """The domain matches in any case; the localpart reaches the template unchanged."""
    assert await _author(_slack(_user("ada@EXAMPLE.com"))) == SlackAuthor(_USER_ID, "Ada", "@ada:example.org")
    identity._AUTHORS.clear()
    assert await _author(_slack(_user("Ada.L@Example.COM"))) == SlackAuthor(_USER_ID, "Ada", "@Ada.L:example.org")


@pytest.mark.asyncio
async def test_localpart_cannot_name_another_server() -> None:
    """A localpart that would make the templated ID invalid does not map."""
    author = await _author(_slack(_user("ada:evil.example@example.com")))

    assert author == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.asyncio
async def test_missing_email_does_not_map() -> None:
    """A profile without an email, or with a blank one, has no Matrix ID."""
    assert await _author(_slack(_user(None))) == SlackAuthor(_USER_ID, "Ada", None)
    identity._AUTHORS.clear()
    assert await _author(_slack(_user(""))) == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.asyncio
async def test_unset_template_does_not_map() -> None:
    """Without `slack.matrix_user_id_template` nobody maps."""
    assert await _author(_slack(_user()), SlackConfig()) == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.asyncio
async def test_display_name_falls_back_to_real_name_then_handle() -> None:
    """The display name follows the Slack display-name rule used everywhere else."""
    user = _user(profile={"display_name": "", "real_name": "Ada Lovelace", "email": "ada@example.com"})
    assert (await _author(_slack(user))).display_name == "Ada Lovelace"
    identity._AUTHORS.clear()
    user = _user(profile={"email": "ada@example.com"})
    assert (await _author(_slack(user))).display_name == "ada"


@pytest.mark.parametrize("overrides", [{}, {"is_bot": True}])
@pytest.mark.asyncio
async def test_cache_hit_skips_api(overrides: dict[str, Any]) -> None:
    """A second lookup for the same team and user does not call Slack, even when the first could not map."""
    slack = _slack(_user(**overrides))

    first = await _author(slack)

    assert await _author(slack) == first
    assert len(slack.requests) == 1


@pytest.mark.asyncio
async def test_cache_is_per_team_and_user() -> None:
    """Another user, or the same ID in another team, is looked up on its own."""
    slack = _slack(_user())
    async with slack.client() as client:
        await slack_author(client, _BOT_TOKEN, _TEAM, _USER_ID, _CONFIG)
        await slack_author(client, _BOT_TOKEN, _TEAM, "U0OTHER99", _CONFIG)
        await slack_author(client, _BOT_TOKEN, "T0OTHER99", _USER_ID, _CONFIG)

    assert len(slack.requests) == 3


@pytest.mark.asyncio
async def test_cache_expires_after_ten_minutes() -> None:
    """An entry is used until 600 seconds old, then looked up again."""
    slack = _slack(_user())
    clock = [1000.0]

    def now() -> float:
        return clock[0]

    await _author(slack, now=now)
    clock[0] += 599
    await _author(slack, now=now)
    assert len(slack.requests) == 1

    clock[0] += 1
    await _author(slack, now=now)
    assert len(slack.requests) == 2

    clock[0] += 599
    await _author(slack, now=now)
    assert len(slack.requests) == 2, "the refresh restarts the 600 seconds"


@pytest.mark.asyncio
async def test_cache_drops_oldest_at_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """At the cap the oldest entry goes, not the newest."""
    monkeypatch.setattr(identity, "_CACHE_MAX_ENTRIES", 2)
    slack = _slack(_user())
    async with slack.client() as client:
        for user_id in ("U0000000A", "U0000000B", "U0000000C"):
            await slack_author(client, _BOT_TOKEN, _TEAM, user_id, _CONFIG)
        assert list(identity._AUTHORS) == [(_TEAM, "U0000000B"), (_TEAM, "U0000000C")]

        await slack_author(client, _BOT_TOKEN, _TEAM, "U0000000B", _CONFIG)
        assert len(slack.requests) == 3

        await slack_author(client, _BOT_TOKEN, _TEAM, "U0000000A", _CONFIG)
        assert len(slack.requests) == 4


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"ok": False, "error": "user_not_found"}, "user_not_found"),
        ({"ok": True}, "invalid_response"),
    ],
)
@pytest.mark.asyncio
async def test_slack_errors_propagate_and_are_not_cached(body: dict[str, Any], error: str) -> None:
    """A failed or malformed `users.info` raises `SlackApiError` and leaves nothing in the cache."""
    slack = _FakeSlack(body)

    with pytest.raises(SlackApiError, match=error):
        await _author(slack)

    assert not identity._AUTHORS
    slack.body = {"ok": True, "user": _user()}
    assert (await _author(slack)).matrix_id == "@ada:example.org"
