"""Count tokens through the counter of the route whose model receives the text, labelling how each count was made.

A count is exact when the model's own tokenizer runs locally or its provider counted the same text before; calibrated
when the counter's estimate is scaled by what its provider counted for other text; otherwise estimated. Replies never
wait for a provider: ``count`` reads only local state, and ``prefetch`` counts in the background, so a later reply
finds the exact count cached. Counts live in this process's memory.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import threading
import time
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Literal

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.llm.provider import TokenCounter

__all__ = ["TokenCount", "TokenCountKind", "count", "count_exact", "fit_text", "prefetch", "text_tokens"]

logger = get_logger(__name__)

type TokenCountKind = Literal["exact", "calibrated", "estimated"]
_KINDS_WEAKEST_FIRST: tuple[TokenCountKind, ...] = ("estimated", "calibrated", "exact")

# One provider count, the framing count included, may take this long before the local count stands in.
_TIMEOUT_SECONDS = 10.0
# After a failed provider count, the counter's key counts locally for this long.
_COOLDOWN_SECONDS = 300.0
# Background counts in flight at once; further prefetches wait for a later reply.
_MAX_PENDING = 8
_MAX_CACHED = 4096
# Each calibration sample weighs this much less than the next one.
_DECAY = 0.9


@dataclass(frozen=True, slots=True)
class TokenCount:
    """A token count and how it was made; a sum is only as good as its weakest part."""

    tokens: int
    kind: TokenCountKind

    def __add__(self, other: TokenCount) -> TokenCount:
        """Add the counts, keeping the weaker kind."""
        return TokenCount(self.tokens + other.tokens, min(self.kind, other.kind, key=_KINDS_WEAKEST_FIRST.index))


type _TextKey = tuple[str, bytes]


@dataclass
class _Counts:
    """The process's exact counts, calibration, cooldowns, and background counts; the lock guards only updates."""

    exact: dict[_TextKey, int] = field(default_factory=dict)
    # A counter key's decayed sums of provider counts and of its estimates for the same texts.
    ratios: dict[str, tuple[float, float]] = field(default_factory=dict)
    cooling_until: dict[str, float] = field(default_factory=dict)
    pending: dict[_TextKey, asyncio.Task[TokenCount]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = _Counts()


def _text_key(counter: TokenCounter, text: str) -> _TextKey:
    return counter.key, hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()


def _utf8_bytes(text: str) -> TokenCount:
    """Count UTF-8 bytes, which no byte-level tokenizer exceeds, while the counter's estimate is unavailable."""
    return TokenCount(len(text.encode("utf-8", "surrogatepass")), "estimated")


def count(counter: TokenCounter, text: str) -> TokenCount:
    """Count ``text`` for ``counter``'s model from local state only, never over the network."""
    if not text:
        return TokenCount(0, "exact")
    if counter.exact_locally:
        tokens = counter.estimate(text)
        return _utf8_bytes(text) if tokens is None else TokenCount(tokens, "exact")
    state = _state
    with state.lock:
        exact = state.exact.get(_text_key(counter, text))
        ratio = state.ratios.get(counter.key)
    if exact is not None:
        return TokenCount(exact, "exact")
    estimate = counter.estimate(text)
    if estimate is None:
        return _utf8_bytes(text)
    if ratio is not None:
        counted, estimated = ratio
        return TokenCount(math.ceil(estimate * counted / estimated), "calibrated")
    return TokenCount(estimate, "estimated")


def _cooling(counter: TokenCounter) -> bool:
    with _state.lock:
        return _state.cooling_until.get(counter.key, 0.0) > time.monotonic()


async def count_exact(counter: TokenCounter, text: str) -> TokenCount:
    """Count ``text`` through ``counter``'s provider unless a local count is exact; a failure counts locally.

    Only background work awaits this: a reply uses ``count``.
    """
    local = count(counter, text)
    if local.kind == "exact" or counter.count is None or not text.strip() or _cooling(counter):
        return local
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            tokens = await counter.count(text)
    # Any network or provider failure leaves the local count standing.
    except Exception as error:
        with _state.lock:
            _state.cooling_until[counter.key] = time.monotonic() + _COOLDOWN_SECONDS
        logger.warning("Token count failed", key=counter.key, fallback_kind=local.kind, error=str(error))
        return local
    estimate = counter.estimate(text)
    state = _state
    with state.lock:
        state.exact[_text_key(counter, text)] = tokens
        if len(state.exact) > _MAX_CACHED:
            del state.exact[next(iter(state.exact))]
        # A byte count stands in only until the estimate loads, so it never calibrates the estimate.
        if estimate:
            counted, estimated = state.ratios.get(counter.key, (0.0, 0.0))
            state.ratios[counter.key] = (counted * _DECAY + tokens, estimated * _DECAY + estimate)
    return TokenCount(tokens, "exact")


def _forget(key: _TextKey, _task: asyncio.Task[TokenCount]) -> None:
    with _state.lock:
        _state.pending.pop(key, None)


def prefetch(counter: TokenCounter, *texts: str) -> None:
    """Count ``texts`` through ``counter``'s provider in the background, so later replies read exact counts.

    Call it on the event loop; it never waits for a count.
    """
    if counter.count is None or counter.exact_locally or _cooling(counter):
        return
    loop = asyncio.get_running_loop()
    state = _state
    for text in texts:
        if not text.strip():
            continue
        key = _text_key(counter, text)
        with state.lock:
            if key in state.exact or key in state.pending or len(state.pending) >= _MAX_PENDING:
                continue
            task = state.pending[key] = loop.create_task(count_exact(counter, text))
        task.add_done_callback(partial(_forget, key))


async def text_tokens(count_message: Callable[[str], Awaitable[int]], text: str) -> int:
    """Count ``text`` through an endpoint that counts it as a whole message, without that message's framing.

    The endpoint also counts a one-character message, whose one token leaves only the framing; each route that
    counts this way has a tokenizer that encodes ``"a"`` as one token.
    """
    tokens, framed_one = await asyncio.gather(count_message(text), count_message("a"))
    return max(0, tokens - (framed_one - 1))


def fit_text(text: str, max_tokens: int, count_tokens: Callable[[str], int], *, notice: str = "") -> str:
    """Return ``text``, or its longest prefix found that fits ``max_tokens`` together with ``notice``.

    Every returned value was measured with ``count_tokens``; when not even ``notice`` alone fits, the result is empty.
    """
    measured = count_tokens(text)
    if measured <= max_tokens:
        return text
    kept = len(text)
    while kept:
        kept = min(int(kept * 0.95), int(kept * max_tokens / max(measured, 1)))
        candidate = text[:kept] + notice
        measured = count_tokens(candidate)
        if measured <= max_tokens:
            return candidate
    return ""
