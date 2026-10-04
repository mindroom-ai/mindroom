"""Rendered tuwunel.toml checks for the Tuwunel chart's structured settings."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from tests.test_helm_instance_worker_isolation import _render_chart, _resource, _run_helm_template, _values_files

TUWUNEL_CHART = Path("cluster/k8s/tuwunel")

SHARED_VALUES = """
tuwunel:
  serverName: chat.example.com
  settings:
    login_with_password: false
    new_user_displayname_suffix: ""
    auto_join_rooms: ["#lobby:{{ .Values.tuwunel.serverName }}"]
    max_request_size: 104857600
    url_preview_bound_ratio: 0.5
    retired_option: true
    "dotted.option": quoted
    default_power_level_content_override:
      users_default: 50
      events:
        m.room.name: 50
  extraConfig: |
    allow_legacy_media = true
    welcome_text = '''
    login_with_password = false
    '''

    [global.media]
    startup_check = false
"""

ENVIRONMENT_VALUES = """
tuwunel:
  serverName: staging.example.com
  settings:
    login_with_password: true
    retired_option: null
"""


def _tuwunel_config(docs: list[dict[str, Any]]) -> dict[str, Any]:
    config_map = _resource(docs, "ConfigMap", "matrix-mindroom-tuwunel-config")
    return tomllib.loads(config_map["data"]["tuwunel.toml"])["global"]


def test_config_without_settings_renders_only_chart_options() -> None:
    """An empty settings map adds nothing to the rendered config."""
    config = _tuwunel_config(_render_chart(TUWUNEL_CHART, "tuwunel.serverName=example.com", release_name="matrix"))

    assert set(config) == {
        "server_name",
        "address",
        "port",
        "database_path",
        "log",
        "mindroom_compact_edits_enabled",
        "well_known",
    }


def test_settings_merge_across_values_files_into_valid_toml(tmp_path: Path) -> None:
    """An environment file changes single options while shared options, tables, and raw TOML still render."""
    values_files = _values_files(tmp_path, SHARED_VALUES, ENVIRONMENT_VALUES)
    config = _tuwunel_config(_render_chart(TUWUNEL_CHART, release_name="matrix", values_files=values_files))

    assert config["server_name"] == "staging.example.com"
    assert config["login_with_password"] is True
    assert config["new_user_displayname_suffix"] == ""
    assert config["auto_join_rooms"] == ["#lobby:staging.example.com"]
    assert config["max_request_size"] == 104857600
    assert isinstance(config["max_request_size"], int)
    assert config["url_preview_bound_ratio"] == 0.5
    assert config["dotted.option"] == "quoted"
    assert "retired_option" not in config
    assert config["default_power_level_content_override"] == {"users_default": 50, "events": {"m.room.name": 50}}
    assert config["allow_legacy_media"] is True
    assert config["welcome_text"] == "login_with_password = false\n"
    assert config["media"] == {"startup_check": False}
    assert config["well_known"] == {"client": "https://staging.example.com", "server": "staging.example.com:443"}


@pytest.mark.parametrize(
    ("values", "error"),
    [
        (
            """
            tuwunel:
              serverName: example.com
              settings:
                port: 8448
            """,
            "tuwunel.settings.port is rendered by the chart",
        ),
        (
            """
            tuwunel:
              serverName: example.com
              registrationToken:
                existingSecret: matrix-registration
              settings:
                allow_registration: true
            """,
            "tuwunel.settings.allow_registration is rendered by the chart",
        ),
        (
            """
            tuwunel:
              serverName: example.com
              settings:
                identity_providers:
                  - brand: keycloak
            """,
            "tuwunel.settings.identity_providers[0] must be a string, number, boolean, or list",
        ),
    ],
    ids=["always-managed-option", "registration-token-option", "array-of-tables"],
)
def test_settings_reject_options_that_cannot_render_cleanly(tmp_path: Path, values: str, error: str) -> None:
    """Duplicate chart options and arrays of tables fail at render time instead of at homeserver startup."""
    completed = _run_helm_template(TUWUNEL_CHART, release_name="matrix", values_files=_values_files(tmp_path, values))

    assert completed.returncode != 0
    assert error in completed.stderr


def test_registration_option_is_free_without_a_registration_token() -> None:
    """Options the chart renders only conditionally stay settable when the chart omits them."""
    config = _tuwunel_config(
        _render_chart(
            TUWUNEL_CHART,
            "tuwunel.serverName=example.com",
            "tuwunel.settings.allow_registration=false",
            release_name="matrix",
        ),
    )

    assert config["allow_registration"] is False
