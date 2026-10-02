"""Tests for CLI `.env` file mutation helpers."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest
from dotenv import dotenv_values

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


def test_upsert_env_values_leaves_exactly_one_assignment(tmp_path: Path) -> None:
    """Later duplicate assignments, which dotenv would let win, are removed so the upserted value is effective."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "OPENAI_API_KEY=\nUNRELATED=value\nexport OPENAI_API_KEY=your-openai-key-here\nOPENAI_API_KEY=old\n",
        encoding="utf-8",
    )

    upsert_env_values(env_path, {"OPENAI_API_KEY": "sk-new"})

    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-new\nUNRELATED=value\n"
    assert dotenv_values(env_path)["OPENAI_API_KEY"] == "sk-new"


def test_upsert_env_values_creates_parent_directory_and_file(tmp_path: Path) -> None:
    """Creating a missing env file should still use the same KEY=value format."""
    env_path = tmp_path / "missing" / ".env"

    upsert_env_values(env_path, {"MINDROOM_NAMESPACE": "a1b2c3d4"})

    assert env_path.read_text(encoding="utf-8") == "MINDROOM_NAMESPACE=a1b2c3d4\n"
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_upsert_env_values_hardens_existing_env_file(tmp_path: Path) -> None:
    """Updating an existing env file should remove access for other OS users."""
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=value\n", encoding="utf-8")
    env_path.chmod(0o644)

    upsert_env_values(env_path, {"NEW": "secret"})

    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_upsert_env_values_refuses_symlink_destination(tmp_path: Path) -> None:
    """Env writes should never follow a destination symlink."""
    target_path = tmp_path / "target"
    target_path.write_text("preserve-me\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.symlink_to(target_path)

    with pytest.raises(ValueError, match="Refusing to write env file through a symlink"):
        upsert_env_values(env_path, {"NEW": "secret"})

    assert target_path.read_text(encoding="utf-8") == "preserve-me\n"


@pytest.mark.parametrize(
    "value",
    [
        'secret\nMINDROOM_STORAGE_PATH="/tmp/x\nExecStartPre=/bin/sh -c id\n#"',
        "secret\rMATRIX_HOMESERVER=https://attacker.example",
        "secret\u2028MATRIX_HOMESERVER=https://attacker.example",
        "secret\x0bMATRIX_HOMESERVER=https://attacker.example",
        "secret\x00",
    ],
)
def test_upsert_env_values_refuses_values_that_would_become_extra_lines(tmp_path: Path, value: str) -> None:
    """A value can never add assignments, now or when a later upsert splits the file into lines again."""
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=value\n", encoding="utf-8")

    with pytest.raises(ValueError, match="MINDROOM_LOCAL_CLIENT_SECRET") as exc_info:
        upsert_env_values(env_path, {"MINDROOM_NAMESPACE": "a1b2c3d4", "MINDROOM_LOCAL_CLIENT_SECRET": value})

    assert "secret" not in str(exc_info.value).replace("MINDROOM_LOCAL_CLIENT_SECRET", "")
    assert env_path.read_text(encoding="utf-8") == "EXISTING=value\n"


@pytest.mark.parametrize(
    "value",
    ["sk$ecret", "it's", 'say "hi"', "a #b", "#start", " padded ", "back\\slash\\", "'", "\\'"],
)
def test_upsert_env_values_reads_back_unchanged(tmp_path: Path, value: str) -> None:
    """Values that unquoted `.env` lines would change are quoted, while plain values stay unquoted."""
    env_path = tmp_path / ".env"

    upsert_env_values(env_path, {"KEY": value, "PLAIN": "value"})

    assert dotenv_values(env_path) == {"KEY": value, "PLAIN": "value"}
    assert env_path.read_text(encoding="utf-8").endswith("\nPLAIN=value\n")


def test_upsert_env_values_refuses_values_dotenv_would_expand(tmp_path: Path) -> None:
    """python-dotenv expands `${NAME}` even in quoted values, so such a value is refused before anything is written."""
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=value\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Refusing to write MINDROOM_API_KEY to the env file"):
        upsert_env_values(env_path, {"MINDROOM_NAMESPACE": "a1b2c3d4", "MINDROOM_API_KEY": "secret-${HOME}"})

    assert env_path.read_text(encoding="utf-8") == "EXISTING=value\n"
