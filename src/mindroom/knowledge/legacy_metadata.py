"""Compatibility normalization for historical knowledge-index metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# Legacy format: Knowledge-index metadata omitted base include_patterns and exclude_patterns.
# Last legacy release: v2026.6.41; replacement: v2026.6.42 persisted both filter identities.
# Handling: Normalize absent or empty filters to the current empty-filter identity without mutating input.
# Coverage: tests/test_knowledge_indexing_config.py::test_indexing_settings_from_metadata_normalizes_legacy_empty_filter_keys.

# Legacy format: Semantic knowledge-index metadata omitted extra_extensions.
# Last legacy release: v2026.6.127; replacement: v2026.6.128 persisted the extension identity.
# Handling: Normalize absence to the semantic empty-filter identity; file-mode absence remains empty.
# Coverage: tests/test_knowledge_indexing_config.py::test_indexing_settings_from_metadata_normalizes_legacy_empty_filter_keys.

# Legacy format: Knowledge-index metadata omitted the effective non-Git skip_hidden gate.
# Last legacy release: v2026.7.162; replacement: v2026.7.163 persisted the gate in the corpus identity.
# Handling: Keep absence empty so a currently hidden-filtered corpus rebuilds while Git identity remains stable.
# Coverage: tests/test_knowledge_indexing_config.py::test_skip_hidden_changes_corpus_key_but_not_query_key.

# Legacy format: Knowledge-index metadata omitted require_content_before_publish.
# Last legacy release: v2026.7.328; replacement: v2026.7.329 persisted the publication gate.
# Handling: Keep absence empty so gated runtime overlays invalidate old empty indexes; false stays compatible.
# Coverage: tests/test_knowledge_indexing_config.py::test_content_publication_gate_round_trips_and_changes_corpus_key.


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
    return normalized


def _optional_filter_key(
    settings: Mapping[str, str],
    name: str,
    *,
    empty_value: str,
) -> str:
    """Normalize one absent or empty historical filter setting."""
    return settings.get(name, "") or empty_value
