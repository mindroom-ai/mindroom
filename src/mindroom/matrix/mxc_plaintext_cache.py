"""Principal-scoped process-local cache for decrypted Matrix sidecar text."""

from __future__ import annotations

import time
from collections import OrderedDict

from mindroom.logging_config import get_logger

logger = get_logger(__name__)

type MxcPlaintextCacheKey = tuple[str, str, str, str]

MXC_CACHE_TTL_SECONDS = 3600.0
MXC_TEXT_MAX_BYTES = 2 * 1024 * 1024

_mxc_cache: OrderedDict[MxcPlaintextCacheKey, tuple[str, float]] = OrderedDict()
_mxc_cache_max_entries = 500
_mxc_cache_max_bytes = 16 * 1024 * 1024
_mxc_cache_total_bytes = 0


def _text_size_bytes(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def cache_mxc_plaintext(
    mxc_url: str,
    text: str,
    timestamp: float,
    *,
    cache_key: MxcPlaintextCacheKey,
) -> None:
    """Cache bounded plaintext under its complete ownership key."""
    global _mxc_cache_total_bytes

    size_bytes = _text_size_bytes(text)
    if size_bytes > MXC_TEXT_MAX_BYTES:
        logger.warning(
            "mxc_text_cache_entry_exceeds_byte_limit",
            mxc_url=mxc_url,
            size_bytes=size_bytes,
            limit_bytes=MXC_TEXT_MAX_BYTES,
        )
        return
    if not _mxc_cache:
        _mxc_cache_total_bytes = 0
    if cache_key in _mxc_cache:
        previous_text, _ = _mxc_cache[cache_key]
        _mxc_cache_total_bytes -= _text_size_bytes(previous_text)
    _mxc_cache[cache_key] = (text, timestamp)
    _mxc_cache_total_bytes += size_bytes
    _mxc_cache.move_to_end(cache_key)
    _clean_expired_cache()


def get_cached_mxc_plaintext(cache_key: MxcPlaintextCacheKey) -> tuple[str, float] | None:
    """Return one cached plaintext entry without changing its LRU position."""
    return _mxc_cache.get(cache_key)


def touch_cached_mxc_plaintext(cache_key: MxcPlaintextCacheKey) -> None:
    """Mark one existing plaintext entry as most recently used."""
    try:
        _mxc_cache.move_to_end(cache_key)
    except KeyError:
        return


def remove_cached_mxc_plaintext(cache_key: MxcPlaintextCacheKey) -> None:
    """Remove one plaintext entry and maintain the byte bound."""
    global _mxc_cache_total_bytes

    cached = _mxc_cache.pop(cache_key, None)
    if cached is None:
        return
    _mxc_cache_total_bytes -= _text_size_bytes(cached[0])
    if not _mxc_cache:
        _mxc_cache_total_bytes = 0


def purge_principal_room_mxc_plaintext(principal_id: str, room_id: str) -> None:
    """Evict process-local plaintext owned by one principal and room."""
    owned_keys = [key for key in _mxc_cache if key[0] == principal_id and key[1] == room_id]
    for key in owned_keys:
        remove_cached_mxc_plaintext(key)


def _clean_expired_cache() -> None:
    """Remove expired entries, then evict oldest live entries until within bounds."""
    global _mxc_cache_total_bytes

    current_time = time.time()
    expired_keys = [
        key for key, (_, timestamp) in _mxc_cache.items() if current_time - timestamp >= MXC_CACHE_TTL_SECONDS
    ]
    for key in expired_keys:
        remove_cached_mxc_plaintext(key)
    evicted_entries = 0
    while len(_mxc_cache) > _mxc_cache_max_entries or _mxc_cache_total_bytes > _mxc_cache_max_bytes:
        _, (evicted_text, _) = _mxc_cache.popitem(last=False)
        _mxc_cache_total_bytes -= _text_size_bytes(evicted_text)
        evicted_entries += 1
    if expired_keys or evicted_entries:
        logger.debug(
            "mxc_cache_cleaned",
            expired_entries=len(expired_keys),
            evicted_entries=evicted_entries,
            cache_bytes=_mxc_cache_total_bytes,
        )
