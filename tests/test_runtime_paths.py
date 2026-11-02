"""Tests for primary and worker runtime path boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom import constants
from mindroom.runtime_env_policy import CONTROL_STATE_PATH_ENV

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path


def test_control_state_defaults_under_storage_root(tmp_path: Path) -> None:
    """Primary runtime defaults control state under the storage root."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )

    assert runtime_paths.control_state_root == (tmp_path / "storage" / "control_state").resolve()
    assert runtime_paths.env_value(CONTROL_STATE_PATH_ENV) == str(runtime_paths.control_state_root)
    assert CONTROL_STATE_PATH_ENV not in runtime_paths.process_env


def test_control_state_env_override(tmp_path: Path) -> None:
    """Primary runtime accepts an explicit control-state env override."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={CONTROL_STATE_PATH_ENV: str(tmp_path / "control")},
    )

    assert runtime_paths.control_state_root == (tmp_path / "control").resolve()
    assert runtime_paths.process_env[CONTROL_STATE_PATH_ENV] == str((tmp_path / "control").resolve())


def test_runtime_paths_with_storage_root_rebases_default_control_state(tmp_path: Path) -> None:
    """Default control state follows storage-root rebases."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )

    rebased = constants.runtime_paths_with_storage_root(runtime_paths, tmp_path / "other-storage")

    assert rebased.storage_root == (tmp_path / "other-storage").resolve()
    assert rebased.control_state_root == (tmp_path / "other-storage" / "control_state").resolve()


def test_runtime_paths_with_storage_root_preserves_explicit_control_state(tmp_path: Path) -> None:
    """Explicit control state stays outside storage when storage root changes."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={CONTROL_STATE_PATH_ENV: str(tmp_path / "control")},
    )

    rebased = constants.runtime_paths_with_storage_root(runtime_paths, tmp_path / "other-storage")

    assert rebased.storage_root == (tmp_path / "other-storage").resolve()
    assert rebased.control_state_root == (tmp_path / "control").resolve()


def test_worker_serialized_runtime_paths_excludes_control_state(tmp_path: Path) -> None:
    """Public worker startup payloads do not carry the primary control-state env."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={CONTROL_STATE_PATH_ENV: str(tmp_path / "control")},
    )

    payload = constants.serialize_public_runtime_paths(runtime_paths)

    assert CONTROL_STATE_PATH_ENV not in payload["process_env"]
    assert CONTROL_STATE_PATH_ENV not in payload["env_file_values"]


def test_worker_serialized_runtime_paths_excludes_session_storage(tmp_path: Path) -> None:
    """Workers must not receive the control-plane-only session volume path."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SESSION_STORAGE_PATH": str(tmp_path / "sessions")},
    )

    payload = constants.serialize_public_runtime_paths(runtime_paths)

    assert "MINDROOM_SESSION_STORAGE_PATH" not in payload["process_env"]
    assert "MINDROOM_SESSION_STORAGE_PATH" not in payload["env_file_values"]


def test_deserialized_public_runtime_paths_cannot_recover_control_state(tmp_path: Path) -> None:
    """Worker startup deserialization keeps control state absent."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )

    worker_paths = constants.deserialize_runtime_paths(constants.serialize_public_runtime_paths(runtime_paths))

    assert worker_paths.control_state_root is None
    assert worker_paths.env_value(CONTROL_STATE_PATH_ENV) is None


