"""Shared token estimation and compaction budget helpers.

Kept deliberately narrow: only generic token math lives here.
Agno replay helpers and compaction serialization stay in their own modules.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Literal

import tiktoken

_CompactionEstimateKind = Literal[
    "model_tiktoken_tokens",
    "utf8_bytes_token_upper_bound",
]


def estimate_text_tokens(value: str | list[str] | None) -> int:
    """Estimate token count using chars / 4."""
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value) // 4
    if isinstance(value, list):
        return sum(len(stable_serialize(part)) for part in value) // 4
    return len(stable_serialize(value)) // 4


@lru_cache(maxsize=16)
def _compaction_encoding(model_id: str | None) -> tiktoken.Encoding | None:
    if model_id:
        try:
            return tiktoken.encoding_for_model(model_id)
        except KeyError:
            pass
    return None


def compaction_estimate_kind(model_id: str | None, *, genuine_openai_endpoint: bool) -> _CompactionEstimateKind:
    """Resolve how compaction sizes summary payloads for one summary model.

    Single source of truth for the sizing branch:
    ``compaction_payload_token_upper_bound`` dispatches on this result and the
    structured sizing logs record it, so arithmetic and labels cannot diverge.
    The tiktoken branch additionally requires the genuine OpenAI endpoint
    because a model id alone does not identify the serving tokenizer — custom
    OpenAI-compatible endpoints can serve arbitrary models under
    tiktoken-recognized ids, so those fall to the byte bound.
    """
    if genuine_openai_endpoint and _compaction_encoding(model_id) is not None:
        return "model_tiktoken_tokens"
    return "utf8_bytes_token_upper_bound"


def compaction_payload_token_upper_bound(value: str, *, model_id: str | None, genuine_openai_endpoint: bool) -> int:
    """Size one serialized compaction payload for the summary model.

    Models with a tiktoken-recognized id served by the genuine OpenAI endpoint
    count exactly. Every other model is sized by UTF-8 byte count, with
    ``surrogatepass`` keeping unpaired surrogates (reachable via JSON-decoded
    provider payloads) countable at 3 bytes each instead of raising.

    The byte count is a proven token upper bound for byte-level BPE
    tokenizers, where every token consumes at least one byte; that covers the
    providers routed through this path today. Tokenizers that normalize text
    before segmentation (NFKC/SentencePiece-class) can expand rare
    compatibility characters (for example U+FDFA) beyond their byte length,
    so the bound is not universal there — realistic chat and tool content
    sits well below the bound, and the compaction budget's reserve and safety
    margin absorb such pockets.
    """
    kind = compaction_estimate_kind(model_id, genuine_openai_endpoint=genuine_openai_endpoint)
    if kind == "model_tiktoken_tokens":
        encoding = _compaction_encoding(model_id)
        if encoding is not None:
            return len(encoding.encode(value, disallowed_special=()))
    return len(value.encode("utf-8", errors="surrogatepass"))


def approximate_o200k_tokens(value: str) -> int:
    """Approximate a token count with the o200k_base encoding.

    An approximation, not a bound: o200k_base can undercount other tokenizers.
    Callers that need a safe compaction sizing bound use
    ``compaction_payload_token_upper_bound``.
    """
    return len(tiktoken.get_encoding("o200k_base").encode(value, disallowed_special=()))


def compute_compaction_input_budget(
    context_window: int,
    *,
    reserve_tokens: int,
    prompt_overhead_tokens: int = 2000,
    safety_margin_ratio: float = 0.10,
) -> int:
    """Compute the max input tokens available for a compaction summary request.

    Subtracts output reserve, prompt overhead (system prompt + response format),
    and a safety margin from the compaction model's context window.
    """
    safety = int(context_window * safety_margin_ratio)
    budget = context_window - reserve_tokens - prompt_overhead_tokens - safety
    return max(0, budget)


def stable_serialize(value: object) -> str:
    """Serialize arbitrary values into a stable JSON-ish string."""
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
