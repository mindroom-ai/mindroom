"""Read historical file replacements whose fallback omitted its media URL."""

from __future__ import annotations

import nio

# Legacy format: m.file replacement events with a URL only inside m.new_content.
# Last legacy release: v2026.8.87; replacement: v2026.8.88 writes valid outer media fallbacks.
# Handling: Supply the validated replacement URL only for parsing; preserve the original source for projection.
# Coverage: tests/test_legacy_media_edits.py::test_legacy_file_edit_is_readable_without_changing_source.


def readable_legacy_file_edit(event: nio.BaseEvent) -> nio.RoomMessageFile | None:
    """Restore only a valid file replacement, never an arbitrary malformed event."""
    if not isinstance(event, nio.BadEvent):
        return None
    source = event.source
    content = source.get("content")
    if (
        source.get("type") != "m.room.message"
        or not isinstance(content, dict)
        or content.get("msgtype") != "m.file"
        or "url" in content
        or "file" in content
    ):
        return None
    relation = content.get("m.relates_to")
    replacement = content.get("m.new_content")
    if (
        not isinstance(relation, dict)
        or relation.get("rel_type") != "m.replace"
        or not isinstance(relation.get("event_id"), str)
        or not relation["event_id"]
        or not isinstance(replacement, dict)
        or replacement.get("msgtype") != "m.file"
    ):
        return None
    parsed_replacement = nio.Event.parse_event({**source, "content": replacement})
    if not isinstance(parsed_replacement, nio.RoomMessageFile):
        return None
    parsed = nio.Event.parse_event({**source, "content": {**content, "url": parsed_replacement.url}})
    if not isinstance(parsed, nio.RoomMessageFile):
        return None
    parsed.source = source
    return parsed
