"""Shared gate deciding when responses may be admitted during a config apply.

A config reload must not hand new responses to entities it is about to stop and
recreate. The gate closes admission for exactly that window, but the applier
never holds it while running the plan: applying stops bots, and stopping a bot
drains its detached responses, which would otherwise wait on the very gate the
applier holds.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class ResponseAdmissionGate:
    """Track in-flight responses and close admission while a config apply runs."""

    _in_flight_response_count: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @property
    def in_flight_response_count(self) -> int:
        """Return the number of admitted, not-yet-finished response lifecycles."""
        return self._in_flight_response_count

    @property
    def closed(self) -> bool:
        """Return whether admission is currently closed for a config apply."""
        return self._closed

    async def admit(self) -> bool:
        """Reserve one response slot, or return False while a config apply owns the runtime."""
        async with self._lock:
            if self._closed:
                return False
            self._in_flight_response_count += 1
            return True

    def release(self) -> None:
        """Release one previously admitted response slot.

        Deliberately synchronous and lock-free, unlike every other mutation
        here. Releasing runs in a ``finally`` on the cancellation path, and an
        ``await`` there can itself be interrupted, which would leak a slot and
        wedge config reload forever. A bare decrement cannot be interrupted, and
        it is safe against the lock because releasing only ever lowers the count:
        it can never turn a closed gate's idle sample back into a busy one, so no
        reader can observe a torn state.
        """
        self._in_flight_response_count -= 1

    async def close_if_idle(self) -> bool:
        """Close admission when no response is in flight, so an apply can start."""
        async with self._lock:
            if self._in_flight_response_count > 0:
                return False
            self._closed = True
            return True

    async def close(self) -> None:
        """Close admission regardless of in-flight responses, for a forced apply."""
        async with self._lock:
            self._closed = True

    async def reopen(self) -> None:
        """Reopen admission after a config apply finishes."""
        async with self._lock:
            self._closed = False
