"""Tests for egress broker configuration integration with main Config."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.config.main import Config
from mindroom.egress_broker.presets import EGRESS_PRESETS


def test_example_config_loads() -> None:
    """An example config with GitHub and OpenAI services loads with its rules and auth intact."""
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


def test_private_networks_round_trip_through_the_authored_dump() -> None:
    """Listed networks load, normalize to their canonical form, and dump back as plain strings for YAML."""
    config = Config.model_validate({"egress_broker": {"private_networks": ["192.168.1.0/24", "FD00::/8"]}})

    assert config.egress_broker.private_networks == ["192.168.1.0/24", "fd00::/8"]
    assert Config.model_validate(yaml.safe_load(yaml.safe_dump(config.authored_model_dump()))) == config


@pytest.mark.parametrize("network", ["192.168.1.5/24", "lab.example", "192.168.1.0/33", ""])
def test_invalid_private_network_rejected(network: str) -> None:
    """A network with host bits set, or anything that is not a network, is refused naming the value."""
    with pytest.raises(ValidationError, match="private_networks"):
        EgressBrokerConfig(private_networks=[network])


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


@pytest.mark.parametrize("provider", ["github", "google_drive", "a", "0x", "my-provider_2", "not-in-registry"])
def test_oauth_provider_format_accepts_registry_style_ids(provider: str) -> None:
    """Provider ids use lowercase letters, digits, underscores, and hyphens; the registry is checked at runtime."""
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


def test_preset_service_names_keep_existing_service_name_rules() -> None:
    """Presets do not bypass service-name validation."""
    with pytest.raises(ValidationError, match="_oauth"):
        EgressBrokerConfig(services={"github_oauth": {"preset": "github"}})  # type: ignore[dict-item]


@pytest.mark.parametrize("field", ["oauth_on_shared_workers", "restrict_to_rules", "restrict_only"])
def test_opt_in_field_defaults_off_and_no_preset_sets_it(field: str) -> None:
    """Shared-worker accounts, path restriction, and restrict-only are the operator's choice, never a preset's."""
    service = EgressService.model_validate({"preset": "github"})

    assert getattr(service, field) is False
    assert all(field not in preset for preset in EGRESS_PRESETS.values())


@pytest.mark.parametrize("field", ["oauth_on_shared_workers", "restrict_to_rules"])
def test_opt_in_field_survives_the_authored_dump(field: str) -> None:
    """The opt-in is an authored field, so saving config keeps it next to the preset reference."""
    config = Config(egress_broker={"services": {"github": {"preset": "github", field: True}}})

    dumped = config.authored_model_dump()["egress_broker"]

    assert dumped == {"services": {"github": {"preset": "github", field: True}}}
    assert getattr(Config(**config.authored_model_dump()).egress_broker.services["github"], field) is True


def test_restrict_only_needs_no_auth_on_its_rules() -> None:
    """A restrict-only service has no credential, so its rules need no auth; how it limits paths is in test_rules."""
    service = EgressService.model_validate(
        {"restrict_only": True, "rules": [{"host": "api.github.com", "path_prefix": "/repos/o/r"}]},
    )

    assert service.restrict_to_rules is False
    assert service.rules[0].auth is None
    assert service.authored_model_dump() == {
        "restrict_only": True,
        "rules": [{"host": "api.github.com", "path_prefix": "/repos/o/r"}],
    }
    # Setting both, as the limit editor does, is the same service.
    both = EgressService.model_validate(
        {"restrict_only": True, "restrict_to_rules": True, "rules": [{"host": "api.github.com"}]},
    )
    assert (both.restrict_only, both.restrict_to_rules) == (True, True)


def test_a_service_that_sends_a_credential_needs_auth_on_every_rule() -> None:
    """Only a restrict-only service may leave out a rule's auth: any other one would not know how to send its key."""
    with pytest.raises(ValidationError, match="every rule needs auth unless the service is restrict_only"):
        EgressService.model_validate(
            {"rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}, {"host": "uploads.example.com"}]},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oauth_provider", "github"),
        ("placeholder_env", {"GH_TOKEN": "mindroom-brokered"}),
        ("oauth_on_shared_workers", True),
    ],
)
def test_restrict_only_refuses_credential_settings(field: str, value: object) -> None:
    """A restrict-only service never sends a credential, so a login, placeholders, or the shared opt-in are refused."""
    with pytest.raises(
        ValidationError,
        match=f"a restrict_only service never sends a credential, so it cannot set {field}",
    ):
        EgressService.model_validate(
            {"restrict_only": True, "rules": [{"host": "api.github.com", "auth": {"type": "bearer"}}], field: value},
        )


