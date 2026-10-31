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
_CLAUDE_MARGIN = 1.7
_GEMINI_MARGIN = 1.3
_OPENAI_MARGIN = 1.0
_OTHER_MARGIN = 1.25
_OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-", "codex-")


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
        logger.warning("o200k_base did not load; token estimates use a quarter token per character", exc_info=True)


def _loaded_o200k() -> tiktoken.Encoding | None:
    """Return the encoder once loaded; the first call starts its load in a background thread and returns None."""
    encoder = _o200k_encoder
    if encoder.value is None and not encoder.started:
        encoder.started = True
        threading.Thread(target=_load_in_background, name="o200k-loader", daemon=True).start()
    return encoder.value


def _o200k_tokens(text: str) -> int | None:
    encoder = _loaded_o200k()
    return None if encoder is None else len(encoder.encode(text, disallowed_special=()))


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
) -> TokenCounter:
    """Return the counter of the model ``identity`` names, counting through ``count`` when no local encoder matches.

    ``local_encoder`` says the route serves OpenAI's own models, so the tokenizer tiktoken maps the model id to is the
    model's own; any other route may serve another model under an OpenAI name, so the name only picks its estimate.
    """
    name = identity.id.lower().rsplit("/", 1)[-1]
    if local_encoder and _maps_to_o200k(name):
        return TokenCounter(key=f"tiktoken:{_O200K}", estimate=_o200k_tokens, exact_locally=True, replay=replay)
    return TokenCounter(
        key=f"{identity.provider}:{identity.endpoint or ''}:{identity.id}",
        estimate=partial(_scaled_o200k_tokens, _margin(name)),
        count=count,
        replay=replay,
    )


def openai_model_counter(model_id: str) -> TokenCounter:
    """Return the counter of a model OpenAI serves itself, such as a realtime voice model."""
    return counter_for_model(ModelIdentity(id=model_id, provider="openai"), local_encoder=True)
