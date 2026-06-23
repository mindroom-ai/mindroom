"""Tests for external trigger configuration."""

from __future__ import annotations

import pytest
import yaml

from mindroom.config.main import Config


def _base_config() -> dict[str, object]:
    return {
        "models": {
            "default": {
                "provider": "openai",
                "id": "gpt-5.5",
            },
        },
        "router": {
            "model": "default",
        },
    }


def test_external_trigger_config_parses_minimal_signed_trigger() -> None:
    """Minimal signed trigger config parses with defaults."""
    config = Config.model_validate(
        {
            **_base_config(),
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "model": "default",
                },
            },
            "external_triggers": {
                "campground": {
                    "description": "Campground availability webhook",
                    "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                    "target": {
                        "room_id": "!room:example.org",
                        "thread_id": "$thread",
                        "agent": "mind",
                    },
                    "allowed_kinds": ["campground.availability"],
                },
            },
        },
    )

    trigger = config.external_triggers["campground"]

    assert trigger.enabled is True
    assert trigger.description == "Campground availability webhook"
    assert trigger.auth == "ed25519"
    assert trigger.key_id == "default"
    assert trigger.public_key == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    assert trigger.target.room_id == "!room:example.org"
    assert trigger.target.thread_id == "$thread"
    assert trigger.target.agent == "mind"
    assert trigger.target.new_thread is False
    assert trigger.allowed_kinds == ["campground.availability"]
    assert trigger.replay_window_seconds == 300
    assert trigger.max_body_bytes == 65536
    assert config.get_all_configured_rooms() == {"!room:example.org"}
    assert config.get_external_trigger_rooms_for_entity("mind") == ["!room:example.org"]


def test_external_trigger_authored_dump_is_safe_yaml_round_trippable() -> None:
    """Authored external trigger config dumps should not emit Python-specific YAML tags."""
    config = Config.model_validate(
        {
            **_base_config(),
            "agents": {
                "mind": {
                    "display_name": "Mind",
                    "model": "default",
                },
            },
            "external_triggers": {
                "campground": {
                    "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                    "target": {
                        "room_id": "!room:example.org",
                        "agent": "mind",
                    },
                    "allowed_kinds": ["campground.availability"],
                },
            },
        },
    )

    yaml_text = yaml.dump(config.authored_model_dump())

    assert yaml.safe_load(yaml_text)["external_triggers"]["campground"]["allowed_kinds"] == [
        "campground.availability",
    ]


def test_external_triggers_null_section_uses_empty_mapping() -> None:
    """An uncommented blank external_triggers section should behave like omission."""
    config = Config.model_validate(
        {
            **_base_config(),
            "external_triggers": None,
        },
    )

    assert config.external_triggers == {}
    assert "external_triggers" not in config.authored_model_dump()


def test_external_trigger_rejects_empty_public_key() -> None:
    """External trigger public keys must not be empty."""
    config_data = {
        **_base_config(),
        "agents": {
            "mind": {
                "display_name": "Mind",
                "model": "default",
            },
        },
        "external_triggers": {
            "campground": {
                "public_key": "",
                "target": {
                    "room_id": "!room:example.org",
                    "agent": "mind",
                },
            },
        },
    }

    with pytest.raises(ValueError, match="public_key") as exc_info:
        Config.model_validate(config_data)

    assert "public_key" in str(exc_info.value)


def test_external_trigger_rejects_empty_key_id() -> None:
    """External trigger key IDs must not be empty."""
    config_data = {
        **_base_config(),
        "agents": {
            "mind": {
                "display_name": "Mind",
                "model": "default",
            },
        },
        "external_triggers": {
            "campground": {
                "key_id": "",
                "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "target": {
                    "room_id": "!room:example.org",
                    "agent": "mind",
                },
            },
        },
    }

    with pytest.raises(ValueError, match="key_id") as exc_info:
        Config.model_validate(config_data)

    assert "key_id" in str(exc_info.value)


@pytest.mark.parametrize("thread_id", ["", "   "])
def test_external_trigger_rejects_empty_thread_id(thread_id: str) -> None:
    """Blank configured thread IDs should fail before delivery builds Matrix relations."""
    config_data = {
        **_base_config(),
        "agents": {
            "mind": {
                "display_name": "Mind",
                "model": "default",
            },
        },
        "external_triggers": {
            "campground": {
                "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "target": {
                    "room_id": "!room:example.org",
                    "thread_id": thread_id,
                    "agent": "mind",
                },
            },
        },
    }

    with pytest.raises(ValueError, match="thread_id") as exc_info:
        Config.model_validate(config_data)

    assert "thread_id" in str(exc_info.value)


def test_external_trigger_requires_configured_agent_or_team_target() -> None:
    """External trigger targets must reference configured agents or teams."""
    config_data = {
        **_base_config(),
        "external_triggers": {
            "campground": {
                "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "target": {
                    "room_id": "!room:example.org",
                    "agent": "missing",
                },
            },
        },
    }

    with pytest.raises(ValueError, match=r"external_triggers\.campground\.target\.agent") as exc_info:
        Config.model_validate(config_data)

    assert "external_triggers.campground.target.agent" in str(exc_info.value)


@pytest.mark.parametrize(
    "public_key",
    [
        "not-base64",
        "c2hvcnQ=",
    ],
)
def test_external_trigger_rejects_invalid_public_key_material(public_key: str) -> None:
    """External trigger public keys must be valid raw Ed25519 public keys."""
    config_data = {
        **_base_config(),
        "agents": {
            "mind": {
                "display_name": "Mind",
                "model": "default",
            },
        },
        "external_triggers": {
            "campground": {
                "public_key": public_key,
                "target": {
                    "room_id": "!room:example.org",
                    "agent": "mind",
                },
            },
        },
    }

    with pytest.raises(ValueError, match="public_key"):
        Config.model_validate(config_data)


def test_external_trigger_rejects_path_unsafe_trigger_id() -> None:
    """External trigger IDs must be usable as a single API path segment."""
    config_data = {
        **_base_config(),
        "agents": {
            "mind": {
                "display_name": "Mind",
                "model": "default",
            },
        },
        "external_triggers": {
            "campground/openings": {
                "public_key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "target": {
                    "room_id": "!room:example.org",
                    "agent": "mind",
                },
            },
        },
    }

    with pytest.raises(ValueError, match="External trigger IDs"):
        Config.model_validate(config_data)
