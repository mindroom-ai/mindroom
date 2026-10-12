"""Shared strict, byte-bounded JSON for the CLI wire format (stdlib only)."""

from __future__ import annotations

import json
import math

from mindroom.json_utils import object_with_unique_keys

MAX_ENVELOPE_BYTES = 64 * 1024


def canonical_json(value: object) -> str:
    """Encode one bounded envelope, rejecting non-finite numbers."""
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            raise ValueError  # noqa: TRY301 - All wire errors share the bounded error contract.
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        msg = "Agent CLI payload must be strict JSON within 64 KiB"
        raise ValueError(msg) from exc
    return encoded


def _number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        msg = "Non-finite JSON number"
        raise ValueError(msg)
    return number


def read_json(payload: str | bytes) -> object:
    """Decode strict JSON without duplicate keys or overflowing floats."""
    try:
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError  # noqa: TRY301 - All wire errors share the bounded error contract.
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_with_unique_keys,
            parse_constant=_number,
            parse_float=_number,
        )
    except (ValueError, UnicodeError, RecursionError) as exc:
        msg = "Agent CLI payload must be strict JSON within 64 KiB"
        raise ValueError(msg) from exc