def test_restrict_only_keeps_a_presets_rules_and_names_but_not_its_login_or_placeholders() -> None:
    """With a preset, a restrict-only service takes its hosts and names; the login and placeholders stay with the preset."""
    service = EgressService.model_validate(
        {
            "preset": "github",
            "restrict_only": True,
            "rules": [{"host": "api.github.com", "path_prefix": "/repos/o/r", "auth": {"type": "bearer"}}],
        },
    )

    assert service.display_name == "GitHub"
    assert service.oauth_provider is None
    assert service.placeholder_env == {}
    assert service.authored_model_dump() == {
        "preset": "github",
        "restrict_only": True,
        "rules": [{"host": "api.github.com", "path_prefix": "/repos/o/r", "auth": {"type": "bearer"}}],
    }
    # An authored login is still refused: only the preset's own is dropped.
    with pytest.raises(ValidationError, match="cannot set oauth_provider"):
        EgressService.model_validate({"preset": "github", "restrict_only": True, "oauth_provider": "github"})


def test_a_config_service_cannot_be_restrict_only() -> None:
    """Only a service a user adds may be a limit; an administrator limits paths with restrict_to_rules instead."""
    limit = {"restrict_only": True, "rules": [{"host": "api.github.com", "path_prefix": "/repos/o/r"}]}
    message = "service 'github-limited': restrict_only is only for services users add; use restrict_to_rules in config"

    with pytest.raises(ValidationError, match=message):
        Config(egress_broker={"services": {"github": {"preset": "github"}, "github-limited": limit}})
    with pytest.raises(ValidationError, match=message):
        EgressBrokerConfig.model_validate({"services": {"github-limited": limit}})
    # The same service is valid on its own, as a user service is validated, and so is `restrict_only: false`.
    assert EgressService.model_validate(limit).restrict_only is True
    unlimited = {"services": {"github": {"preset": "github", "restrict_only": False}}}
    assert EgressBrokerConfig.model_validate(unlimited).services["github"].restrict_only is False


@pytest.mark.parametrize("value", ["true", "yes", 1, "false"])
def test_restrict_only_must_be_a_real_boolean(value: object) -> None:
    """A string or number is refused, so `restrict_only: "true"` never quietly turns a service into a limit or not."""
    with pytest.raises(ValidationError, match="restrict_only\n  Input should be a valid boolean"):
        EgressService.model_validate({"restrict_only": value, "rules": [{"host": "api.github.com"}]})


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "basic"},  # Missing username
        {"type": "header"},  # Missing name
        {"type": "header", "name": "X-Key", "template": "none"},  # Missing {secret}
    ],
)
def test_invalid_auth_rejected(bad: dict) -> None:
    """Invalid auth configurations are rejected."""
    with pytest.raises(ValueError, match=r"(requires|must contain)"):
        EgressAuth(**bad)


@pytest.mark.parametrize(
    "host",
    [
        "*.*.example.com",  # Multiple wildcards
        "https://x.com",  # Has scheme
        "x.com:443",  # Has port
        "x.com/path",  # Has path
    ],
)
def test_invalid_host_rejected(host: str) -> None:
    """Invalid host values are rejected."""
    with pytest.raises(ValueError, match=r"(host|wildcard)"):
        EgressRule(host=host, auth=EgressAuth(type="bearer"))


@pytest.mark.parametrize(
    "name",
    [
        "HTTPS_PROXY",
        "no_proxy",
        "MINDROOM_X",
        "SSL_CERT_FILE",
        "PATH",
        "ALL_PROXY",
        "all_proxy",
        "NODE_USE_ENV_PROXY",
        "lower",  # Must start with uppercase
        "GH_TOKEN\n",  # The whole name must match, not just up to a trailing newline
    ],
)
def test_invalid_placeholder_env_rejected(name: str) -> None:
    """Invalid placeholder env names are rejected."""
    with pytest.raises(ValueError, match=r"(placeholder_env|match|reserved|prefix)"):
        EgressService(
            rules=[EgressRule(host="example.com", auth=EgressAuth(type="bearer"))],
            placeholder_env={name: "value"},
        )


