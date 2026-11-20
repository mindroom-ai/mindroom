"""Tests for the built-in egress service presets."""

from __future__ import annotations

import base64

import pytest

from mindroom.config.egress_broker import EgressBrokerConfig, EgressService
from mindroom.egress_broker.presets import EGRESS_PRESETS
from mindroom.egress_broker.rules import inject_credentials, match_rule

_BROKERED = "mindroom-brokered"

# (host, path_prefix, auth type, auth username, auth name); written out independently of presets.py.
type _ExpectedRule = tuple[str, str, str, str | None, str | None]

_EXPECTED: dict[str, tuple[str | None, list[_ExpectedRule], dict[str, str]]] = {
    "github": (
        "github",
        [
            ("api.github.com", "/", "bearer", None, None),
            ("uploads.github.com", "/", "bearer", None, None),
            ("github.com", "/", "basic", "x-access-token", None),
        ],
        {"GH_TOKEN": _BROKERED, "GITHUB_TOKEN": _BROKERED},
    ),
    "google_drive": (
        "google_drive",
        [
            ("www.googleapis.com", "/drive/", "bearer", None, None),
            ("www.googleapis.com", "/upload/drive/", "bearer", None, None),
        ],
        {},
    ),
    "google_gmail": (
        "google_gmail",
        [
            ("gmail.googleapis.com", "/", "bearer", None, None),
            ("www.googleapis.com", "/gmail/", "bearer", None, None),
        ],
        {},
    ),
    "google_calendar": (
        "google_calendar",
        [("www.googleapis.com", "/calendar/", "bearer", None, None)],
        {},
    ),
    "google_sheets": (
        "google_sheets",
        [("sheets.googleapis.com", "/", "bearer", None, None)],
        {},
    ),
    "google_docs": (
        "google_docs",
        [("docs.googleapis.com", "/", "bearer", None, None)],
        {},
    ),
    "google_tasks": (
        "google_tasks",
        [
            ("tasks.googleapis.com", "/", "bearer", None, None),
            ("www.googleapis.com", "/tasks/", "bearer", None, None),
        ],
        {},
    ),
    "atlassian": (
        "atlassian",
        [("api.atlassian.com", "/", "bearer", None, None)],
        {},
    ),
    "overleaf": (
        None,
        [("git.overleaf.com", "/", "basic", "git", None)],
        {},
    ),
    "openai": (
        None,
        [("api.openai.com", "/", "bearer", None, None)],
        {"OPENAI_API_KEY": _BROKERED},
    ),
    "anthropic": (
        None,
        [("api.anthropic.com", "/", "header", None, "x-api-key")],
        {"ANTHROPIC_API_KEY": _BROKERED},
    ),
}


def test_preset_table_has_exactly_the_documented_names() -> None:
    """The built-in table offers exactly the expected presets and nothing else."""
    assert set(EGRESS_PRESETS) == set(_EXPECTED)


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_preset_expands_to_documented_rules(name: str) -> None:
    """Each preset validates and expands to the documented provider, rules, and placeholders."""
    provider, rules, placeholders = _EXPECTED[name]

    service = EgressService(preset=name)

    assert service.preset == name
    assert service.oauth_provider == provider
    assert [
        (rule.host, rule.path_prefix, rule.auth.type, rule.auth.username, rule.auth.name) for rule in service.rules
    ] == rules
    assert all(rule.port is None for rule in service.rules)
    assert all(rule.auth.template == "{secret}" for rule in service.rules)
    assert service.placeholder_env == placeholders
    assert service.display_name
    assert service.description


def test_preset_data_is_not_shared_between_services() -> None:
    """Expanding a preset never aliases the built-in table."""
    first = EgressService(preset="github")
    first.placeholder_env["EXTRA"] = "x"
    first.rules.clear()

    second = EgressService(preset="github")

    assert "EXTRA" not in second.placeholder_env
    assert len(second.rules) == 3
    assert "EXTRA" not in EGRESS_PRESETS["github"]["placeholder_env"]  # type: ignore[operator]


