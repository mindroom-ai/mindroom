"""Fail-closed cleanup for untrusted principal-owned Matrix cache rows."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .event_cache import ConversationEventCache


async def clear_untrusted_principal_cache(cache: ConversationEventCache) -> None:
    """Purge untrusted rows or disable this principal view until the next runtime."""
    if not cache.durable_writes_available:
        cache.disable("untrusted_principal_cache_cleanup_unavailable")
        return
    try:
        await cache.purge_principal()
    except Exception:
        cache.disable("untrusted_principal_cache_cleanup_failed")
        raise
