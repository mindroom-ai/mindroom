"""Tests for shipped Matrix authorities and the foreign-homeserver startup warning."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.constants import OWNER_MATRIX_USER_ID_PLACEHOLDER, RuntimePaths, resolve_runtime_paths
from mindroom.matrix_identifiers import split_concrete_matrix_user_ids
from mindroom.orchestration.rooms import warn_about_foreign_homeserver_authorities

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WARNING = "Administrators, room invitees, or room admins are on another homeserver; remove them unless you trust them"


def _runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.com", **env},
    )


def _foreign_warnings(config: Config, runtime_paths: RuntimePaths) -> list[dict[str, object]]:
    with capture_logs() as logs:
        warn_about_foreign_homeserver_authorities(config, runtime_paths)
    return [log for log in logs if log["event"] == _WARNING]


@pytest.mark.parametrize("path", ["config.yaml", "cluster/k8s/instance/default-config.yaml"])
def test_shipped_configs_grant_no_concrete_matrix_users(path: str) -> None:
    """Shipped seed configs must only reference the inert owner placeholder."""
    config = yaml.safe_load((_REPO_ROOT / path).read_text(encoding="utf-8"))
    room_policies = [config.get("room_defaults", {}), *config.get("rooms", {}).values()]
    user_ids = [
        *config.get("administrators", []),
        *(user_id for policy in room_policies for user_id in policy.get("invite_users") or []),
        *(user_id for policy in room_policies for user_id in policy.get("admins") or []),
    ]

    assert split_concrete_matrix_user_ids(user_ids)[0] == []
    assert config["administrators"] == [OWNER_MATRIX_USER_ID_PLACEHOLDER]


def test_warns_about_foreign_administrators_invitees_and_room_admins(tmp_path: Path) -> None:
    """Every authority on another homeserver is named; local users and placeholders are not."""
    config = Config.model_validate(
        {
            "administrators": ["@test:m-test-4.mindroom.chat", "@owner:matrix.example.com"],
            "room_defaults": {
                "invite_users": ["@guest:other.example", OWNER_MATRIX_USER_ID_PLACEHOLDER],
                "admins": ["@owner:Matrix.Example.com"],
            },
            "rooms": {"project": {"admins": ["@mod:third.example"]}},
        },
    )

    warnings = _foreign_warnings(config, _runtime_paths(tmp_path))

    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["server_name"] == "matrix.example.com"
    assert warnings[0]["user_ids"] == [
        "@guest:other.example",
        "@mod:third.example",
        "@test:m-test-4.mindroom.chat",
    ]


def test_matrix_server_name_defines_the_local_homeserver(tmp_path: Path) -> None:
    """The configured server name, not the homeserver URL host, decides what is local."""
    config = Config.model_validate(
        {
            "administrators": ["@owner:m-alpha.example.com"],
            "room_defaults": {"invite_users": ["@owner:m-alpha.example.com"]},
            "rooms": {"project": {}},
        },
    )

    assert _foreign_warnings(config, _runtime_paths(tmp_path, MATRIX_SERVER_NAME="m-alpha.example.com")) == []
    assert _foreign_warnings(config, _runtime_paths(tmp_path))[0]["user_ids"] == ["@owner:m-alpha.example.com"]


def test_placeholder_only_config_does_not_warn(tmp_path: Path) -> None:
    """The shipped owner placeholder grants nothing and must not be reported."""
    config = Config.model_validate(
        {
            "administrators": [OWNER_MATRIX_USER_ID_PLACEHOLDER],
            "room_defaults": {"invite_users": [OWNER_MATRIX_USER_ID_PLACEHOLDER]},
            "rooms": {"project": {}},
        },
    )

    assert _foreign_warnings(config, _runtime_paths(tmp_path)) == []
