"""The one rule that cuts every file inlined into the system prompt: context files and `MEMORY.md` alike."""

from __future__ import annotations


def cap_preload_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Return what of ``text`` the prompt inlines within ``max_chars`` characters, and whether anything was left out.

    Text over the limit keeps its first whole lines whose characters, each line with its newline, fit ``max_chars``;
    only a first line longer than ``max_chars`` keeps its first ``max_chars`` characters. The kept text loses its
    trailing whitespace. Every file is cut on its own, and text added after the cut never changes what is kept, so a
    growing file leaves the prompt as it was.
    """
    if len(text) <= max_chars:
        return text, False
    end = text.rfind("\n", 0, max_chars)
    return text[: end if end >= 0 else max_chars].rstrip(), True
