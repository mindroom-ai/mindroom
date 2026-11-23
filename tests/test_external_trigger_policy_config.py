"""Tests for external trigger policy configuration."""

from __future__ import annotations

import pytest
import yaml

from mindroom.config.main import Config


def _base_config() -> dict[str, object]:
    return {
        "models": {
            "default": {
                "provider": "openai",
                "id": "gpt-6-astra",
            },
        },
        "router": {"model": "default"},
    }


def test_external_trigger_policy_defaults() -> None:
    """External trigger policy defaults are enabled and capped."""
    config = Config.model_validate(_base_config())

    policy = config.external_trigger_policy

    assert policy.enabled is True
    assert policy.default_replay_window_seconds == 300
    assert policy.max_replay_window_seconds == 3600
    assert policy.default_max_body_bytes == 65536
    assert policy.max_body_bytes == 262144
    assert policy.max_triggers_per_owner == 20
    assert policy.admin_users == []


@pytest.mark.parametrize("value", [{"campground": {}}, {}, None])
def test_external_triggers_config_key_is_rejected(value: object) -> None:
    """Old authored per-trigger config is rejected."""
    with pytest.raises(ValueError, match="external_triggers"):
        Config.model_validate({**_base_config(), "external_triggers": value})


def test_external_trigger_policy_dump_is_plain_yaml_safe() -> None:
    """Policy authored dumps are YAML-safe data."""
    config = Config.model_validate(
        {
            **_base_config(),
            "external_trigger_policy": {
                "enabled": True,
                "admin_users": ["@admin:example.org"],
                "default_replay_window_seconds": 120,
                "max_replay_window_seconds": 600,
            },
        },
    )

    yaml_text = yaml.dump(config.authored_model_dump())
    loaded = yaml.safe_load(yaml_text)

    assert loaded["external_trigger_policy"]["admin_users"] == ["@admin:example.org"]
    assert "!!python" not in yaml_text


def test_private_agent_targets_are_not_rejected_by_config() -> None:
    """Config no longer validates external trigger targets, including private agents."""
    config = Config.model_validate(
        {
            **_base_config(),
            "agents": {
                "private_agent": {
                    "display_name": "Private",
                    "model": "default",
                    "private": {"per": "user"},
                    "rooms": ["lobby"],
                },
            },
            "rooms": {"lobby": {"display_name": "Lobby"}},
        },
    )

    assert "private_agent" in config.agents
    assert config.get_all_configured_rooms() == {"lobby"}


def test_external_trigger_policy_rejects_defaults_above_caps() -> None:
    """Policy defaults must fit inside policy caps."""
    with pytest.raises(ValueError, match="default_replay_window_seconds"):
        Config.model_validate(
            {
                **_base_config(),
                "external_trigger_policy": {
                    "default_replay_window_seconds": 600,
                    "max_replay_window_seconds": 300,
                },
            },
        )


@pytest.mark.parametrize(
    "subscriber",
    [
        "@alice:example.org",
        "@alice:matrix.example.org:8448",
        "@alice:127.0.0.1:8448",
        "@alice:[::1]:8448",
        "@alice:[2001:db8::1]",
    ],
)
def test_event_source_subscribers_accept_valid_matrix_server_names(subscriber: str) -> None:
    """Event source subscribers accept valid Matrix user IDs including ports and bracketed IPv6."""
    config = Config.model_validate(
        {
            **_base_config(),
            "external_trigger_policy": {
                "event_sources": {
                    "gh": {
                        "kind": "github",
                        "secret_env": "GH_SECRET",
                        "allowed_repos": ["mindroom-ai/mindroom"],
                        "subscribers": [subscriber],
                    },
                },
            },
        },
    )
    assert config.external_trigger_policy.event_sources["gh"].subscribers == [subscriber]


@pytest.mark.parametrize(
    "subscriber",
    [
        "alice:example.org",
        "@:example.org",
        "@alice:",
        "@alice:bad host",
        "@alice:example.org:0",
        "@alice:example.org:99999",
        "@ali ce:example.org",
        "@alice:[::1",
        "@alice:[::1]:",
    ],
)
def test_event_source_subscribers_reject_invalid_matrix_user_ids(subscriber: str) -> None:
    """Event source subscribers reject malformed Matrix user IDs and invalid server names."""
    with pytest.raises(ValueError, match="Invalid Matrix user ID"):
        Config.model_validate(
            {
                **_base_config(),
                "external_trigger_policy": {
                    "event_sources": {
                        "gh": {
                            "kind": "github",
                            "secret_env": "GH_SECRET",
                            "allowed_repos": ["mindroom-ai/mindroom"],
                            "subscribers": [subscriber],
                        },
                    },
                },
            },
        )
