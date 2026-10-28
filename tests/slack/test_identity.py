"""Tests for mapping a Slack author to a MindRoom account by email."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
import pytest

from mindroom.config.main import Config
from mindroom.config.slack import SlackConfig
from mindroom.slack import identity
from mindroom.slack.api import SlackApiError
from mindroom.slack.identity import SlackAuthor, slack_author
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_BOT_TOKEN = "xoxb-test-bot-token"  # noqa: S105
_TEAM = "T0123ABCD"
_USER_ID = "U0123ABCD"
_CONFIG = SlackConfig(email_domain="example.com", matrix_user_id_template="@{localpart}:example.org")


@pytest.fixture(autouse=True)
def _empty_author_cache() -> Iterator[None]:
    identity._PROFILES.clear()
    yield
    identity._PROFILES.clear()


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    """Return a runtime on the `localhost` homeserver, whose router account is `@mindroom_router:localhost`."""
    return test_runtime_paths(tmp_path)


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


async def _author(
    slack: _FakeSlack,
    paths: RuntimePaths,
    slack_config: SlackConfig = _CONFIG,
    **kwargs: Any,  # noqa: ANN401
) -> SlackAuthor:
    async with slack.client() as client:
        return await slack_author(client, _BOT_TOKEN, _TEAM, _USER_ID, Config(slack=slack_config), paths, **kwargs)


@pytest.mark.asyncio
async def test_maps_member_email(paths: RuntimePaths) -> None:
    """A workspace member whose email is in the domain gets the templated Matrix ID and their Slack display name."""
    slack = _slack(_user())

    assert await _author(slack, paths) == SlackAuthor(_USER_ID, "Ada", "@ada:example.org")
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
async def test_rejects_guest_bot_deleted_other_team(overrides: dict[str, Any], paths: RuntimePaths) -> None:
    """Accounts that are not full members of our workspace have no Matrix ID, but keep their display name."""
    author = await _author(_slack(_user(**overrides)), paths)

    assert author == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.parametrize("email", ["ada@other.example", "ada@example.com.evil.example", "ada@sub.example.com", "ada"])
@pytest.mark.asyncio
async def test_rejects_other_domain(email: str, paths: RuntimePaths) -> None:
    """An email outside the configured domain does not map."""
    assert await _author(_slack(_user(email)), paths) == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.asyncio
async def test_email_domain_is_case_insensitive(paths: RuntimePaths) -> None:
    """The domain matches in any case; the localpart reaches the template unchanged."""
    assert await _author(_slack(_user("ada@EXAMPLE.com")), paths) == SlackAuthor(_USER_ID, "Ada", "@ada:example.org")
    identity._PROFILES.clear()
    assert await _author(_slack(_user("Ada.L@Example.COM")), paths) == SlackAuthor(
        _USER_ID,
        "Ada",
        "@Ada.L:example.org",
    )


@pytest.mark.asyncio
async def test_localpart_cannot_name_another_server(paths: RuntimePaths) -> None:
    """A localpart that would make the templated ID invalid does not map."""
    author = await _author(_slack(_user("ada:evil.example@example.com")), paths)

    assert author == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.parametrize("localpart", ["mindroom_router", "mindroom_launch"])
@pytest.mark.asyncio
async def test_email_naming_a_mindroom_account_does_not_map(localpart: str, paths: RuntimePaths) -> None:
    """An email whose localpart is a MindRoom account's own, such as the router's or an agent's, never drives an agent."""
    slack_config = SlackConfig(email_domain="example.com", matrix_user_id_template="@{localpart}:localhost")
    config = Config(slack=slack_config, agents={"launch": {"display_name": "Launch"}})
    async with _slack(_user(f"{localpart}@example.com")).client() as client:
        author = await slack_author(client, _BOT_TOKEN, _TEAM, _USER_ID, config, paths)
        person = await slack_author(client, _BOT_TOKEN, _TEAM, "U0OTHER99", Config(slack=slack_config), paths)

    assert author == SlackAuthor(_USER_ID, "Ada", None)
    # Without that agent configured, the same localpart names nobody MindRoom runs, so it maps.
    assert person.matrix_id == (None if localpart == "mindroom_router" else "@mindroom_launch:localhost")


