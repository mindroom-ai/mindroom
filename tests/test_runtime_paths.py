"""Tests for primary and worker runtime path boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom import constants
from mindroom.runtime_env_policy import CONTROL_STATE_PATH_ENV

if TYPE_CHECKING:
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
