"""Tests for egress broker configuration integration with main Config."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from mindroom.config.egress_broker import EgressBrokerConfig, EgressService
from mindroom.config.main import Config


def test_spec_example_config_loads() -> None:
    """Load the spec example YAML from section 5.2 and validate services."""
    # The example from spec section 5.2
    config_yaml = """
    egress_broker:
      unmatched_hosts: passthrough
      services:
        github:
          display_name: GitHub
          description: GitHub API, gh CLI, and git over HTTPS
          rules:
            - host: api.github.com
              auth: { type: bearer }
            - host: uploads.github.com
              auth: { type: bearer }
            - host: github.com
              auth: { type: basic, username: x-access-token }
          placeholder_env:
            GH_TOKEN: mindroom-brokered
            GITHUB_TOKEN: mindroom-brokered
        openai:
          rules:
            - host: api.openai.com
              auth: { type: header, name: Authorization, template: "Bearer {secret}" }
    """
    config = Config(**yaml.safe_load(config_yaml))

    # Validate github service
    assert "github" in config.egress_broker.services
    github = config.egress_broker.services["github"]
    assert github.display_name == "GitHub"
    assert len(github.rules) == 3
    # Third rule should have basic auth with x-access-token username
    assert github.rules[2].host == "github.com"
    assert github.rules[2].auth.type == "basic"
    assert github.rules[2].auth.username == "x-access-token"

    # Validate openai service
    assert "openai" in config.egress_broker.services
    openai = config.egress_broker.services["openai"]
    assert len(openai.rules) == 1


def test_default_is_empty_passthrough() -> None:
    """Config() default egress_broker should be empty with passthrough."""
    config = Config()
    assert config.egress_broker == EgressBrokerConfig()
    assert config.egress_broker.unmatched_hosts == "passthrough"
    assert config.egress_broker.services == {}


def test_preset_only_service_validates_with_oauth_provider() -> None:
    """`services: {github: {preset: github}}` is a complete service."""
    config = Config(**yaml.safe_load("egress_broker:\n  services:\n    github:\n      preset: github\n"))

    github = config.egress_broker.services["github"]
    assert github.preset == "github"
    assert github.oauth_provider == "github"
    assert github.display_name == "GitHub"
    assert [rule.host for rule in github.rules] == ["api.github.com", "uploads.github.com", "github.com"]
    assert github.placeholder_env == {"GH_TOKEN": "mindroom-brokered", "GITHUB_TOKEN": "mindroom-brokered"}


def test_explicit_fields_replace_preset_fields() -> None:
    """Fields set in config replace the preset's value; `rules` is replaced wholesale."""
    service = EgressService.model_validate(
        {
            "preset": "github",
            "display_name": "Work GitHub",
            "rules": [{"host": "ghe.example.com", "auth": {"type": "bearer"}}],
            "placeholder_env": {"GHE_TOKEN": "mindroom-brokered"},
            "oauth_provider": None,
        },
    )

    assert service.display_name == "Work GitHub"
    assert [rule.host for rule in service.rules] == ["ghe.example.com"]
    assert service.placeholder_env == {"GHE_TOKEN": "mindroom-brokered"}
    assert service.oauth_provider is None
    # Untouched preset fields still come through.
    assert service.description


def test_explicit_oauth_provider_overrides_preset() -> None:
    """A different provider id replaces the preset's provider."""
    service = EgressService.model_validate({"preset": "openai", "oauth_provider": "my-provider_2"})

    assert service.oauth_provider == "my-provider_2"
    assert [rule.host for rule in service.rules] == ["api.openai.com"]


def test_unknown_preset_is_rejected_by_name() -> None:
    """An unknown preset fails validation and names the preset."""
    with pytest.raises(ValidationError, match="unknown egress preset 'githb'"):
        EgressService.model_validate({"preset": "githb"})


@pytest.mark.parametrize("preset", [5, ["github"], ""])
def test_non_string_or_empty_preset_is_rejected(preset: object) -> None:
    """Preset must be a known string."""
    with pytest.raises(ValidationError):
        EgressService.model_validate({"preset": preset})


def test_service_without_preset_still_requires_rules() -> None:
    """Existing behaviour: a service with neither preset nor rules is invalid."""
    with pytest.raises(ValidationError, match="rules"):
        EgressService.model_validate({"display_name": "Nothing"})
    with pytest.raises(ValidationError):
        EgressService.model_validate({"oauth_provider": "github"})


@pytest.mark.parametrize("provider", ["github", "google_drive", "a", "0x", "my-provider_2"])
def test_oauth_provider_format_accepts_registry_style_ids(provider: str) -> None:
    """Provider ids use lowercase letters, digits, underscores, and hyphens."""
    service = EgressService.model_validate(
        {"oauth_provider": provider, "rules": [{"host": "example.com", "auth": {"type": "bearer"}}]},
    )

    assert service.oauth_provider == provider


@pytest.mark.parametrize("provider", ["", "GitHub", "_github", "-github", "git hub", "github\n", "git.hub"])
def test_oauth_provider_format_rejects_other_ids(provider: str) -> None:
    """Malformed provider ids are rejected; registry existence is checked at runtime."""
    with pytest.raises(ValidationError, match="oauth_provider"):
        EgressService.model_validate(
            {"oauth_provider": provider, "rules": [{"host": "example.com", "auth": {"type": "bearer"}}]},
        )


def test_unregistered_but_well_formed_oauth_provider_loads() -> None:
    """The registry lookup happens at runtime, so an unknown well-formed id still validates here."""
    service = EgressService.model_validate(
        {"oauth_provider": "not-in-registry", "rules": [{"host": "example.com", "auth": {"type": "bearer"}}]},
    )

    assert service.oauth_provider == "not-in-registry"


def test_preset_service_names_keep_existing_service_name_rules() -> None:
    """Presets do not bypass service-name validation."""
    with pytest.raises(ValidationError, match="_oauth"):
        EgressBrokerConfig(services={"github_oauth": {"preset": "github"}})  # type: ignore[dict-item]
