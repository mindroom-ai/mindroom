"""The one rule that cuts every file inlined into the system prompt: context files and `MEMORY.md` alike."""

from __future__ import annotations


def cap_preload_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Return what of the file ``text`` the prompt inlines within ``max_chars`` characters, and whether it was cut.

    Nothing is stripped before counting. Text over the limit keeps its first whole lines that fit ``max_chars``, the line
    feed after the last kept line not counted; only a line feed ends a line. A first line longer than ``max_chars``
    keeps its first ``max_chars`` characters. The kept text depends only on ``text[: max_chars + 1]``, so text added
    after the cut never changes it.
    """
    if len(text) <= max_chars:
        return text, False
    end = text.rfind("\n", 0, max_chars + 1)
    return text[: end if end >= 0 else max_chars], True
