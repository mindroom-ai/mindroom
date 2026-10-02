"""Historical shared replay file for external triggers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

_SECTIONS = ("nonces", "events", "threads")


# LEGACY_COMPAT: One external-trigger replay file shared by every replay scope.
# Legacy format: `<control-state>/external_triggers/replay.json` keyed its nonce, event, and thread-key sections by replay scope.
# Last legacy release: v2026.10.40; replacement: the next release keeps each scope in its own file under `external_triggers/replay/`.
# Handling: The first replay store call splits a present file into one current file per scope under the old lock, then deletes it; a malformed file fails closed until repaired, and the next trigger record write removes the files of scopes whose trigger was deleted or rotated.
# Coverage: tests/test_external_trigger_replay_store.py::test_shared_replay_file_is_split_into_scope_files.
def split_shared_replay_store(raw_store: object) -> dict[str, dict[str, object]] | None:
    """Return each scope's unvalidated sections from the shared replay file, or None for a malformed file."""
    if not isinstance(raw_store, Mapping):
        return None
    store_mapping = cast("Mapping[object, object]", raw_store)
    if "nonces" not in store_mapping or "events" not in store_mapping:
        return None
    scopes: dict[str, dict[str, object]] = {}
    for section in _SECTIONS:
        # LEGACY_COMPAT: External-trigger replay claims without thread-key tracking.
        # Legacy format: External-trigger replay stores contained nonce and event claims but no threads section.
        # Last legacy release: v2026.9.20; replacement: v2026.9.21 persisted thread-key claims.
        # Handling: Treat the missing section as empty and preserve existing dedup claims when the file is split.
        # Coverage: tests/test_external_trigger_replay_store.py::test_store_without_threads_section_is_accepted.
        raw_section = store_mapping.get(section, {})
        if not isinstance(raw_section, Mapping):
            return None
        for scope, records in cast("Mapping[object, object]", raw_section).items():
            if not isinstance(scope, str):
                return None
            scopes.setdefault(scope, {name: {} for name in _SECTIONS})[section] = records
    return scopes
