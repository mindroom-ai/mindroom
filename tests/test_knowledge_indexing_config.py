"""Tests for knowledge indexing-config identity and probe resource ownership.

Storage keys and indexing-settings metadata are persisted cache/identity keys
for vector collections, so their stability is the invariant pinned here.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Never
from unittest.mock import Mock

import pytest
from agno.knowledge.embedder.base import Embedder
from agno.vectordb.chroma import ChromaDb as AgnoChromaDb
from chromadb.api import ClientAPI
from chromadb.api.client import Client
from chromadb.config import Settings

from mindroom.knowledge.chroma_client import ChromaDb
from mindroom.knowledge.indexing_config import IndexingSettings, chroma_collection_exists, storage_key_for_base


def test_chroma_client_rejects_client_without_concrete_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported provider client must fail at the typed ownership boundary."""
    client = Mock(spec_set=ClientAPI)
    monkeypatch.setattr(AgnoChromaDb, "client", property(lambda _self: client))
    vector_db = ChromaDb(collection="collection", path=str(tmp_path), embedder=Embedder())

    with pytest.raises(TypeError, match="Expected a concrete Chroma client"):
        _ = vector_db.client


def _assert_probe_storage_released(storage_path: Path) -> None:
    # A fresh client may change settings only once every previous client has
    # closed. This detects leaked Chroma systems through its public API.
    with Client(settings=Settings(is_persistent=True, persist_directory=str(storage_path), allow_reset=True)) as client:
        assert client.count_collections() == 1


@pytest.mark.parametrize(("collection_name", "expected"), [("present", True), ("missing", False)])
def test_collection_probe_releases_storage(tmp_path: Path, collection_name: str, expected: bool) -> None:
    """Repeated found/missing probes must leave no unowned persistent client behind."""
    with Client(settings=Settings(is_persistent=True, persist_directory=str(tmp_path))) as client:
        client.create_collection("present")

    for _ in range(3):
        assert chroma_collection_exists(tmp_path, collection_name) is expected

    _assert_probe_storage_released(tmp_path)


def test_collection_probe_releases_storage_after_lookup_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed lookup must release the probe's client just like a successful lookup."""
    with Client(settings=Settings(is_persistent=True, persist_directory=str(tmp_path))) as client:
        client.create_collection("present")

    def fail_lookup(self: Client, name: str, **kwargs: object) -> Never:  # noqa: ARG001
        message = "collection lookup failed"
        raise RuntimeError(message)

    monkeypatch.setattr(Client, "get_collection", fail_lookup)
    assert chroma_collection_exists(tmp_path, "present") is False

    _assert_probe_storage_released(tmp_path)


def test_collection_probe_keeps_retained_reader_queryable(tmp_path: Path) -> None:
    """Closing a probe must preserve another client's shared system and leave no extra owner."""
    with Client(settings=Settings(is_persistent=True, persist_directory=str(tmp_path))) as reader:
        collection = reader.create_collection("present")
        collection.add(ids=["document"], embeddings=[[1.0, 0.0]])

        assert chroma_collection_exists(tmp_path, "present") is True
        assert chroma_collection_exists(tmp_path, "missing") is False
        assert collection.query(query_embeddings=[[1.0, 0.0]], n_results=1)["ids"] == [["document"]]

    _assert_probe_storage_released(tmp_path)


def test_collection_probe_returns_false_when_client_cannot_open(tmp_path: Path) -> None:
    """Client construction errors keep the probe's existing false-result contract."""
    with Client(settings=Settings(is_persistent=True, persist_directory=str(tmp_path), allow_reset=True)) as reader:
        reader.create_collection("present")
        # The probe's default settings conflict with this live client.
        assert chroma_collection_exists(tmp_path, "present") is False
        assert reader.count_collections() == 1


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
        include_patterns="()",
        exclude_patterns="()",
        include_extensions="",
        exclude_extensions="()",
        extra_extensions="()",
    )


