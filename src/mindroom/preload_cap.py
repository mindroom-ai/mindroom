"""The one rule that cuts every file inlined into the system prompt: context files and `MEMORY.md` alike."""

from __future__ import annotations

import codecs


def preload_text(payload: bytes, *, read_cut: bool) -> str:
    """Return the text a preloaded file's bytes hold, the file's surrounding whitespace stripped.

    A ``read_cut`` payload, the first bytes of a file longer than its read cap, keeps its partial last line, so the cut
    sees the file's first characters, and drops only a character the read split; its end is not stripped.
    """
    text = codecs.getincrementaldecoder("utf-8")(errors="replace").decode(payload, final=not read_cut)
    return text.lstrip() if read_cut else text.strip()


def cap_preload_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Return what of ``text`` the prompt inlines within ``max_chars`` characters, and whether anything was left out.

    Text over the limit keeps its first whole lines whose characters, each line with its line feed, fit ``max_chars``;
    only a line feed ends a line, so a carriage return counts as a character of its line. Only a first line longer
    than ``max_chars`` keeps its first ``max_chars`` characters. The kept text is exactly the file's first characters.
    Every file is cut on its own, and text added after the cut never changes what is kept, so a growing file leaves the
    prompt as it was.
    """
    if len(text) <= max_chars:
        return text, False
    end = text.rfind("\n", 0, max_chars)
    return text[: end if end >= 0 else max_chars], True
