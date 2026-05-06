"""Focused tests for shared config validation helpers."""

from __future__ import annotations

import pytest

from mindroom.config.agent import AgentConfig, CultureConfig, TeamConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.models import DefaultsConfig, ToolkitDefinition
from mindroom.config.validation import duplicate_items, validate_history_limit_choice


def test_duplicate_items_preserves_first_duplicate_encounter_order() -> None:
    """Duplicate reporting should follow the first repeated occurrence."""
    assert duplicate_items(["alpha", "beta", "gamma", "beta", "alpha", "beta"]) == ["beta", "alpha"]


def test_history_limit_choice_rejects_both_limits_with_existing_message() -> None:
    """Shared history validation should keep the existing user-facing message."""
    with pytest.raises(ValueError, match="num_history_runs and num_history_messages are mutually exclusive"):
        validate_history_limit_choice(num_history_runs=2, num_history_messages=10)


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: ToolkitDefinition(tools=["shell", "file", "shell", "file"]),
            "Duplicate toolkit tools are not allowed: shell, file",
        ),
        (
            lambda: AgentConfig(
                display_name="Code",
                allowed_toolkits=["dev", "research", "dev", "research"],
            ),
            "Duplicate allowed_toolkits are not allowed: dev, research",
        ),
        (
            lambda: AgentConfig(
                display_name="Code",
                initial_toolkits=["dev", "research", "dev", "research"],
            ),
            "Duplicate initial_toolkits are not allowed: dev, research",
        ),
        (
            lambda: AgentConfig(
                display_name="Code",
                knowledge_bases=["docs", "runbooks", "docs", "runbooks"],
            ),
            "Duplicate knowledge bases are not allowed: docs, runbooks",
        ),
        (
            lambda: TeamConfig(
                display_name="Team",
                role="Work",
                agents=["code", "research", "code", "research"],
            ),
            "Duplicate agents are not allowed in a team: code, research",
        ),
        (
            lambda: CultureConfig(agents=["code", "research", "code", "research"]),
            "Duplicate agents are not allowed in a culture: code, research",
        ),
        (
            lambda: AuthorizationConfig(
                aliases={
                    "@alice:example.com": ["@telegram_1:example.com", "@discord_1:example.com"],
                    "@bob:example.com": ["@telegram_1:example.com", "@discord_1:example.com"],
                },
            ),
            "Duplicate bridge aliases are not allowed: @telegram_1:example.com, @discord_1:example.com",
        ),
    ],
)
def test_duplicate_validators_preserve_error_order(factory: object, match: str) -> None:
    """Config duplicate validators should preserve encounter-ordered duplicate names."""
    with pytest.raises(ValueError, match=match):
        factory()


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: DefaultsConfig(num_history_runs=2, num_history_messages=10),
            "num_history_runs and num_history_messages are mutually exclusive",
        ),
        (
            lambda: AgentConfig(display_name="Code", num_history_runs=2, num_history_messages=10),
            "num_history_runs and num_history_messages are mutually exclusive",
        ),
        (
            lambda: TeamConfig(
                display_name="Team",
                role="Work",
                agents=["code"],
                num_history_runs=2,
                num_history_messages=10,
            ),
            "num_history_runs and num_history_messages are mutually exclusive",
        ),
    ],
)
def test_history_limit_validators_preserve_error_message(factory: object, match: str) -> None:
    """Defaults, agents, and teams should reject ambiguous history limits consistently."""
    with pytest.raises(ValueError, match=match):
        factory()