@pytest.mark.asyncio
async def test_missing_email_does_not_map(paths: RuntimePaths) -> None:
    """A profile without an email, or with a blank one, has no Matrix ID."""
    assert await _author(_slack(_user(None)), paths) == SlackAuthor(_USER_ID, "Ada", None)
    identity._PROFILES.clear()
    assert await _author(_slack(_user("")), paths) == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.asyncio
async def test_unset_template_does_not_map(paths: RuntimePaths) -> None:
    """Without `slack.matrix_user_id_template` nobody maps."""
    assert await _author(_slack(_user()), paths, SlackConfig()) == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.asyncio
async def test_display_name_falls_back_to_real_name_then_handle(paths: RuntimePaths) -> None:
    """The display name follows the Slack display-name rule used everywhere else."""
    user = _user(profile={"display_name": "", "real_name": "Ada Lovelace", "email": "ada@example.com"})
    assert (await _author(_slack(user), paths)).display_name == "Ada Lovelace"
    identity._PROFILES.clear()
    user = _user(profile={"email": "ada@example.com"})
    assert (await _author(_slack(user), paths)).display_name == "ada"


@pytest.mark.parametrize("overrides", [{}, {"is_bot": True}])
@pytest.mark.asyncio
async def test_cache_hit_skips_api(overrides: dict[str, Any], paths: RuntimePaths) -> None:
    """A second lookup for the same team and user does not call Slack, even when the first could not map."""
    slack = _slack(_user(**overrides))

    first = await _author(slack, paths)

    assert await _author(slack, paths) == first
    assert len(slack.requests) == 1


@pytest.mark.asyncio
async def test_cache_is_per_team_and_user(paths: RuntimePaths) -> None:
    """Another user, or the same ID in another team, is looked up on its own."""
    slack = _slack(_user())
    async with slack.client() as client:
        await slack_author(client, _BOT_TOKEN, _TEAM, _USER_ID, Config(slack=_CONFIG), paths)
        await slack_author(client, _BOT_TOKEN, _TEAM, "U0OTHER99", Config(slack=_CONFIG), paths)
        await slack_author(client, _BOT_TOKEN, "T0OTHER99", _USER_ID, Config(slack=_CONFIG), paths)

    assert len(slack.requests) == 3


@pytest.mark.asyncio
async def test_cache_expires_after_ten_minutes(paths: RuntimePaths) -> None:
    """An entry is used until 600 seconds old, then looked up again."""
    slack = _slack(_user())
    clock = [1000.0]

    def now() -> float:
        return clock[0]

    await _author(slack, paths, now=now)
    clock[0] += 599
    await _author(slack, paths, now=now)
    assert len(slack.requests) == 1

    clock[0] += 1
    await _author(slack, paths, now=now)
    assert len(slack.requests) == 2

    clock[0] += 599
    await _author(slack, paths, now=now)
    assert len(slack.requests) == 2, "the refresh restarts the 600 seconds"


@pytest.mark.asyncio
async def test_cache_drops_oldest_at_cap(monkeypatch: pytest.MonkeyPatch, paths: RuntimePaths) -> None:
    """At the cap the oldest entry goes, not the newest."""
    monkeypatch.setattr(identity, "_CACHE_MAX_ENTRIES", 2)
    slack = _slack(_user())
    async with slack.client() as client:
        for user_id in ("U0000000A", "U0000000B", "U0000000C"):
            await slack_author(client, _BOT_TOKEN, _TEAM, user_id, Config(slack=_CONFIG), paths)
        assert list(identity._PROFILES) == [(_TEAM, "U0000000B"), (_TEAM, "U0000000C")]

        await slack_author(client, _BOT_TOKEN, _TEAM, "U0000000B", Config(slack=_CONFIG), paths)
        assert len(slack.requests) == 3

        await slack_author(client, _BOT_TOKEN, _TEAM, "U0000000A", Config(slack=_CONFIG), paths)
        assert len(slack.requests) == 4


