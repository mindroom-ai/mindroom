"""Joining speech-provider transcript fragments into whole spoken turns."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

# Speech providers can close a transcript item mid-sentence, so the next one may start with punctuation.
_GLUED_PUNCTUATION = tuple(",.;:!?…)]}")


def spoken_continuation(fragment: str) -> str:
    """Return one normalized, non-empty speech fragment prefixed for appending to the same speaker's turn."""
    return fragment if fragment.startswith(_GLUED_PUNCTUATION) else f" {fragment}"


def merge_spoken_turns(turns: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    """Join consecutive normalized, non-empty fragments from the same speaker into one turn."""
    merged: list[tuple[str, str]] = []
    for role, text in turns:
        if merged and merged[-1][0] == role:
            merged[-1] = (role, merged[-1][1] + spoken_continuation(text))
        else:
            merged.append((role, text))
    return merged
