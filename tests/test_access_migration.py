"""Access-schema migration behavior."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_access_migration_leaves_current_schema_unchanged() -> None:
    """A current config must not trigger a rewrite."""
    from mindroom.config.access_migration import migrate_access_config_data  # noqa: PLC0415

    data = {
        "administrators": ["@owner:example.com"],
        "agents": {
            "talent": {
                "display_name": "Talent",
                "access": {"current_room_members": True},
            },
        },
    }

    result = migrate_access_config_data(data)

    assert result.changed is False
    assert result.data == data


def test_access_migration_splits_legacy_owner_permissions() -> None:
    """One legacy owner must become four explicit capabilities."""
    from mindroom.config.access_migration import migrate_access_config_data  # noqa: PLC0415

    result = migrate_access_config_data(
        {
            "agents": {
                "talent": {
                    "display_name": "Talent",
                    "rooms": ["talent"],
                },
            },
            "authorization": {
                "global_users": ["@owner:example.com"],
                "config_command_enabled": True,
                "aliases": {"@owner:example.com": ["@bridge-owner:example.com"]},
                "agent_reply_permissions": {
                    "talent": ["@owner:example.com"],
                },
            },
        },
    )

    assert result.changed is True
    assert result.data["administrators"] == ["@owner:example.com"]
    assert result.data["room_defaults"]["invite_users"] == ["@owner:example.com"]
    assert result.data["agents"]["talent"]["access"] == {
        "current_room_members": False,
        "members_of_rooms": [],
        "users": ["@owner:example.com"],
    }
    assert result.data["agents"]["talent"]["credential_managers"] == ["@owner:example.com"]
    assert result.data["authorization"] == {
        "config_command_enabled": True,
        "aliases": {"@owner:example.com": ["@bridge-owner:example.com"]},
    }


def test_access_migration_removes_mode_marker_and_is_idempotent() -> None:
    """A migrated config must not retain a mode switch or change again."""
    from mindroom.config.access_migration import migrate_access_config_data  # noqa: PLC0415

    first = migrate_access_config_data(
        {
            "access_model": "room_membership",
            "administrators": ["@owner:example.com"],
            "room_defaults": {"join_policy": "invite"},
            "agents": {"talent": {"display_name": "Talent", "rooms": ["talent"]}},
        },
    )
    second = migrate_access_config_data(first.data)

    assert first.changed is True
    assert "access_model" not in first.data
    assert second.changed is False
    assert second.data == first.data


def test_access_migration_materializes_wildcard_responder_policy() -> None:
    """A wildcard policy must become explicit access on every responder it covers."""
    from mindroom.config.access_migration import migrate_access_config_data  # noqa: PLC0415

    result = migrate_access_config_data(
        {
            "agents": {
                "talent": {"display_name": "Talent", "rooms": ["talent"]},
                "support": {"display_name": "Support", "rooms": ["support"]},
            },
            "teams": {
                "editorial": {"display_name": "Editorial", "agents": ["talent"]},
            },
            "router": {},
            "authorization": {
                "agent_reply_permissions": {
                    "*": {
                        "users": ["@shared:example.com", "@partner_*:example.com"],
                        "joined_rooms": ["core"],
                    },
                    "talent": ["@talent-owner:example.com"],
                },
            },
        },
    )

    wildcard_access = {
        "current_room_members": False,
        "members_of_rooms": ["core"],
        "users": ["@shared:example.com", "@partner_*:example.com"],
    }
    assert result.data["agents"]["talent"]["access"]["users"] == ["@talent-owner:example.com"]
    assert result.data["agents"]["support"]["access"] == wildcard_access
    assert result.data["teams"]["editorial"]["access"] == wildcard_access
    assert result.data["router"]["access"] == wildcard_access
    assert result.data["agents"]["support"]["credential_managers"] == ["@shared:example.com"]
    assert "credential_managers" not in result.data["teams"]["editorial"]


def test_access_migration_maps_room_permissions_and_matrix_defaults() -> None:
    """Retired room controls must become explicit defaults and room overrides."""
    from mindroom.config.access_migration import migrate_access_config_data  # noqa: PLC0415

    result = migrate_access_config_data(
        {
            "agents": {
                "talent": {"display_name": "Talent", "rooms": ["talent", "private"]},
            },
            "rooms": {
                "talent": {"invite_users": ["@existing:example.com"]},
                "private": {"description": "Private room"},
            },
            "room_defaults": {"admins": ["@existing-admin:example.com"]},
            "authorization": {
                "room_permissions": {
                    "talent": ["@talent-owner:example.com"],
                },
            },
            "matrix_room_access": {
                "mode": "multi_user",
                "multi_user_join_rule": "knock",
                "publish_to_room_directory": True,
                "invite_only_rooms": ["private"],
                "encrypt_managed_rooms": True,
                "room_admins": ["@room-admin:example.com"],
            },
        },
    )

    assert result.data["room_defaults"] == {
        "join_policy": "knock",
        "listed": True,
        "encrypted": True,
        "admins": ["@existing-admin:example.com", "@room-admin:example.com"],
    }
    assert result.data["rooms"]["talent"]["invite_users"] == [
        "@existing:example.com",
        "@talent-owner:example.com",
    ]
    assert result.data["rooms"]["private"] == {
        "description": "Private room",
        "join_policy": "invite",
        "listed": False,
    }
    assert "matrix_room_access" not in result.data
    assert "room_permissions" not in result.data["authorization"]


def test_access_migration_preserves_explicit_new_schema_grants() -> None:
    """Migration must combine list grants without replacing explicit new values."""
    from mindroom.config.access_migration import migrate_access_config_data  # noqa: PLC0415

    result = migrate_access_config_data(
        {
            "agents": {
                "talent": {
                    "display_name": "Talent",
                    "access": {
                        "current_room_members": True,
                        "members_of_rooms": ["new-core"],
                        "users": ["@new:example.com"],
                    },
                    "credential_managers": ["@new-manager:example.com"],
                },
            },
            "authorization": {
                "agent_reply_permissions": {
                    "talent": {
                        "users": ["@old:example.com", "@partner_*:example.com"],
                        "joined_rooms": ["old-core"],
                    },
                },
            },
        },
    )

    assert result.data["agents"]["talent"]["access"] == {
        "current_room_members": True,
        "members_of_rooms": ["new-core", "old-core"],
        "users": ["@new:example.com", "@old:example.com", "@partner_*:example.com"],
    }
    assert result.data["agents"]["talent"]["credential_managers"] == [
        "@new-manager:example.com",
        "@old:example.com",
    ]


def test_access_migration_maps_default_room_access_to_current_membership() -> None:
    """Broad retired room access must become explicit current-room access."""
    from mindroom.config.access_migration import migrate_access_config_data  # noqa: PLC0415

    result = migrate_access_config_data(
        {
            "agents": {"talent": {"display_name": "Talent"}},
            "teams": {"editorial": {"display_name": "Editorial", "agents": ["talent"]}},
            "router": {},
            "authorization": {"default_room_access": True},
        },
    )

    expected_access = {
        "current_room_members": True,
        "members_of_rooms": [],
        "users": [],
    }
    assert result.data["agents"]["talent"]["access"] == expected_access
    assert result.data["teams"]["editorial"]["access"] == expected_access
    assert result.data["router"]["access"] == expected_access


def test_access_migration_rejects_unknown_room_permission_key() -> None:
    """A room-scoped grant must never migrate onto an unmanaged room key."""
    from mindroom.config.access_migration import AccessMigrationError, migrate_access_config_data  # noqa: PLC0415

    with pytest.raises(AccessMigrationError, match="unknown managed room key"):
        migrate_access_config_data(
            {
                "agents": {"talent": {"display_name": "Talent", "rooms": ["talent"]}},
                "authorization": {
                    "room_permissions": {"unknown": ["@owner:example.com"]},
                },
            },
        )


def test_access_migration_rejects_unknown_reply_policy_entity() -> None:
    """A retired responder policy must not disappear when its entity is unknown."""
    from mindroom.config.access_migration import AccessMigrationError, migrate_access_config_data  # noqa: PLC0415

    with pytest.raises(AccessMigrationError, match="contains unknown entities: missing"):
        migrate_access_config_data(
            {
                "agents": {"talent": {"display_name": "Talent"}},
                "authorization": {
                    "agent_reply_permissions": {"missing": ["@owner:example.com"]},
                },
            },
        )


def test_access_migration_resolves_unambiguous_room_alias() -> None:
    """A full alias whose localpart is a managed key must migrate to that key."""
    from mindroom.config.access_migration import migrate_access_config_data  # noqa: PLC0415

    result = migrate_access_config_data(
        {
            "agents": {"talent": {"display_name": "Talent", "rooms": ["talent"]}},
            "authorization": {
                "room_permissions": {"#talent:example.com": ["@owner:example.com"]},
            },
            "matrix_room_access": {
                "invite_only_rooms": ["#talent:example.com"],
            },
        },
    )

    assert result.data["rooms"]["talent"] == {
        "invite_users": ["@owner:example.com"],
        "join_policy": "invite",
        "listed": False,
    }


def test_access_migration_maps_matrix_access_without_authorization() -> None:
    """Retired Matrix room settings must migrate without an authorization section."""
    from mindroom.config.access_migration import migrate_access_config_data  # noqa: PLC0415

    result = migrate_access_config_data(
        {
            "matrix_room_access": {
                "mode": "single_user_private",
                "encrypt_managed_rooms": True,
            },
        },
    )

    assert result.data == {
        "room_defaults": {
            "join_policy": "invite",
            "listed": False,
            "encrypted": True,
            "admins": [],
        },
    }


def test_load_config_migrates_single_file_after_validation(tmp_path: Path) -> None:
    """A valid monolithic legacy config must be backed up and atomically rewritten."""
    from mindroom import yaml_io  # noqa: PLC0415
    from mindroom.config.main import load_config  # noqa: PLC0415
    from mindroom.constants import resolve_runtime_paths  # noqa: PLC0415

    config_path = Path(tmp_path) / "config.yaml"
    original = "authorization:\n  global_users:\n    - '@owner:example.com'\n"
    config_path.write_text(original, encoding="utf-8")

    config = load_config(resolve_runtime_paths(config_path=config_path))

    migrated = yaml_io.safe_load(config_path.read_text(encoding="utf-8"))
    assert migrated["administrators"] == ["@owner:example.com"]
    assert migrated["room_defaults"]["invite_users"] == ["@owner:example.com"]
    assert "global_users" not in migrated["authorization"]
    assert config.administrators == ["@owner:example.com"]
    backup_path = config_path.with_name(f"{config_path.name}.pre-membership-access")
    assert backup_path.read_text(encoding="utf-8") == original


def test_load_config_rejects_access_migration_with_include(tmp_path: Path) -> None:
    """A composed legacy config must fail before validation or persistence."""
    from mindroom.config.access_migration import AccessMigrationError  # noqa: PLC0415
    from mindroom.config.main import load_config  # noqa: PLC0415
    from mindroom.constants import resolve_runtime_paths  # noqa: PLC0415

    config_path = Path(tmp_path) / "config.yaml"
    authorization_path = Path(tmp_path) / "authorization.yaml"
    config_text = "authorization: !include authorization.yaml\n"
    authorization_text = "global_users:\n  - '@owner:example.com'\n"
    config_path.write_text(config_text, encoding="utf-8")
    authorization_path.write_text(authorization_text, encoding="utf-8")

    with pytest.raises(AccessMigrationError, match="does not support !include"):
        load_config(resolve_runtime_paths(config_path=config_path))

    assert config_path.read_text(encoding="utf-8") == config_text
    assert authorization_path.read_text(encoding="utf-8") == authorization_text
    assert not config_path.with_name(f"{config_path.name}.pre-membership-access").exists()


def test_load_config_does_not_persist_invalid_migration(tmp_path: Path) -> None:
    """Validation failure must leave the original file untouched and create no backup."""
    from pydantic import ValidationError  # noqa: PLC0415

    from mindroom.config.main import load_config  # noqa: PLC0415
    from mindroom.constants import resolve_runtime_paths  # noqa: PLC0415

    config_path = Path(tmp_path) / "config.yaml"
    original = "authorization:\n  global_users:\n    - not-a-matrix-user\n"
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(resolve_runtime_paths(config_path=config_path))

    assert config_path.read_text(encoding="utf-8") == original
    assert not config_path.with_name(f"{config_path.name}.pre-membership-access").exists()
