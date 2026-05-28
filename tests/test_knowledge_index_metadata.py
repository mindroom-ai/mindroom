"""Knowledge published-index metadata codec tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindroom.knowledge.index_metadata import (
    load_index_metadata_payload,
    parse_index_metadata_fields,
    write_index_metadata_payload,
)


def test_index_metadata_fields_support_registry_and_manager_strictness(tmp_path: Path) -> None:
    """Metadata parsing keeps registry leniency separate from manager strictness."""
    metadata_path = tmp_path / "indexing_settings.json"
    metadata_path.write_text(
        json.dumps(
            {
                "settings": {"base": "storage"},
                "status": "indexing",
                "refresh_job": "running",
            },
        ),
        encoding="utf-8",
    )

    payload = load_index_metadata_payload(metadata_path)
    assert payload is not None
    assert (
        parse_index_metadata_fields(
            payload,
            allowed_statuses={"resetting", "indexing", "complete"},
            require_complete_fields_for_all_statuses=True,
        )
        is None
    )

    fields = parse_index_metadata_fields(
        payload,
        allowed_statuses={"resetting", "indexing", "complete", "failed"},
        require_complete_fields_for_all_statuses=False,
    )

    assert fields == ({"base": "storage"}, "indexing", None, None, None, None, None)


def test_strict_index_metadata_parsing_requires_collection_except_complete_file_mode() -> None:
    """Strict metadata parsing should only omit collection for completed file-mode records."""
    indexing_payload = {
        "settings": {"base": "storage", "mode": "files"},
        "status": "indexing",
        "indexed_count": 0,
        "source_signature": "source-signature",
    }

    assert (
        parse_index_metadata_fields(
            indexing_payload,
            allowed_statuses={"indexing", "complete"},
            require_complete_fields_for_all_statuses=True,
        )
        is None
    )
    assert parse_index_metadata_fields(
        {**indexing_payload, "status": "complete"},
        allowed_statuses={"indexing", "complete"},
        require_complete_fields_for_all_statuses=True,
    ) == ({"base": "storage", "mode": "files"}, "complete", None, None, None, 0, "source-signature")


def test_strict_index_metadata_parsing_accepts_complete_file_mode_from_settings() -> None:
    """Published file-mode metadata stores file mode in indexing settings."""
    payload = {
        "settings": {"base": "storage", "mode": "files"},
        "status": "complete",
        "indexed_count": 0,
        "source_signature": "source-signature",
    }

    assert parse_index_metadata_fields(
        payload,
        allowed_statuses={"indexing", "complete"},
        require_complete_fields_for_all_statuses=True,
    ) == ({"base": "storage", "mode": "files"}, "complete", None, None, None, 0, "source-signature")


def test_write_index_metadata_payload_preserves_field_names_and_omits_none_values(tmp_path: Path) -> None:
    """Payload building preserves on-disk field names and omits absent optional fields."""
    metadata_path = tmp_path / "indexing_settings.json"

    write_index_metadata_payload(
        metadata_path,
        settings={"base": "storage"},
        status="complete",
        collection="published_collection",
        last_published_at="2026-01-02T03:04:05+00:00",
        indexed_count=7,
        source_signature="source-signature",
        refresh_job="idle",
        reason=None,
        last_refresh_at="2026-01-02T03:05:06+00:00",
    )

    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert payload == {
        "settings": {"base": "storage"},
        "status": "complete",
        "collection": "published_collection",
        "last_published_at": "2026-01-02T03:04:05+00:00",
        "indexed_count": 7,
        "source_signature": "source-signature",
        "refresh_job": "idle",
        "last_refresh_at": "2026-01-02T03:05:06+00:00",
    }


def test_write_index_metadata_payload_uses_unique_temp_and_cleans_failed_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Atomic writes use unique hidden temp files and clean them up on failure."""
    metadata_path = tmp_path / "indexing_settings.json"
    attempted_temp_paths: list[Path] = []
    original_replace = Path.replace

    def _fail_temp_replace(self: Path, target: Path) -> Path:
        if self.parent == tmp_path and self.name.startswith(".indexing_settings.json.") and self.name.endswith(".tmp"):
            attempted_temp_paths.append(self)
            msg = "replace failed"
            raise OSError(msg)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _fail_temp_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_index_metadata_payload(
            metadata_path,
            settings={"base": "storage"},
            status="complete",
        )

    assert attempted_temp_paths
    assert attempted_temp_paths[0].name != "indexing_settings.json.tmp"
    assert not attempted_temp_paths[0].exists()
