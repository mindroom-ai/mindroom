"""Tests for the top-level `slack:` config section."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.config.slack import SlackConfig

_MAPPING_ERROR = "Email mapping requires a valid Matrix template and explicit email domain"


def _config(**slack: object) -> Config:
    return Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "router": {"model": "default"},
            "slack": slack,
        },
    )


def test_slack_config_defaults_to_no_mapping() -> None:
    """A config without a `slack:` section maps no Slack emails to Matrix users."""
    config = Config.model_validate(
        {"models": {"default": {"provider": "openai", "id": "gpt-6-astra"}}, "router": {"model": "default"}},
    )

    assert config.slack == SlackConfig()
    assert config.slack.email_domain is None
    assert config.slack.matrix_user_id_template is None


def test_slack_config_accepts_a_complete_mapping() -> None:
    """A domain plus a template with one localpart placeholder is a valid mapping."""
    config = _config(email_domain="example.com", matrix_user_id_template="@{localpart}:example.org")

    assert config.slack.email_domain == "example.com"
    assert config.slack.matrix_user_id_template == "@{localpart}:example.org"


@pytest.mark.parametrize(
    "fields",
    [
        {"email_domain": "example.com"},
        {"matrix_user_id_template": "@{localpart}:example.org"},
    ],
)
def test_slack_config_requires_both_fields(fields: dict[str, str]) -> None:
    """Setting only the domain or only the template is rejected."""
    with pytest.raises(ValidationError, match="Set email_domain and matrix_user_id_template together"):
        _config(**fields)


@pytest.mark.parametrize(
    "template",
    [
        "@user:example.org",
        "@{localpart}{localpart}:example.org",
        "@{localpart}:{server}",
        "{localpart}",
    ],
)
def test_template_requires_single_localpart(template: str) -> None:
    """The Matrix template must hold exactly one `{localpart}` and form a valid Matrix ID."""
    with pytest.raises(ValidationError, match=_MAPPING_ERROR):
        _config(email_domain="example.com", matrix_user_id_template=template)


def test_slack_config_rejects_unknown_fields() -> None:
    """The section is closed: misspelled keys fail instead of being ignored."""
    with pytest.raises(ValidationError, match="email_domian"):
        _config(email_domian="example.com")