_EXAMPLE_SERVICE = EgressService(rules=[EgressRule(host="example.com", auth=EgressAuth(type="bearer"))])


@pytest.mark.parametrize("name", ["GitHub", "a b"], ids=["uppercase", "space"])
def test_invalid_service_name_rejected(name: str) -> None:
    """Invalid service names are rejected."""
    with pytest.raises(ValueError, match=r"service name.*must match"):
        EgressBrokerConfig(services={name: _EXAMPLE_SERVICE})


@pytest.mark.parametrize("name", ["x_oauth", "x_oauth_client", "oauth", "oauth_client"])
def test_service_name_with_oauth_suffix_rejected(name: str) -> None:
    """Names whose `egress_<name>` credential service ends like an OAuth service are rejected."""
    with pytest.raises(ValueError, match="OAuth"):
        EgressBrokerConfig(services={name: _EXAMPLE_SERVICE})


@pytest.mark.parametrize("name", ["oauth_proxy", "my-oauth", "xoauth"])
def test_service_name_mentioning_oauth_elsewhere_accepted(name: str) -> None:
    """Only the OAuth suffixes are reserved, not the word itself."""
    assert name in EgressBrokerConfig(services={name: _EXAMPLE_SERVICE}).services


@pytest.mark.parametrize("name", ["", "X Key", "X-Key:", "X-Key\n", "X-Key\r\nInjected: 1", "X/Key", "Clé"])
def test_header_auth_name_must_be_token(name: str) -> None:
    """A header auth name must be a valid HTTP header field name."""
    with pytest.raises(ValueError, match="header name"):
        EgressAuth(type="header", name=name)


@pytest.mark.parametrize(
    "name",
    [
        "Host",
        "content-length",
        "Transfer-Encoding",
        "connection",
        "Upgrade",
        "TE",
        "trailer",
        "Proxy-Authorization",
        "proxy-connection",
        "Keep-Alive",
    ],
)
def test_header_auth_name_rejects_framing_and_routing_headers(name: str) -> None:
    """The broker owns framing, routing, and proxy headers, so no rule may inject into them."""
    with pytest.raises(ValueError, match="reserved"):
        EgressAuth(type="header", name=name)


@pytest.mark.parametrize("name", ["Authorization", "X-Api-Key", "x-goog-api-key", "Private-Token"])
def test_header_auth_name_accepts_common_headers(name: str) -> None:
    """Ordinary credential headers remain allowed."""
    assert EgressAuth(type="header", name=name).name == name


def test_query_auth_name_must_not_be_empty() -> None:
    """A query auth name must name a parameter."""
    with pytest.raises(ValueError, match="query auth requires a non-empty name"):
        EgressAuth(type="query", name="")


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_rule_port_out_of_range_rejected(port: int) -> None:
    """Rule ports must be valid TCP ports."""
    with pytest.raises(ValueError, match="port"):
        EgressRule(host="example.com", port=port, auth=EgressAuth(type="bearer"))


@pytest.mark.parametrize("port", [1, 443, 65535])
def test_rule_port_in_range_accepted(port: int) -> None:
    """Valid TCP ports are accepted."""
    assert EgressRule(host="example.com", port=port, auth=EgressAuth(type="bearer")).port == port


def test_host_strips_trailing_dot() -> None:
    """Host with trailing dot (FQDN notation) should be normalized."""
    rule = EgressRule(host="api.example.com.", auth=EgressAuth(type="bearer"))
    assert rule.host == "api.example.com"

    # A config loaded from YAML is normalized the same way.
    config_yaml = """
    services:
      test:
        rules:
          - host: api.github.com.
            auth: { type: bearer }
    """
    config = EgressBrokerConfig(**yaml.safe_load(config_yaml))
    assert config.services["test"].rules[0].host == "api.github.com"