def test_googleapis_presets_share_host_and_match_by_longest_path() -> None:
    """Drive, Calendar, Gmail, and Tasks share www.googleapis.com and are told apart by path."""
    config = EgressBrokerConfig(
        services={
            "drive": EgressService(preset="google_drive"),
            "calendar": EgressService(preset="google_calendar"),
            "gmail": EgressService(preset="google_gmail"),
            "tasks": EgressService(preset="google_tasks"),
        },
    )

    def service_for(path: str) -> str | None:
        match = match_rule(config, "www.googleapis.com", 443, path)
        return match.service if match else None

    assert service_for("/drive/v3/files") == "drive"
    assert service_for("/upload/drive/v3/files") == "drive"
    assert service_for("/calendar/v3/calendars/primary/events") == "calendar"
    assert service_for("/gmail/v1/users/me/messages") == "gmail"
    assert service_for("/tasks/v1/lists") == "tasks"
    assert service_for("/youtube/v3/videos") is None
    assert service_for("/") is None
    assert match_rule(config, "gmail.googleapis.com", 443, "/gmail/v1/users/me").service == "gmail"  # type: ignore[union-attr]
    assert match_rule(config, "tasks.googleapis.com", 443, "/tasks/v1/lists").service == "tasks"  # type: ignore[union-attr]


def test_overleaf_preset_sends_the_git_token_as_basic_auth_for_user_git() -> None:
    """Overleaf's Git bridge takes the token as the password of user `git`, on git.overleaf.com only."""
    config = EgressBrokerConfig(services={"overleaf": EgressService(preset="overleaf")})

    match = match_rule(config, "git.overleaf.com", 443, "/0123456789abcdef01234567/git-upload-pack")
    assert match is not None
    assert match.service == "overleaf"
    assert match_rule(config, "www.overleaf.com", 443, "/project") is None

    headers, _ = inject_credentials([], b"/0123456789abcdef01234567/info/refs", match.rule.auth, "olp_token")
    assert headers == [(b"authorization", b"Basic " + base64.b64encode(b"git:olp_token"))]


@pytest.mark.parametrize(
    "sent",
    [
        [(b"authorization", b"Bearer mindroom-brokered")],
        [(b"x-api-key", b"mindroom-brokered")],
        [(b"Authorization", b"Bearer mindroom-brokered"), (b"x-api-key", b"mindroom-brokered")],
    ],
    ids=["sdk-2-bearer", "x-api-key", "both"],
)
def test_anthropic_preset_sends_one_key_whichever_header_the_client_used(sent: list[tuple[bytes, bytes]]) -> None:
    """The Anthropic SDK sends its key as a bearer token since version 2; the API gets the key in ``x-api-key`` alone."""
    config = EgressBrokerConfig(services={"anthropic": EgressService(preset="anthropic")})
    match = match_rule(config, "api.anthropic.com", 443, "/v1/messages")
    assert match is not None
    other = (b"authorization-hint", b"Bearer something-else")

    headers, _ = inject_credentials(
        [*sent, other],
        b"/v1/messages",
        match.rule.auth,
        "sk-ant-real",
        placeholders=match.definition.placeholder_env.values(),
    )
    assert headers == [other, (b"x-api-key", b"sk-ant-real")]


def test_presets_declare_read_only() -> None:
    """The Google presets and Atlassian offer read-only access; git hosts do not, and only Calendar reads with POST."""
    read_only = {name for name in _EXPECTED if EgressService(preset=name).read_only is not None}

    assert read_only == {
        "google_drive",
        "google_gmail",
        "google_calendar",
        "google_sheets",
        "google_docs",
        "google_tasks",
        "atlassian",
    }
    calendar = EgressService(preset="google_calendar").read_only
    assert calendar is not None
    assert calendar.post_paths == ["/calendar/v3/freeBusy"]
    assert EgressService(preset="google_drive").read_only.post_paths == []  # type: ignore[union-attr]
