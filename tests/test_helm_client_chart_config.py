"""Rendered config.json checks for the web client chart's structured config values."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tests.test_helm_instance_worker_isolation import _render_chart, _run_helm_template

CLIENT_CHART = Path("cluster/k8s/client")

SHARED_VALUES = """
matrix:
  homeserverUrl: https://chat.example.com
config:
  values:
    auth:
      allowRegistration: false
      disablePasswordLogin: true
    featuredCommunities:
      openAsDefault: false
      rooms:
        - '#lobby:{{ .Values.matrix.homeserverUrl | trimPrefix "https://" }}'
        - '#general:{{ .Values.matrix.homeserverUrl | trimPrefix "https://" }}'
    mindroom:
      computers:
        apiUrl: "{{ .Values.matrix.homeserverUrl }}"
      maxUploadBytes: 104857600
      typingRatio: 0.5
"""

ENVIRONMENT_VALUES = """
matrix:
  homeserverUrl: https://staging.example.com
config:
  values:
    auth:
      allowRegistration: true
"""


def _values_file(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _client_config(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return json.loads(next(doc["data"]["config.json"] for doc in docs if "config.json" in doc.get("data", {})))


def _render_layered(tmp_path: Path, *contents: str) -> list[dict[str, Any]]:
    values_files = tuple(
        _values_file(tmp_path, f"values-{index}.yaml", content) for index, content in enumerate(contents)
    )
    return _render_chart(CLIENT_CHART, release_name="mindroom-client", values_files=values_files)


def test_default_client_config_is_unchanged_without_structured_values() -> None:
    """The chart default config.json stays the minimal homeserver document."""
    docs = _render_chart(CLIENT_CHART, release_name="mindroom-client")

    assert _client_config(docs) == {
        "defaultHomeserver": 0,
        "homeserverList": ["https://matrix.example.com"],
        "allowCustomHomeservers": False,
        "hashRouter": {"enabled": False, "basename": "/"},
    }


def test_structured_values_merge_over_the_default_config_across_values_files(tmp_path: Path) -> None:
    """An environment file overrides one nested setting while shared settings and templates follow it."""
    config = _client_config(_render_layered(tmp_path, SHARED_VALUES, ENVIRONMENT_VALUES))

    assert config == {
        "defaultHomeserver": 0,
        "homeserverList": ["https://staging.example.com"],
        "allowCustomHomeservers": False,
        "hashRouter": {"enabled": False, "basename": "/"},
        "auth": {"allowRegistration": True, "disablePasswordLogin": True},
        "featuredCommunities": {
            "openAsDefault": False,
            "rooms": ["#lobby:staging.example.com", "#general:staging.example.com"],
        },
        "mindroom": {
            "computers": {"apiUrl": "https://staging.example.com"},
            "maxUploadBytes": 104857600,
            "typingRatio": 0.5,
        },
    }


def test_structured_values_override_chart_default_entries(tmp_path: Path) -> None:
    """Structured values replace default lists and override nested default keys."""
    config = _client_config(
        _render_layered(
            tmp_path,
            """
            config:
              values:
                homeserverList:
                  - https://chat.example.com
                  - https://other.example.com
                hashRouter:
                  enabled: true
            """,
        ),
    )

    assert config["homeserverList"] == ["https://chat.example.com", "https://other.example.com"]
    assert config["hashRouter"] == {"enabled": True, "basename": "/"}


@pytest.mark.parametrize(
    ("values", "error"),
    [
        (
            """
            config:
              data: '{"defaultHomeserver": 0}'
              values:
                auth:
                  allowRegistration: false
            """,
            "config.values cannot be combined with config.data",
        ),
        (
            """
            config:
              values: false
            """,
            "config.values must be a map",
        ),
        (
            """
            config:
              existingConfigMap: client-config
              values:
                auth:
                  allowRegistration: false
            """,
            "config.values requires the chart-managed client config",
        ),
    ],
    ids=["with-config-data", "not-a-map", "existing-config-map"],
)
def test_structured_values_reject_inputs_they_cannot_apply(tmp_path: Path, values: str, error: str) -> None:
    """Structured values never disappear silently."""
    completed = _run_helm_template(
        CLIENT_CHART,
        release_name="mindroom-client",
        values_files=(_values_file(tmp_path, "values.yaml", values),),
    )

    assert completed.returncode != 0
    assert error in completed.stderr
