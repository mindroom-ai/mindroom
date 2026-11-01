"""Count tokens with the local estimate of the route whose model receives the text.

The estimate is the model's own tokenizer where it runs locally, and an upper bound elsewhere; no count waits for the
network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.llm.provider import TokenCounter

__all__ = ["count", "fit_text", "opaque_tokens", "utf8_byte_bound"]


def opaque_tokens(characters: int) -> int:
    """Guard the size of ``characters`` of ciphertext at a quarter token each.

    No tokenizer reads ciphertext: the provider counts the text it decrypts to, which its length only bounds.
    """
    return (characters + 3) // 4


def utf8_byte_bound(text: str) -> int:
    """Return the UTF-8 byte count of ``text``, which no byte-level tokenizer's token count exceeds."""
    return len(text.encode("utf-8", "surrogatepass"))


def count(counter: TokenCounter, text: str, *, upper_bound: bool = False) -> int:
    """Count ``text`` for ``counter``'s model with its local estimate.

    While the counter's encoder is unavailable, only briefly after startup or for good when its vocabulary cannot be
    loaded, a size that must not undercount (``upper_bound``) takes the UTF-8 byte count, an upper bound; any other
    takes a quarter token per character, rounded up so no text is free, since the byte count would triple history
    estimates meanwhile and compact conversations that fit.
    """
    if not text:
        return 0
    tokens = counter.estimate(text)
    if tokens is not None:
        return tokens
    return utf8_byte_bound(text) if upper_bound else (len(text) + 3) // 4


def fit_text(
    text: str,
    max_tokens: int,
    count_tokens: Callable[[str], int],
    *,
    notice: str = "",
    keep: Literal["start", "end"] = "start",
) -> str:
    """Return ``text``, or its longest start (or end) found that fits ``max_tokens`` together with ``notice``.

    ``notice`` follows a kept start and precedes a kept end. Every returned value was measured with ``count_tokens``;
    when not even ``notice`` alone fits, the result is empty.
    """
    measured = count_tokens(text)
    if measured <= max_tokens:
        return text
    kept = len(text)
    while kept:
        kept = min(int(kept * 0.95), int(kept * max_tokens / max(measured, 1)))
        candidate = text[:kept] + notice if keep == "start" else notice + text[len(text) - kept :]
        measured = count_tokens(candidate)
        if measured <= max_tokens:
            return candidate
    return ""
