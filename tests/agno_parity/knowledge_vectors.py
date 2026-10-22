"""Deterministic embedding vectors the knowledge goldens were captured with, which their parity tests reuse."""

from __future__ import annotations

import hashlib

#: Width of every parity vector.
PARITY_VECTOR_DIMENSIONS = 8


def parity_vector(text: str) -> list[float]:
    """Return a non-zero vector derived from ``text``, exactly representable as float32.

    Each component is an odd multiple of 1/256, so Chroma's float32 storage returns it unchanged and no component is
    ever zero.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [(2 * byte - 255) / 256 for byte in digest[:PARITY_VECTOR_DIMENSIONS]]
