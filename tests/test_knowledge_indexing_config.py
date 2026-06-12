"""Unit tests for knowledge indexing-config identity invariants.

Storage keys and indexing-settings metadata are persisted cache/identity keys
for vector collections, so their stability is the invariant pinned here.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from mindroom.knowledge.indexing_config import IndexingSettings, storage_key_for_base

if TYPE_CHECKING:
    from pathlib import Path


def _settings(base_id: str = "docs") -> IndexingSettings:
    return IndexingSettings(
        base_id=base_id,
        storage_root="storage",
        knowledge_path=f"knowledge/{base_id}",
        mode="semantic",
        embedder_provider="openai",
        embedder_model="text-embedding-3-small",
        embedder_host="",
        embedder_dimensions="",
        chunk_size="5000",
        chunk_overlap="0",
        repo_identity="",
        git_branch="",
        git_lfs="",
        git_skip_hidden="",
        git_include_patterns="",
        git_exclude_patterns="",
        include_patterns="",
        exclude_patterns="",
        include_extensions="",
        exclude_extensions="()",
    )


def test_storage_key_for_base_is_deterministic(tmp_path: Path) -> None:
    """Same base ID and path must always produce the same persisted key."""
    knowledge_path = tmp_path / "docs"
    assert storage_key_for_base("docs", knowledge_path) == storage_key_for_base("docs", knowledge_path)


def test_storage_key_for_base_pins_persisted_key_format(tmp_path: Path) -> None:
    """The key format is persisted on disk and must stay byte-identical."""
    knowledge_path = tmp_path / "docs"
    digest = hashlib.sha256(f"docs:{knowledge_path.resolve()}".encode()).hexdigest()[:8]
    assert storage_key_for_base("docs", knowledge_path) == f"docs_{digest}"


def test_storage_key_for_base_differs_per_base_and_path(tmp_path: Path) -> None:
    """Distinct base IDs or paths must map to distinct storage keys."""
    docs_path = tmp_path / "docs"
    other_path = tmp_path / "other"
    assert storage_key_for_base("docs", docs_path) != storage_key_for_base("wiki", docs_path)
    assert storage_key_for_base("docs", docs_path) != storage_key_for_base("docs", other_path)


def test_storage_key_for_base_sanitizes_unsafe_identifiers(tmp_path: Path) -> None:
    """Unsafe characters are sanitized while the digest keeps keys unique."""
    knowledge_path = tmp_path / "docs"
    key = storage_key_for_base("my docs/v1", knowledge_path)
    assert key.startswith("my_docs_v1_")
    assert key != storage_key_for_base("my docs.v1", knowledge_path)


def test_indexing_settings_metadata_round_trip() -> None:
    """to_metadata/from_metadata must round-trip without loss."""
    settings = _settings()
    assert IndexingSettings.from_metadata(settings.to_metadata()) == settings


def test_indexing_settings_from_metadata_rejects_invalid_payloads() -> None:
    """Unknown keys, missing keys, and unknown modes are rejected."""
    metadata = _settings().to_metadata()
    assert IndexingSettings.from_metadata({**metadata, "unexpected": "value"}) is None
    assert IndexingSettings.from_metadata({key: value for key, value in metadata.items() if key != "base_id"}) is None
    assert IndexingSettings.from_metadata({**metadata, "mode": "unknown"}) is None


def test_indexing_settings_from_metadata_defaults_optional_filter_keys() -> None:
    """Older payloads without include/exclude patterns still parse."""
    metadata = _settings().to_metadata()
    del metadata["include_patterns"]
    del metadata["exclude_patterns"]
    parsed = IndexingSettings.from_metadata(metadata)
    assert parsed is not None
    assert parsed.include_patterns == ""
    assert parsed.exclude_patterns == ""
