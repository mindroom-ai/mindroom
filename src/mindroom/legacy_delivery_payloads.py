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
# Last legacy release: v2026.8.88; replacement: v2026.8.89 added local result_json and a wire marker.
# Handling: Prefer the visible replacement outcome; substantive inline data wins, while a marker defers to local data.
# Coverage: tests/test_event_journal_store.py::test_legacy_inline_edit_prefers_visible_replacement_outcome.


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


# LEGACY_COMPAT: Regeneration answers queued before reply records, carrying their selected edit.
# Legacy format: a FINAL row with no reply_id, keyed by the edit event, whose result_json holds the regeneration's
# prepared_edit_record.
# Last legacy release: v2026.10.201; replacement: the unreleased durable reply messages keep the selected edit
# on the regeneration span and commit it when the answer settles its sources.
# Handling: reply classification makes an owed row the next write of the reply it edits, on a regeneration span
# carrying that edit, which it commits as a regeneration's terminal row does now; a newer edit waits for its delivery.
# Coverage: tests/test_legacy_reply_messages.py::test_edit_answers_still_in_flight_are_written_by_their_reply.
def legacy_prepared_edit(result: Mapping[str, object] | None) -> tuple[str, dict[str, object]] | None:
    """Return the selected edit an earlier release's regeneration FINAL carries, with the source it is stored under."""
    prepared = (result or {}).get("prepared_edit_record")
    if prepared is None:
        return None
    assert isinstance(prepared, dict), "Corrupt prepared edit record"
    stored = cast("dict[str, object]", prepared)
    sources = stored.get("source_event_ids")
    assert isinstance(sources, list), "Corrupt prepared edit sources"
    assert sources, "Empty prepared edit sources"
    assert isinstance(sources[0], str), "Corrupt prepared edit source"
    return sources[0], stored
