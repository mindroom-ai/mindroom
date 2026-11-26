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


def test_hmac_event_source_config_defaults_and_custom_fields() -> None:
    """HmacEventSourceConfig validates defaults and custom header/field/prefix options."""
    config = Config.model_validate(
        {
            **_base_config(),
            "external_trigger_policy": {
                "event_sources": {
                    "default_hmac": {
                        "kind": "hmac",
                        "secret_env": "WEBHOOK_SECRET",
                    },
                    "linear": {
                        "kind": "hmac",
                        "secret_env": "LINEAR_SECRET_1",
                        "signature_header": "Linear-Signature",
                        "signature_prefix": "",
                        "kind_header": "Linear-Event",
                        "kind_field": "meta.event_type",
                        "event_id_header": "Linear-Delivery",
                        "allowed_kinds": ["Issue", "Comment"],
                        "subscribers": ["@alice:example.org"],
                        "max_body_bytes": 131072,
                    },
                },
            },
        },
    )

    default_src = config.external_trigger_policy.event_sources["default_hmac"]
    assert default_src.kind == "hmac"
    assert default_src.signature_header == "X-Webhook-Signature-256"
    assert default_src.signature_prefix == "sha256="
    assert default_src.kind_header is None
    assert default_src.kind_field == "kind"
    assert default_src.event_id_header is None
    assert default_src.allowed_kinds == []
    assert default_src.max_body_bytes == 262144

    linear_src = config.external_trigger_policy.event_sources["linear"]
    assert linear_src.kind == "hmac"
    assert linear_src.signature_header == "Linear-Signature"
    assert linear_src.signature_prefix == ""
    assert linear_src.kind_header == "Linear-Event"
    assert linear_src.kind_field == "meta.event_type"
    assert linear_src.event_id_header == "Linear-Delivery"
    assert linear_src.allowed_kinds == ["Issue", "Comment"]


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"secret_env": "not-valid-env"}, "secret_env"),
        ({"signature_header": "Bad Header"}, "signature_header"),
        ({"signature_header": "Bad:Header"}, "signature_header"),
        ({"signature_header": ""}, "signature_header"),
        ({"kind_header": "Bad Header"}, "kind_header"),
        ({"event_id_header": "Bad:Id"}, "event_id_header"),
        ({"kind_field": ""}, "kind_field"),
        ({"kind_field": "event..type"}, "kind_field"),
        ({"allowed_kinds": ["  "]}, "allowed_kinds"),
        ({"subscribers": ["invalid-user"]}, "Invalid Matrix user ID"),
        ({"extra_field": "nope"}, "extra_field"),
    ],
)
def test_hmac_event_source_config_rejects_invalid_fields(overrides: dict[str, object], match: str) -> None:
    """HmacEventSourceConfig rejects malformed env vars, headers, kind paths, and unknown fields."""
    source_cfg: dict[str, object] = {
        "kind": "hmac",
        "secret_env": "HMAC_SECRET",
        **overrides,
    }
    with pytest.raises(ValueError, match=match):
        Config.model_validate(
            {
                **_base_config(),
                "external_trigger_policy": {
                    "event_sources": {"custom": source_cfg},
                },
            },
        )


def test_external_trigger_policy_github_pr_fields_defaults_and_bounds() -> None:
    """ExternalTriggerPolicyConfig exposes validated github_pr polling policy defaults and bounds."""
    default_cfg = Config.model_validate(_base_config())
    pol = default_cfg.external_trigger_policy
    assert pol.github_pr_polling_enabled is True
    assert pol.min_github_pr_poll_interval_seconds == 60
    assert pol.max_github_pr_watches_per_owner == 20
    gh_cfg = Config.model_validate(
        {
            **_base_config(),
            "external_trigger_policy": {
                "event_sources": {
                    "gh": {"kind": "github", "secret_env": "GH_SECRET", "allowed_repos": ["org/repo"]},
                },
            },
        },
    ).external_trigger_policy.event_sources["gh"]
    assert gh_cfg.kind == "github"
    assert {"issue_comment", "pull_request_review_comment", "check_run"} <= set(gh_cfg.allowed_events)

    with pytest.raises(ValueError, match="min_github_pr_poll_interval_seconds"):
        Config.model_validate(
            {**_base_config(), "external_trigger_policy": {"min_github_pr_poll_interval_seconds": 10}},
        )
