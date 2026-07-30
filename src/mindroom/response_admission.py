"""Shared global gate deciding when responses may enter a runtime replacement.

Config reloads and MCP catalog replacements must not hand new responses to
entities they are about to stop and recreate. ``ConfigReloadLifecycle``
serializes those flows behind one admission owner, waits up to 600 seconds for
active responses to drain, and then force-applies if the runtime never becomes
idle. MCP notifications schedule their replacement asynchronously so the
triggering admitted tool call can release its own slot first.

The gate closes admission for exactly the apply window, but the applier never
holds it while running the plan. Applying stops bots, and stopping a bot drains
its detached responses, which would otherwise wait on the very gate the
applier holds.

Scope: the gate covers Matrix-driven response lifecycles, which is where a
replacement can stop an entity mid-turn. Direct agent-run entry points that
bypass the response lifecycle (the OpenAI-compatible API in
``mindroom.api.openai_compat`` and cascaded voice in
``mindroom.matrix_rtc.call_tools``) are not admitted through it, so a
replacement can still land underneath one of those runs.

Every state transition is deliberately synchronous. No critical section here
contains an ``await``, so the single-threaded event loop cannot interleave one
transition against another and a lock would add nothing. ``wait_until_open``
only observes the event published by those transitions. Keeping ``release``
synchronous matters on cancellation, where an ``await`` could itself be
interrupted and permanently leak a slot, wedging replacement admission.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


class ResponseAdmissionRefusedError(Exception):
    """Raised when a response waiting through replacement loses its runtime.

    Deliberately not an ``asyncio.CancelledError``. The response never entered
    its lifecycle, so replacement-runtime replay must see a failed callback
    instead of expected shutdown noise.
    """

    def __init__(self) -> None:
        super().__init__("Runtime replacement is restarting this entity")


@dataclass
class ResponseAdmissionGate:
    """Track in-flight responses and close admission while a replacement runs."""

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
        """Return whether admission is currently closed for a replacement."""
        return self._closed

    def admit(self) -> bool:
        """Reserve one response slot, or return False during replacement."""
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
        """Reopen admission after a replacement finishes."""
        self._closed = False
        self._open_event.set()

    async def wait_until_open(self) -> None:
        """Wait until runtime replacement reopens response admission."""
        await self._open_event.wait()
