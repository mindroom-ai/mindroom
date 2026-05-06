"""Tests for CLI `.env` file mutation helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.cli.env_file import env_path_for_config, upsert_env_values

if TYPE_CHECKING:
    from pathlib import Path


def test_env_path_for_config_uses_config_directory(tmp_path: Path) -> None:
    """The active config path should determine which sibling `.env` file is updated."""
    config_path = tmp_path / "nested" / "config.yaml"

    assert env_path_for_config(config_path) == config_path.parent.resolve() / ".env"


def test_upsert_env_values_preserves_lines_and_rewrites_exported_keys(tmp_path: Path) -> None:
    """Upserting values should preserve unrelated lines while normalizing replaced keys."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep this comment\nexport MATRIX_HOMESERVER=https://old.example\nUNRELATED=value\n\n",
        encoding="utf-8",
    )

    result = upsert_env_values(
        env_path,
        {
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MATRIX_SSL_VERIFY": "false",
        },
    )

    assert result == env_path
    assert env_path.read_text(encoding="utf-8") == (
        "# keep this comment\nMATRIX_HOMESERVER=http://localhost:8008\nUNRELATED=value\n\nMATRIX_SSL_VERIFY=false\n"
    )


def test_upsert_env_values_creates_parent_directory_and_file(tmp_path: Path) -> None:
    """Creating a missing env file should still use the same KEY=value format."""
    env_path = tmp_path / "missing" / ".env"

    upsert_env_values(env_path, {"MINDROOM_NAMESPACE": "a1b2c3d4"})

    assert env_path.read_text(encoding="utf-8") == "MINDROOM_NAMESPACE=a1b2c3d4\n"
