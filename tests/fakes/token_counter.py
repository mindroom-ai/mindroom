"""A token counter that estimates a quarter token per character and counts remotely only through a given function."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.llm.provider import TokenCounter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.llm.provider import PortableReplayEstimator


def quarter_tokens(text: str) -> int:
    """Estimate a quarter token per character, so tests can state sizes as character counts."""
    return len(text) // 4


def fake_token_counter(
    *,
    key: str = "fake",
    count: Callable[[str], Awaitable[int]] | None = None,
    replay: PortableReplayEstimator | None = None,
    framed: bool = False,
) -> TokenCounter:
    """Return a counter estimating ``quarter_tokens``; ``count`` stands in for a provider's counting endpoint."""
    return TokenCounter(key=key, estimate=quarter_tokens, count=count, replay=replay, framed=framed)


__all__ = ["fake_token_counter", "quarter_tokens"]
