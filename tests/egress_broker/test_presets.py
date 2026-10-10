"""Tests for the built-in egress service presets."""

from __future__ import annotations

import pytest

from mindroom.config.egress_broker import EgressBrokerConfig, EgressService
from mindroom.egress_broker.presets import EGRESS_PRESETS
from mindroom.egress_broker.rules import match_rule

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
    """The built-in table offers the spec section 10.2 presets and nothing else."""
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