def test_isolated_runtime_paths_excludes_control_state_from_worker_payload(tmp_path: Path) -> None:
    """Isolated worker runtime views strip primary control-state env."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={CONTROL_STATE_PATH_ENV: str(tmp_path / "control")},
    )

    worker_paths = constants.isolated_runtime_paths(runtime_paths)

    assert worker_paths.control_state_root is None
    assert worker_paths.env_value(CONTROL_STATE_PATH_ENV) is None
    assert CONTROL_STATE_PATH_ENV not in worker_paths.process_env
    assert CONTROL_STATE_PATH_ENV not in worker_paths.env_file_values
    assert constants.serialize_public_runtime_paths(worker_paths)["process_env"].get(CONTROL_STATE_PATH_ENV) is None


def test_isolated_runtime_paths_keep_the_process_env_over_the_env_file(tmp_path: Path) -> None:
    """An isolated worker sees each layer it may inherit in its own place, so the process env still wins."""
    (tmp_path / ".env").write_text("MINDROOM_TIMEZONE=from-file\nMINDROOM_LOCALE=file-only\n", encoding="utf-8")
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_TIMEZONE": "from-process"},
    )

    worker_paths = constants.isolated_runtime_paths(runtime_paths)

    assert "MINDROOM_LOCALE" not in worker_paths.process_env
    assert worker_paths.env_file_values["MINDROOM_TIMEZONE"] == "from-file"
    assert worker_paths.env_value("MINDROOM_TIMEZONE") == "from-process"
    assert worker_paths.env_value("MINDROOM_LOCALE") == "file-only"


@pytest.mark.parametrize(
    ("process_value", "file_value", "expected"),
    [
        (None, None, "default"),
        ("from-process", "from-file", "from-process"),
        (None, "from-file", "from-file"),
        # A compose passthrough of an unset variable exports it blank; that must not hide the `.env` value.
        ("", "from-file", "from-file"),
        ("  ", "from-file", "from-file"),
        ("", "", "default"),
        ("", None, "default"),
        # An unedited starter-template placeholder is unset in either layer too.
        ("your-anthropic-key-here", "sk-from-file", "sk-from-file"),
        ("your-anthropic-key-here", "your-anthropic-key-here", "default"),
    ],
)
def test_env_value_counts_blank_and_placeholder_values_as_unset(
    tmp_path: Path,
    process_value: str | None,
    file_value: str | None,
    expected: str,
) -> None:
    """Every variable reads the process env, then the `.env`, then the default, skipping blank or placeholder values."""
    name = "ANTHROPIC_API_KEY"
    if file_value is not None:
        (tmp_path / ".env").write_text(f"{name}={file_value}\n", encoding="utf-8")
    runtime_paths = constants.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={} if process_value is None else {name: process_value},
    )

    assert runtime_paths.env_value(name, default="default") == expected
    assert runtime_paths.env_value(name) == (None if expected == "default" else expected)


@pytest.mark.parametrize(
    ("file_text", "expected"),
    [("", True), ("MATRIX_SSL_VERIFY=false\n", False)],
)
def test_env_flag_under_a_blank_export_reads_the_env_file_then_the_default(
    tmp_path: Path,
    file_text: str,
    *,
    expected: bool,
) -> None:
    """A blank exported flag is unset, so it neither disables a default-on flag nor hides the `.env` value."""
    (tmp_path / ".env").write_text(file_text, encoding="utf-8")
    runtime_paths = constants.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MATRIX_SSL_VERIFY": ""},
    )

    assert runtime_paths.env_flag("MATRIX_SSL_VERIFY", default=True) is expected
    assert constants.runtime_matrix_ssl_verify(runtime_paths) is expected


@pytest.mark.parametrize(
    "merged_env",
    [
        constants.runtime_env_values,
        constants.trusted_tool_runtime_env_values,
        lambda runtime_paths: constants.build_execution_tool_env("python", runtime_paths),
    ],
    ids=["runtime", "trusted-tool", "execution-tool"],
)
def test_merged_env_mappings_keep_the_env_file_value_under_a_blank_export(
    tmp_path: Path,
    merged_env: Callable[[constants.RuntimePaths], Mapping[str, str]],
) -> None:
    """The env mappings tools and worker backends read follow ``env_value``; a lone exported blank still stays."""
    (tmp_path / ".env").write_text(
        "MINDROOM_TIMEZONE=Europe/Amsterdam\nANTHROPIC_API_KEY=your-anthropic-key-here\n",
        encoding="utf-8",
    )
    runtime_paths = constants.resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MINDROOM_TIMEZONE": "", "MINDROOM_LOCALE": "", "ANTHROPIC_API_KEY": ""},
    )

    env = merged_env(runtime_paths)

    assert (env["MINDROOM_TIMEZONE"], env["MINDROOM_LOCALE"]) == ("Europe/Amsterdam", "")
    # A starter-template placeholder never reaches a tool, whatever the layer filters let through.
    assert env.get("ANTHROPIC_API_KEY", "") == ""
