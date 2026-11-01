"""The local counter of each model: its own tokenizer where one runs locally, otherwise a scaled o200k_base estimate.

o200k_base loads in a background thread on first use, because tiktoken downloads its vocabulary then, without a
timeout, and no reply may wait for that. Until it has loaded, or when it cannot load, estimates are None and counts
use a stand-in (``token_counting.counts``).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING

import tiktoken
import tiktoken.model

from mindroom.llm.provider import ModelIdentity, TokenCounter
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.llm.provider import PortableReplayEstimator

__all__ = ["counter_for_model", "load_o200k", "openai_model_counter"]

logger = get_logger(__name__)

_O200K = "o200k_base"
# o200k_harmony adds only special tokens, which plain text never encodes, so o200k_base counts its text exactly.
_O200K_ENCODINGS = frozenset({"o200k_base", "o200k_harmony"})
# How many tokens a family counts per o200k_base token, at most, over the samples in docs/architecture/token-counting.md.
_CLAUDE_MARGIN = 1.8
_GEMINI_MARGIN = 1.35
_OPENAI_MARGIN = 1.0
_OTHER_MARGIN = 1.25
_OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-", "codex-")
# tiktoken's cost grows with the square of a run without whitespace, so text is encoded in pieces of at most this many
# characters, each cut after a newline or before a word where one is near; a hard cut can add a token.
_PIECE_CHARS = 8_192


@dataclass
class _Encoder:
    """The o200k_base encoder once loaded, and whether its background load started."""

    value: tiktoken.Encoding | None = None
    started: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


_o200k_encoder = _Encoder()


def load_o200k() -> tiktoken.Encoding:
    """Return the o200k_base encoder, loading it first; this blocks, and the first load may download its vocabulary."""
    encoder = _o200k_encoder
    if encoder.value is None:
        with encoder.lock:
            if encoder.value is None:
                encoder.value = tiktoken.get_encoding(_O200K)
    return encoder.value


def _load_in_background() -> None:
    try:
        load_o200k()
    except Exception:
        logger.warning(
            "o200k_base did not load; token estimates use a quarter token per character, and compaction input its bytes",
            exc_info=True,
        )


def _loaded_o200k() -> tiktoken.Encoding | None:
    """Return the encoder once loaded; the first call starts its load in a background thread and returns None."""
    encoder = _o200k_encoder
    if encoder.value is None and not encoder.started:
        encoder.started = True
        threading.Thread(target=_load_in_background, name="o200k-loader", daemon=True).start()
    return encoder.value


def _piece_end(text: str, start: int, end: int) -> int:
    """Return where a piece starting at ``start`` ends, at most at ``end``: just before a word, after a newline or a space.

    o200k_base starts a new token there, so pieces cut there count as one encode does; a stretch without such a
    place is cut at ``end``, which can add a token.
    """
    newline = text.rfind("\n", start, end - 1)
    while newline >= start and text[newline + 1].isspace():
        newline = text.rfind("\n", start, newline)
    if newline >= start:
        return newline + 1
    space = text.rfind(" ", start + 1, end - 1)
    while space > start and text[space + 1].isspace():
        space = text.rfind(" ", start + 1, space)
    return space if space > start else end


def _encoded_length(encoder: tiktoken.Encoding, text: str) -> int:
    total = 0
    start = 0
    while start < len(text):
        end = start + _PIECE_CHARS
        if end < len(text):
            end = _piece_end(text, start, end)
        total += len(encoder.encode(text[start:end], disallowed_special=()))
        start = end
    return total


def _o200k_tokens(text: str) -> int | None:
    encoder = _loaded_o200k()
    return None if encoder is None else _encoded_length(encoder, text)


def _scaled_o200k_tokens(margin: float, text: str) -> int | None:
    tokens = _o200k_tokens(text)
    return None if tokens is None else math.ceil(tokens * margin)


def _maps_to_o200k(name: str) -> bool:
    try:
        return tiktoken.model.encoding_name_for_model(name) in _O200K_ENCODINGS
    except KeyError:
        return False


def _margin(name: str) -> float:
    if "claude" in name:
        return _CLAUDE_MARGIN
    if "gemini" in name:
        return _GEMINI_MARGIN
    if name.startswith(_OPENAI_PREFIXES):
        return _OPENAI_MARGIN
    return _OTHER_MARGIN


def counter_for_model(
    identity: ModelIdentity,
    *,
    local_encoder: bool = False,
    count: Callable[[str], Awaitable[int]] | None = None,
    replay: PortableReplayEstimator | None = None,
    endpoint: str | None = None,
) -> TokenCounter:
    """Return the counter of the model ``identity`` names, counting through ``count`` when no local encoder matches.

    ``local_encoder`` says the route serves OpenAI's own models, so the tokenizer tiktoken maps the model id to is the
    model's own; any other route may serve another model under an OpenAI name, so the name only picks its estimate.
    ``endpoint`` names where ``count`` reaches for a route whose identity names no endpoint, so a failing endpoint
    pauses only its own counts.
    """
    name = identity.id.lower().rsplit("/", 1)[-1]
    if local_encoder and _maps_to_o200k(name):
        return TokenCounter(key=f"tiktoken:{_O200K}", estimate=_o200k_tokens, exact_locally=True, replay=replay)
    return TokenCounter(
        key=f"{identity.provider}:{endpoint or identity.endpoint or ''}:{identity.id}",
        estimate=partial(_scaled_o200k_tokens, _margin(name)),
        count=count,
        replay=replay,
    )


def openai_model_counter(model_id: str) -> TokenCounter:
    """Return the counter of a model OpenAI serves itself, such as a realtime voice model."""
    return counter_for_model(ModelIdentity(id=model_id, provider="openai"), local_encoder=True)
