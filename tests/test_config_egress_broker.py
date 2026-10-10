"""Tests for egress broker configuration integration with main Config."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from mindroom.config.egress_broker import EgressBrokerConfig, EgressService
from mindroom.config.main import Config
from mindroom.egress_broker.presets import EGRESS_PRESETS


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


def test_service_without_rules_or_preset_is_rejected_naming_the_service() -> None:
    """A service that resolves to no rules is invalid, and the error path names the service."""
    with pytest.raises(ValidationError, match=r"(?s)services\.nothing\b.*at least one rule"):
        Config(**yaml.safe_load("egress_broker:\n  services:\n    nothing:\n      display_name: Nothing\n"))
    with pytest.raises(ValidationError, match="at least one rule"):
        EgressService.model_validate({"oauth_provider": "github"})
    with pytest.raises(ValidationError, match="at least one rule"):
        EgressService.model_validate({"preset": "github", "rules": []})


def test_preset_only_service_round_trips_through_authored_dump() -> None:
    """Expanded preset values are not authored, so saving config keeps just the preset reference."""
    config = Config(**yaml.safe_load("egress_broker:\n  services:\n    github:\n      preset: github\n"))

    assert config.authored_model_dump()["egress_broker"] == {"services": {"github": {"preset": "github"}}}
    # The resolved model still exposes the full preset.
    github = config.egress_broker.services["github"]
    assert len(github.rules) == 3
    assert github.oauth_provider == "github"
    assert github.model_fields_set == {"preset"}


def test_authored_overrides_stay_set_alongside_preset() -> None:
    """Only explicitly authored fields are persisted; the rest keep coming from the preset."""
    config = Config(
        **yaml.safe_load(
            "egress_broker:\n  services:\n    github:\n      preset: github\n      display_name: GH\n",
        ),
    )

    assert config.authored_model_dump()["egress_broker"] == {
        "services": {"github": {"preset": "github", "display_name": "GH"}},
    }
    github = config.egress_broker.services["github"]
    assert github.display_name == "GH"
    assert github.description
    assert len(github.rules) == 3

    rules_authored = Config(
        egress_broker={
            "services": {
                "github": {"preset": "github", "rules": [{"host": "ghe.example.com", "auth": {"type": "bearer"}}]},
            },
        },
    )
    dumped = rules_authored.authored_model_dump()["egress_broker"]["services"]["github"]
    assert set(dumped) == {"preset", "rules"}
    assert dumped["rules"][0]["host"] == "ghe.example.com"


def test_authored_dump_reloads_to_the_same_services() -> None:
    """Re-validating the authored dump yields the same resolved services."""
    config = Config(egress_broker={"services": {"github": {"preset": "github", "display_name": "GH"}}})

    reloaded = Config(**config.authored_model_dump())

    assert reloaded.egress_broker == config.egress_broker


def test_rules_are_optional_in_the_schema() -> None:
    """Presets can supply the rules, so the JSON schema must not require them."""
    schema = EgressService.model_json_schema()

    assert "rules" not in schema.get("required", [])


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


def test_oauth_on_shared_workers_defaults_off_and_no_preset_sets_it() -> None:
    """Requester-scoped accounts stay off shared workers unless the operator opts in, never through a preset."""
    service = EgressService.model_validate({"preset": "github"})

    assert service.oauth_on_shared_workers is False
    assert all("oauth_on_shared_workers" not in preset for preset in EGRESS_PRESETS.values())


def test_oauth_on_shared_workers_survives_the_authored_dump() -> None:
    """The opt-in is an authored field, so saving config keeps it next to the preset reference."""
    config = Config(egress_broker={"services": {"github": {"preset": "github", "oauth_on_shared_workers": True}}})

    dumped = config.authored_model_dump()["egress_broker"]

    assert dumped == {"services": {"github": {"preset": "github", "oauth_on_shared_workers": True}}}
    assert Config(**config.authored_model_dump()).egress_broker.services["github"].oauth_on_shared_workers is True


def test_restrict_to_rules_defaults_off_and_no_preset_sets_it() -> None:
    """Unlisted paths are forwarded without credentials unless the service opts in; presets never opt in."""
    service = EgressService.model_validate({"preset": "github"})

    assert service.restrict_to_rules is False
    assert all("restrict_to_rules" not in preset for preset in EGRESS_PRESETS.values())


def test_restrict_to_rules_survives_the_authored_dump() -> None:
    """The restriction is an authored field, so saving config keeps it next to the preset reference."""
    config = Config(egress_broker={"services": {"github": {"preset": "github", "restrict_to_rules": True}}})

    dumped = config.authored_model_dump()["egress_broker"]

    assert dumped == {"services": {"github": {"preset": "github", "restrict_to_rules": True}}}
    assert Config(**config.authored_model_dump()).egress_broker.services["github"].restrict_to_rules is True
