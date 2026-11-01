"""A token counter that estimates a quarter token per character."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.llm.provider import TokenCounter

if TYPE_CHECKING:
    from mindroom.llm.provider import PortableReplayEstimator


def quarter_tokens(text: str) -> int:
    """Estimate a quarter token per character, so tests can state sizes as character counts."""
    return len(text) // 4


def fake_token_counter(*, replay: PortableReplayEstimator | None = None) -> TokenCounter:
    """Return a counter estimating ``quarter_tokens``."""
    return TokenCounter(estimate=quarter_tokens, replay=replay)


__all__ = ["fake_token_counter", "quarter_tokens"]
