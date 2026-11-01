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
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Literal

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.llm.provider import TokenCounter

__all__ = [
    "TokenCount",
    "TokenCountKind",
    "cached_count",
    "count",
    "count_exact",
    "fit_text",
    "frozen",
    "opaque_tokens",
    "prefetch",
    "text_tokens",
    "utf8_byte_bound",
]

logger = get_logger(__name__)

type TokenCountKind = Literal["exact", "calibrated", "estimated"]

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
    """A token count and how it was made."""

    tokens: int
    kind: TokenCountKind


type _TextKey = tuple[str, bytes]


@dataclass
class _Counts:
    """The process's exact counts, calibration, cooldowns, and background counts; the lock guards only updates."""

    exact: dict[_TextKey, int] = field(default_factory=dict)
    # A counter key's decayed sums of provider counts and of its estimates for the same texts.
    ratios: dict[str, tuple[float, float]] = field(default_factory=dict)
    cooling_until: dict[str, float] = field(default_factory=dict)
    # Counter keys whose latest provider count failed, so a streak of failures warns once.
    failing: set[str] = field(default_factory=set)
    pending: dict[_TextKey, asyncio.Task[TokenCount]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_state = _Counts()


def _text_key(counter: TokenCounter, text: str) -> _TextKey:
    return counter.key, hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()


def opaque_tokens(characters: int) -> int:
    """Guard the size of ``characters`` of ciphertext at a quarter token each.

    No tokenizer reads ciphertext: the provider counts the text it decrypts to, which its length only bounds.
    """
    return (characters + 3) // 4


def utf8_byte_bound(text: str) -> int:
    """Return the UTF-8 byte count of ``text``, which no byte-level tokenizer's token count exceeds."""
    return len(text.encode("utf-8", "surrogatepass"))


def _without_encoder(text: str) -> TokenCount:
    """Estimate a quarter token per character while the counter's encoder is unavailable.

    That is only briefly after startup, or for good when its vocabulary cannot be loaded; an upper bound such as the
    UTF-8 byte count would triple history estimates meanwhile and compact conversations that fit.
    """
    return TokenCount(len(text) // 4, "estimated")


def cached_count(counter: TokenCounter, text: str) -> int | None:
    """Return the exact count of ``text`` its provider made for ``counter``'s key, or None when it has not counted it."""
    key = _text_key(counter, text)
    with _state.lock:
        return _state.exact.get(key)


def count(counter: TokenCounter, text: str, *, calibrated: bool = True) -> TokenCount:
    """Count ``text`` for ``counter``'s model from local state only, never over the network.

    ``calibrated=False`` keeps an estimate's family margin, for a size that must not undercount: one ratio per route
    cannot fit every text's content.
    """
    if not text:
        return TokenCount(0, "exact")
    if counter.exact_locally:
        tokens = counter.estimate(text)
        return _without_encoder(text) if tokens is None else TokenCount(tokens, "exact")
    key = _text_key(counter, text)
    state = _state
    with state.lock:
        exact = state.exact.get(key)
        ratio = state.ratios.get(counter.key)
    if exact is not None:
        return TokenCount(exact, "exact")
    estimate = counter.estimate(text)
    if estimate is None:
        return _without_encoder(text)
    if ratio is not None and calibrated:
        counted, estimated = ratio
        return TokenCount(math.ceil(estimate * counted / estimated), "calibrated")
    return TokenCount(estimate, "estimated")


def _cooling(counter: TokenCounter) -> bool:
    with _state.lock:
        return _state.cooling_until.get(counter.key, 0.0) > time.monotonic()


async def count_exact(counter: TokenCounter, text: str) -> TokenCount:
    """Count ``text`` through ``counter``'s provider unless a local count is exact; a failure counts locally.

    Only background work awaits this: a reply uses ``count``. Encoding runs in a worker thread, so a long text never
    stalls the event loop.
    """
    local = await asyncio.to_thread(count, counter, text)
    if local.kind == "exact" or counter.count is None or not text.strip() or _cooling(counter):
        return local
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            tokens = await counter.count(text)
    # Any network or provider failure leaves the local count standing.
    except Exception as error:
        with _state.lock:
            _state.cooling_until[counter.key] = time.monotonic() + _COOLDOWN_SECONDS
            streak = counter.key in _state.failing
            _state.failing.add(counter.key)
        log = logger.debug if streak else logger.warning
        log("Token count failed", key=counter.key, fallback_kind=local.kind, error=str(error))
        return local
    estimate = await asyncio.to_thread(counter.estimate, text)
    key = _text_key(counter, text)
    state = _state
    with state.lock:
        state.failing.discard(counter.key)
        state.exact[key] = tokens
        if len(state.exact) > _MAX_CACHED:
            del state.exact[next(iter(state.exact))]
        # Calibration scales the counter's own estimate, never the stand-in used before its encoder loads.
        if estimate:
            counted, estimated = state.ratios.get(counter.key, (0.0, 0.0))
            state.ratios[counter.key] = (counted * _DECAY + tokens, estimated * _DECAY + estimate)
    return TokenCount(tokens, "exact")


def frozen(counter: TokenCounter) -> TokenCounter:
    """Return a counter that estimates as ``counter`` does now, calibration and encoder included, and never changes.

    Sizes measured at two times compare only on one basis; later provider counts would move a live calibration, and
    an encoder that finishes loading in between would replace the stand-in.
    """
    scale = 1.0
    if not counter.exact_locally:
        with _state.lock:
            counted, estimated = _state.ratios.get(counter.key, (1.0, 1.0))
        scale = counted / estimated
    loaded = counter.estimate("a") is not None

    def estimate(text: str) -> int | None:
        tokens = counter.estimate(text) if loaded else None
        return None if tokens is None else math.ceil(tokens * scale)

    return replace(counter, key=f"{counter.key}#frozen:{scale}:{loaded}", estimate=estimate, count=None)


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
