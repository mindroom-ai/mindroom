"""Tests for deciding when a hosted install must pair before startup."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.matrix.provisioning_env import local_pairing_required

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime(tmp_path: Path, env: dict[str, str]) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data", process_env=env)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"MINDROOM_PROVISIONING_URL": "https://mindroom.chat"}, True),
        ({"MINDROOM_PROVISIONING_URL": "https://mindroom.chat", "MATRIX_REGISTRATION_TOKEN": "t"}, False),
        (
            {
                "MINDROOM_PROVISIONING_URL": "https://mindroom.chat",
                "MINDROOM_LOCAL_CLIENT_ID": "id",
                "MINDROOM_LOCAL_CLIENT_SECRET": "secret",
            },
            False,
        ),
        (
            {"MINDROOM_PROVISIONING_URL": "https://mindroom.chat", "MATRIX_REGISTRATION_SHARED_SECRET": "s"},
            False,
        ),
        ({}, False),
    ],
)
def test_local_pairing_required(tmp_path: Path, env: dict[str, str], expected: bool) -> None:
    """Only hosted installs without a registration token, shared secret, or credentials must pair."""
    assert local_pairing_required(_runtime(tmp_path, env)) is expected


def test_local_pairing_not_required_with_shared_secret_file(tmp_path: Path) -> None:
    """A shared secret supplied through its _FILE variant also registers without pairing."""
    secret_file = tmp_path / "shared_secret"
    secret_file.write_text("s\n", encoding="utf-8")
    env = {
        "MINDROOM_PROVISIONING_URL": "https://mindroom.chat",
        "MATRIX_REGISTRATION_SHARED_SECRET_FILE": str(secret_file),
    }

    assert local_pairing_required(_runtime(tmp_path, env)) is False
