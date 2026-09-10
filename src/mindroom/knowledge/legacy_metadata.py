"""Compatibility normalization for historical knowledge-index metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


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
