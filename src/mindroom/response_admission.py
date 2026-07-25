"""Shared gate deciding when responses may be admitted during a config apply.

A config reload must not hand new responses to entities it is about to stop and
recreate. The gate closes admission for exactly that window, but the applier
never holds it while running the plan: applying stops bots, and stopping a bot
drains its detached responses, which would otherwise wait on the very gate the
applier holds.

Scope: the gate covers Matrix-driven response lifecycles, which is where a
config reload can stop an entity mid-turn. Direct agent-run entry points that
bypass the response lifecycle (the OpenAI-compatible API in
``mindroom.api.openai_compat`` and cascaded voice in
``mindroom.matrix_rtc.call_tools``) are not admitted through it, so a reload can
still land underneath one of those runs.

Every state transition is deliberately synchronous. No critical section here
contains an ``await``, so the single-threaded event loop cannot interleave one
transition against another and a lock would add nothing. ``wait_until_open``
only observes the event published by those transitions. Keeping ``release``
synchronous matters on cancellation, where an ``await`` could itself be
interrupted and permanently leak a slot, wedging config reload.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class ResponseAdmissionRefusedError(Exception):
    """Raised when a response waiting through config apply loses its runtime.

    Deliberately not an ``asyncio.CancelledError``. The response never entered
    its lifecycle, so replacement-runtime replay must see a failed callback
    instead of expected shutdown noise.
    """

    def __init__(self) -> None:
        super().__init__("Configuration reload is restarting this entity")


@dataclass
class ResponseAdmissionGate:
    """Track in-flight responses and close admission while a config apply runs."""

    _in_flight_response_count: int = field(default=0, init=False)
    _closed: bool = field(default=False, init=False)
    _open_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    def __post_init__(self) -> None:
        """Publish the initially open state to response waiters."""
        self._open_event.set()

    @property
    def in_flight_response_count(self) -> int:
        """Return the number of admitted, not-yet-finished response lifecycles."""
        return self._in_flight_response_count

    @property
    def closed(self) -> bool:
        """Return whether admission is currently closed for a config apply."""
        return self._closed

    def admit(self) -> bool:
        """Reserve one response slot, or return False while a config apply owns the runtime."""
        if self._closed:
            return False
        self._in_flight_response_count += 1
        return True

    def release(self) -> None:
        """Release one previously admitted response slot."""
        assert self._in_flight_response_count > 0, "release() without a matching admit()"
        self._in_flight_response_count -= 1

    def close_if_idle(self) -> bool:
        """Close admission when no response is in flight, so an apply can start."""
        if self._in_flight_response_count > 0:
            return False
        self._closed = True
        self._open_event.clear()
        return True

    def close(self) -> None:
        """Close admission regardless of in-flight responses, for a forced apply."""
        self._closed = True
        self._open_event.clear()

    def reopen(self) -> None:
        """Reopen admission after a config apply finishes."""
        self._closed = False
        self._open_event.set()

    async def wait_until_open(self) -> None:
        """Wait until config application reopens response admission."""
        await self._open_event.wait()