def _legacy_metadata() -> dict[str, str]:
    return {
        "base_id": "docs",
        "storage_root": "storage",
        "knowledge_path": "knowledge/docs",
        "mode": "semantic",
        "embedder_provider": "openai",
        "embedder_model": "text-embedding-3-small",
        "embedder_host": "",
        "embedder_dimensions": "",
        "chunk_size": "5000",
        "chunk_overlap": "0",
        "repo_identity": "",
        "git_branch": "",
        "git_lfs": "",
        "git_skip_hidden": "",
        "git_include_patterns": "",
        "git_exclude_patterns": "",
        "include_extensions": "",
        "exclude_extensions": "()",
    }


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


def test_storage_key_for_base_resolves_each_path_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated lookups must not re-enter the filesystem, which can be a slow network mount."""
    storage_key_for_base.cache_clear()
    knowledge_path = tmp_path / "docs"
    resolved: list[Path] = []
    original_resolve = Path.resolve

    def counting_resolve(self: Path, strict: bool = False) -> Path:
        resolved.append(self)
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    first = storage_key_for_base("docs", knowledge_path)
    second = storage_key_for_base("docs", knowledge_path)

    assert first == second
    assert resolved == [knowledge_path]


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


def test_indexing_settings_from_metadata_normalizes_legacy_empty_filter_keys() -> None:
    """Older semantic payloads normalize absent or empty filter tuples at the parse boundary."""
    metadata = _legacy_metadata()
    original = dict(metadata)
    parsed = IndexingSettings.from_metadata(metadata)

    assert parsed is not None
    assert (parsed.include_patterns, parsed.exclude_patterns, parsed.extra_extensions) == ("()", "()", "()")
    assert (parsed.skip_hidden, parsed.require_content_before_publish) == ("", "")
    assert metadata == original
    assert IndexingSettings.from_metadata(parsed.to_metadata()) == parsed


def test_files_mode_legacy_empty_patterns_normalize_without_semantic_extensions() -> None:
    """File-mode patterns remain tuple keys while semantic-only extensions stay empty."""
    metadata = replace(_settings(), mode="files", extra_extensions="").to_metadata()
    metadata["include_patterns"] = ""
    del metadata["exclude_patterns"]
    metadata["extra_extensions"] = ""

    parsed = IndexingSettings.from_metadata(metadata)

    assert parsed is not None
    assert parsed.include_patterns == "()"
    assert parsed.exclude_patterns == "()"
    assert parsed.extra_extensions == ""


def test_skip_hidden_changes_corpus_key_but_not_query_key() -> None:
    """Indexes published before hidden-path filtering ('' from old metadata) must rebuild, not stay queryable."""
    legacy = IndexingSettings.from_metadata(
        {
            **_legacy_metadata(),
            "include_patterns": "('docs/**',)",
            "exclude_patterns": "('drafts/**',)",
            "extra_extensions": "('.mdx',)",
        },
    )
    assert legacy is not None
    assert (legacy.include_patterns, legacy.exclude_patterns, legacy.extra_extensions) == (
        "('docs/**',)",
        "('drafts/**',)",
        "('.mdx',)",
    )
    current = replace(legacy, skip_hidden="True")
    assert legacy.corpus_compatibility_key() != current.corpus_compatibility_key()
    assert legacy.query_compatibility_key() == current.query_compatibility_key()


def test_content_publication_gate_round_trips_and_changes_corpus_key() -> None:
    """The runtime-overlay publication gate must persist and invalidate ungated empty indexes."""
    ungated = IndexingSettings.from_metadata(
        {
            **_legacy_metadata(),
            "include_patterns": "('published/**',)",
            "exclude_patterns": "('drafts/**',)",
            "extra_extensions": "('.mdx',)",
            "skip_hidden": "True",
        },
    )
    assert ungated is not None
    assert (ungated.include_patterns, ungated.exclude_patterns, ungated.extra_extensions) == (
        "('published/**',)",
        "('drafts/**',)",
        "('.mdx',)",
    )
    assert ungated.skip_hidden == "True"
    assert ungated.require_content_before_publish == ""
    gated = replace(ungated, require_content_before_publish="True")

    assert IndexingSettings.from_metadata(gated.to_metadata()) == gated
    assert ungated.corpus_compatibility_key() != gated.corpus_compatibility_key()
    assert ungated.query_compatibility_key() == gated.query_compatibility_key()
