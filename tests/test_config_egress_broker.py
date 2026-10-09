"""Tests for egress broker configuration integration with main Config."""

from __future__ import annotations

import yaml

from mindroom.config.egress_broker import EgressBrokerConfig
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
