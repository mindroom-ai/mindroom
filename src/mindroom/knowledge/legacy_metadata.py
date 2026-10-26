"""Compatibility normalization for historical knowledge-index metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# LEGACY_COMPAT: Knowledge metadata without include/exclude filter identities.
# Legacy format: Knowledge-index metadata omitted base include_patterns and exclude_patterns.
# Last legacy release: v2026.6.41; replacement: v2026.6.42 persisted both filter identities.
# Handling: Normalize absent or empty filters to the current empty-filter identity without mutating input.
# Coverage: tests/test_knowledge_indexing_config.py::test_indexing_settings_from_metadata_normalizes_legacy_empty_filter_keys.

# LEGACY_COMPAT: Knowledge metadata without extra-extension identity.
# Legacy format: Semantic knowledge-index metadata omitted extra_extensions.
# Last legacy release: v2026.6.127; replacement: v2026.6.128 persisted the extension identity.
# Handling: Normalize absence to the semantic empty-filter identity; file-mode absence remains empty.
# Coverage: tests/test_knowledge_indexing_config.py::test_indexing_settings_from_metadata_normalizes_legacy_empty_filter_keys.

# LEGACY_COMPAT: Knowledge metadata without the effective hidden-file gate.
# Legacy format: Knowledge-index metadata omitted the effective non-Git skip_hidden gate.
# Last legacy release: v2026.7.162; replacement: v2026.7.163 persisted the gate in the corpus identity.
# Handling: Keep absence empty so a currently hidden-filtered corpus rebuilds while Git identity remains stable.
# Coverage: tests/test_knowledge_indexing_config.py::test_skip_hidden_changes_corpus_key_but_not_query_key.

# LEGACY_COMPAT: Knowledge metadata without the content-publication gate.
# Legacy format: Knowledge-index metadata omitted require_content_before_publish.
# Last legacy release: v2026.7.328; replacement: v2026.7.329 persisted the publication gate.
# Handling: Keep absence empty so gated runtime overlays invalidate old empty indexes; false stays compatible.
# Coverage: tests/test_knowledge_indexing_config.py::test_content_publication_gate_round_trips_and_changes_corpus_key.


# LEGACY_COMPAT: Knowledge metadata from the Chroma-backed store.
# Legacy format: Knowledge-index metadata omitted vector_store; its semantic vectors lived in Chroma.
# Last legacy release: v2026.10.552; replacement: v2026.10.553 stored semantic indexes in owned SQLite files.
# Handling: Keep absence empty so the query key no longer matches; the next refresh copies the vectors out of Chroma.
# Coverage: tests/test_knowledge_indexing_config.py::test_chroma_era_metadata_rebuilds_in_the_owned_store.

# LEGACY_COMPAT: Knowledge metadata without contextual chunk headers.
# Legacy format: Knowledge-index metadata omitted chunk_context_model.
# Last legacy release: v2026.10.552; replacement: v2026.10.553 persisted the chunk context model.
# Handling: Keep absence empty, which is the identity of indexes whose chunks were indexed as read.
# Coverage: tests/test_knowledge_indexing_config.py::test_chroma_era_metadata_rebuilds_in_the_owned_store.


def normalize_legacy_indexing_settings(
    settings: Mapping[str, str],
    *,
    empty_filter_key: str,
) -> dict[str, str]:
    """Fill optional historical settings with their current identity values.

    Unknown fields are deliberately retained so the current metadata parser can
    reject them. Missing boolean settings remain empty to force a rebuild when
    today's corpus identity differs from an index created before those settings.
    """
    normalized = dict(settings)
    for key in ("include_patterns", "exclude_patterns"):
        normalized[key] = _optional_filter_key(normalized, key, empty_value=empty_filter_key)

    extra_extensions_empty = empty_filter_key if normalized["mode"] == "semantic" else ""
    normalized["extra_extensions"] = _optional_filter_key(
        normalized,
        "extra_extensions",
        empty_value=extra_extensions_empty,
    )
    normalized.setdefault("skip_hidden", "")
    normalized.setdefault("require_content_before_publish", "")
    normalized.setdefault("vector_store", "")
    normalized.setdefault("chunk_context_model", "")
    return normalized


def _optional_filter_key(
    settings: Mapping[str, str],
    name: str,
    *,
    empty_value: str,
) -> str:
    """Normalize one absent or empty historical filter setting."""
    return settings.get(name, "") or empty_value
