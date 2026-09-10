"""Read body suffixes written before structured stream status existed."""

from __future__ import annotations

_LEGACY_TERMINAL_SUFFIXES = (" [cancelled]", " [error]")

# Legacy format: Body-only cancellation and error suffixes without stream_status.
# Last legacy release: v2026.3.121; replacement: v2026.3.122 wrote structured stream status.
# Handling: Detect and strip only the two bounded suffixes in their historical single-pass order.
# Coverage: tests/test_streaming_behavior.py::TestStreamingBehavior::test_clean_partial_reply_text_preserves_marker_stripping_order.


def has_legacy_terminal_suffix(text: str) -> bool:
    """Return whether text ends with an old body-only terminal suffix."""
    return text.endswith(_LEGACY_TERMINAL_SUFFIXES)


def strip_legacy_terminal_suffixes(text: str) -> str:
    """Strip each old body-only terminal suffix once in historical order."""
    for suffix in _LEGACY_TERMINAL_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)].rstrip()
    return text
