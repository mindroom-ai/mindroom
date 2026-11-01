"""Count tokens through the counter of the route whose model receives the text, labelling how each count was made.

A count is exact when the model's own tokenizer runs locally or its provider counted the same text; otherwise it is
the counter's estimate, an upper bound. ``measure`` has the provider count texts a reply sizes, waiting for it, and
``count`` reads that exact count from then on. Counts live in this process's memory.
"""

from __future__ import annotations

import asyncio
import hashlib
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
    "count",
    "count_exact",
    "estimated",
    "fit_text",
    "measure",
    "opaque_tokens",
    "text_tokens",
    "utf8_byte_bound",
]

logger = get_logger(__name__)

type TokenCountKind = Literal["exact", "estimated"]

# One provider count, the framing count included, may take this long before the local count stands in.
_TIMEOUT_SECONDS = 5.0
# After a failed provider count, the counter's key counts locally for this long.
_COOLDOWN_SECONDS = 300.0
_MAX_CACHED = 4096


@dataclass(frozen=True, slots=True)
class TokenCount:
    """A token count and how it was made."""

    tokens: int
    kind: TokenCountKind


type _TextKey = tuple[str, bytes]


@dataclass
class _Counts:
    """The process's exact counts, cooldowns, and provider counts in flight; the lock guards only updates."""

    exact: dict[_TextKey, int] = field(default_factory=dict)
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


def _without_encoder(text: str, *, upper_bound: bool) -> TokenCount:
    """Estimate ``text`` while the counter's encoder is unavailable, as ``count``'s ``upper_bound`` asks.

    That is only briefly after startup, or for good when its vocabulary cannot be loaded. A size that must not
    undercount takes the UTF-8 byte count, an upper bound; any other takes a quarter token per character, rounded up
    so no text is free, since the byte count would triple history estimates meanwhile and compact conversations that
    fit.
    """
    return TokenCount(utf8_byte_bound(text) if upper_bound else (len(text) + 3) // 4, "estimated")


def count(counter: TokenCounter, text: str, *, upper_bound: bool = False) -> TokenCount:
    """Count ``text`` for ``counter``'s model from local state only, never over the network.

    ``upper_bound`` asks for a size that must not undercount even while the counter's encoder is unavailable.
    """
    if not text:
        return TokenCount(0, "exact")
    if counter.exact_locally:
        tokens = counter.estimate(text)
        return _without_encoder(text, upper_bound=upper_bound) if tokens is None else TokenCount(tokens, "exact")
    with _state.lock:
        exact = _state.exact.get(_text_key(counter, text))
    if exact is not None:
        return TokenCount(exact, "exact")
    estimate = counter.estimate(text)
    if estimate is None:
        return _without_encoder(text, upper_bound=upper_bound)
    return TokenCount(estimate, "estimated")


def estimated(counter: TokenCounter) -> TokenCounter:
    """Return ``counter`` counting by its local estimate alone, never by provider counts cached for its key.

    Sizes measured at two times then compare on one basis, even when the provider counted one of the texts between.
    """
    return replace(counter, key=f"{counter.key}#estimated", count=None)


def _cooling(counter: TokenCounter) -> bool:
    with _state.lock:
        return _state.cooling_until.get(counter.key, 0.0) > time.monotonic()


async def count_exact(counter: TokenCounter, text: str) -> TokenCount:
    """Count ``text`` through ``counter``'s provider unless a local count is exact; a failure counts locally.

    Encoding runs in a worker thread, so a long text never stalls the event loop.
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
    with _state.lock:
        _state.failing.discard(counter.key)
        _state.exact[_text_key(counter, text)] = tokens
        if len(_state.exact) > _MAX_CACHED:
            del _state.exact[next(iter(_state.exact))]
    return TokenCount(tokens, "exact")


def _forget(key: _TextKey, _task: asyncio.Task[TokenCount]) -> None:
    with _state.lock:
        _state.pending.pop(key, None)


async def measure(counter: TokenCounter, *texts: str) -> None:
    """Have ``counter``'s provider count each of ``texts`` it has not counted yet, so ``count`` reads exact counts.

    A reply awaits this for the text it sizes. Each count has the time limit and the route's pause after a failure, a
    count runs on even when the reply that asked is cancelled, and concurrent requests for one text share one count.
    """
    if counter.count is None or counter.exact_locally or _cooling(counter):
        return
    loop = asyncio.get_running_loop()
    tasks: list[asyncio.Task[TokenCount]] = []
    for text in dict.fromkeys(texts):
        if not text.strip():
            continue
        key = _text_key(counter, text)
        with _state.lock:
            if key in _state.exact:
                continue
            task = _state.pending.get(key)
            if task is None:
                task = _state.pending[key] = loop.create_task(count_exact(counter, text))
                task.add_done_callback(partial(_forget, key))
        tasks.append(task)
    if tasks:
        await asyncio.wait(tasks)


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
