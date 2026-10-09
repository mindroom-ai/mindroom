"""Read delivery payload fields written before local outbox results existed."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from mindroom.constants import DURABLE_FINAL_OUTCOME_KEY, DURABLE_FINAL_OUTCOME_VERSION

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

# LEGACY_COMPAT: FINAL outcomes embedded directly in Matrix payloads.
# Legacy format: Substantive final outcomes inline in Matrix payloads, including m.new_content edits.
# Last legacy release: v2026.8.88; replacement: v2026.8.89 added local result_json.
# Handling: Prefer the visible replacement outcome; substantive inline data wins, while a marker defers to local data.
# Coverage: tests/test_event_journal_store.py::TestOutbox::test_legacy_inline_edit_prefers_visible_replacement_outcome.


def _inline_final_result(payload: Mapping[str, object]) -> dict[str, object] | None:
    """Read an old inline outcome, preferring an edit's visible replacement."""
    replacement = payload.get("m.new_content")
    value = (
        cast("dict[str, object]", replacement).get(DURABLE_FINAL_OUTCOME_KEY)
        if isinstance(replacement, dict)
        else payload.get(
            DURABLE_FINAL_OUTCOME_KEY,
        )
    )
    return dict(cast("Mapping[str, object]", value)) if isinstance(value, dict) else None


# LEGACY_COMPAT: Version-only FINAL outcome markers in Matrix payloads.
# Legacy format: io.mindroom.final_delivery = {"version": 2} on a FINAL payload, beside the outcome in result_json.
# Last legacy release: v2026.10.214 (written since v2026.8.89); replacement: the unreleased durable reply messages
# write no wire marker.
# Handling: A marker carries no outcome, so the local result_json decides, and sanitizing keeps the marker in place.
# Coverage: tests/test_event_journal_store.py::TestOutbox::test_a_compatibility_marker_does_not_replace_the_local_result.
def _is_compatibility_marker(result: Mapping[str, object]) -> bool:
    """Return whether a version-only old-reader marker carries no outcome."""
    version = result.get("version")
    return set(result) == {"version"} and isinstance(version, int) and not isinstance(version, bool)


def decode_delivery_result(
    payload: Mapping[str, object],
    raw_result: str | None,
    *,
    delivery_id: str,
) -> dict[str, object] | None:
    """Decode current local results and older inline outcomes with their original precedence."""
    inline_result = _inline_final_result(payload)
    if (inline_result is not None and not _is_compatibility_marker(inline_result)) or raw_result is None:
        return inline_result

    decoded_result = json.loads(raw_result)
    if not isinstance(decoded_result, dict):
        msg = f"Outbox result for delivery {delivery_id!r} is not an object"
        raise TypeError(msg)
    return cast("dict[str, object]", decoded_result)


def without_inline_final_result(content: dict[str, Any]) -> dict[str, Any]:
    """Remove substantive old inline outcomes while retaining the bounded marker."""
    replacement = content.get("m.new_content")
    compatibility_marker = {"version": DURABLE_FINAL_OUTCOME_VERSION}
    outer_has_result = (
        DURABLE_FINAL_OUTCOME_KEY in content and content[DURABLE_FINAL_OUTCOME_KEY] != compatibility_marker
    )
    nested_has_result = (
        isinstance(replacement, dict)
        and DURABLE_FINAL_OUTCOME_KEY in replacement
        and replacement[DURABLE_FINAL_OUTCOME_KEY] != compatibility_marker
    )
    if not outer_has_result and not nested_has_result:
        return content

    sanitized = dict(content)
    if outer_has_result:
        sanitized.pop(DURABLE_FINAL_OUTCOME_KEY, None)
    if isinstance(replacement, dict):
        sanitized_replacement = dict(replacement)
        if nested_has_result:
            sanitized_replacement.pop(DURABLE_FINAL_OUTCOME_KEY, None)
        sanitized["m.new_content"] = sanitized_replacement
    return sanitized
