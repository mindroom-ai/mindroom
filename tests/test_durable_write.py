"""Tests for shared durable JSON and override-record helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom import durable_write
from mindroom.durable_write import (
    delete_file_durable,
    load_cached_override_records,
    replace_file_durable,
    write_json_file_durable,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_durable_json_write_creates_target_parent_with_separate_temp_dir(tmp_path: Path) -> None:
    """A separate temp directory must not bypass target-parent creation."""
    target = tmp_path / "target" / "payload.json"
    temp_dir = tmp_path / "temp"

    write_json_file_durable(target, {"value": 1}, temp_dir=temp_dir)

    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 1}
    assert not list(temp_dir.glob("*.tmp"))


def test_durable_delete_fsyncs_parent_after_unlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A successful delete must durably publish the parent-directory update."""
    target = tmp_path / "credential.json"
    target.write_text("secret", encoding="utf-8")
    fsynced: list[Path] = []
    monkeypatch.setattr(durable_write, "_fsync_directory_durable", fsynced.append)

    assert delete_file_durable(target) is True
    assert not target.exists()
    assert fsynced == [tmp_path]
    assert delete_file_durable(target) is False
    assert fsynced == [tmp_path]


def test_durable_replace_fsyncs_parent_after_publish(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An atomic replacement must durably publish the parent-directory update."""
    source = tmp_path / "credential.tmp"
    target = tmp_path / "credential.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    fsynced: list[Path] = []
    monkeypatch.setattr(durable_write, "_fsync_directory_durable", fsynced.append)

    replace_file_durable(source, target)

    assert target.read_text(encoding="utf-8") == "new"
    assert not source.exists()
    assert fsynced == [tmp_path]


def test_durable_delete_propagates_directory_fsync_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A caller must not report deletion committed when directory durability is unknown."""
    target = tmp_path / "credential.json"
    target.write_text("secret", encoding="utf-8")

    def fail_fsync(_directory: Path) -> None:
        msg = "directory fsync failed"
        raise OSError(msg)

    monkeypatch.setattr(durable_write, "_fsync_directory_durable", fail_fsync)

    with pytest.raises(OSError, match="directory fsync failed"):
        delete_file_durable(target)


def test_cached_override_records_are_returned_as_independent_snapshots(tmp_path: Path) -> None:
    """Callers must not be able to mutate cached record dictionaries."""
    path = tmp_path / "overrides.json"
    path.write_text(
        json.dumps({"record": {"model": "default", "set_at": "2026-07-09T00:00:00+00:00"}}),
        encoding="utf-8",
    )

    def is_valid(_record_id: str, record: dict[object, object]) -> bool:
        return isinstance(record.get("model"), str) and isinstance(record.get("set_at"), str)

    first = load_cached_override_records(path, is_valid)
    first["record"]["model"] = "mutated"
    first["extra"] = {"model": "extra", "set_at": "2026-07-09T00:00:01+00:00"}

    second = load_cached_override_records(path, is_valid)
    assert second == {"record": {"model": "default", "set_at": "2026-07-09T00:00:00+00:00"}}

    second["record"]["model"] = "mutated-again"
    assert load_cached_override_records(path, is_valid) == {
        "record": {"model": "default", "set_at": "2026-07-09T00:00:00+00:00"},
    }