@pytest.mark.asyncio
async def test_config_changes_apply_to_a_cached_profile(paths: RuntimePaths) -> None:
    """The cache holds what Slack said, so a reloaded config maps a cached person with no new request."""
    slack = _slack(_user())
    other_template = SlackConfig(email_domain="example.com", matrix_user_id_template="@staff.{localpart}:example.net")
    other_domain = SlackConfig(email_domain="other.example", matrix_user_id_template="@{localpart}:example.org")

    assert await _author(slack, paths) == SlackAuthor(_USER_ID, "Ada", "@ada:example.org")
    assert await _author(slack, paths, other_template) == SlackAuthor(_USER_ID, "Ada", "@staff.ada:example.net")
    assert await _author(slack, paths, other_domain) == SlackAuthor(_USER_ID, "Ada", None)
    assert await _author(slack, paths, SlackConfig()) == SlackAuthor(_USER_ID, "Ada", None)
    assert await _author(slack, paths) == SlackAuthor(_USER_ID, "Ada", "@ada:example.org")
    assert len(slack.requests) == 1


@pytest.mark.asyncio
async def test_team_is_the_callers_team(paths: RuntimePaths) -> None:
    """A user maps only when their team is the team the caller passes, which is the bot token's own team."""
    slack = _slack(_user(team_id="T0OTHER99"))
    async with slack.client() as client:
        ours = await slack_author(client, _BOT_TOKEN, "T0OTHER99", _USER_ID, Config(slack=_CONFIG), paths)
        theirs = await slack_author(client, _BOT_TOKEN, _TEAM, _USER_ID, Config(slack=_CONFIG), paths)

    assert ours == SlackAuthor(_USER_ID, "Ada", "@ada:example.org")
    assert theirs == SlackAuthor(_USER_ID, "Ada", None)


@pytest.mark.parametrize(
    ("overrides", "matrix_id"),
    [
        ({"is_email_confirmed": True}, "@ada:example.org"),
        ({}, "@ada:example.org"),
        ({"is_email_confirmed": False}, None),
    ],
)
@pytest.mark.asyncio
async def test_unconfirmed_email_does_not_map(
    overrides: dict[str, Any],
    matrix_id: str | None,
    paths: RuntimePaths,
) -> None:
    """An email Slack reports as unconfirmed proves nothing; a profile that does not say is taken as is."""
    assert await _author(_slack(_user(**overrides)), paths) == SlackAuthor(_USER_ID, "Ada", matrix_id)


@pytest.mark.parametrize("error", ["user_not_found", "user_not_visible"])
@pytest.mark.asyncio
async def test_unknown_user_is_unmappable_and_cached(error: str, paths: RuntimePaths) -> None:
    """Slack's final "no such person" answers are a refusal named by the ID, not a retryable failure."""
    slack = _FakeSlack({"ok": False, "error": error})

    first = await _author(slack, paths)

    assert first == SlackAuthor(_USER_ID, _USER_ID, None)
    assert await _author(slack, paths, SlackConfig()) == first
    assert len(slack.requests) == 1


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"ok": False, "error": "internal_error"}, "internal_error"),
        ({"ok": False, "error": "ratelimited"}, "ratelimited"),
        ({"ok": True}, "invalid_response"),
    ],
)
@pytest.mark.asyncio
async def test_other_slack_errors_propagate_and_are_not_cached(
    body: dict[str, Any],
    error: str,
    paths: RuntimePaths,
) -> None:
    """Any other failed or malformed `users.info` raises `SlackApiError` and leaves nothing in the cache."""
    slack = _FakeSlack(body)

    with pytest.raises(SlackApiError, match=error):
        await _author(slack, paths)

    assert not identity._PROFILES
    slack.body = {"ok": True, "user": _user()}
    assert (await _author(slack, paths)).matrix_id == "@ada:example.org"
