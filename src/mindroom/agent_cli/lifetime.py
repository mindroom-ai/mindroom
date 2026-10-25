"""Whole-response ownership, separate from per-attempt prepared bindings."""

from __future__ import annotations

__all__ = ["CliTurnLifetime", "cli_control_calls", "current_cli_lifetime", "response_cli_lifetime"]

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from mindroom.tool_jobs.resources import defer_execution_cleanup

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.agent_cli.turn import LiveTurnTools
    from mindroom.llm.events import ReportedCall

_CURRENT: ContextVar[CliTurnLifetime | None] = ContextVar("cli_turn_lifetime", default=None)


class CliTurnLifetime:
    """Keep the grant through continuations; retire only attempt catalogs."""

    def __init__(self) -> None:
        self.owner: LiveTurnTools | None = None
        self.grant_expires_at_ns: int | None = None
        self.continuation_count = 0
        self._response_task = asyncio.current_task()
        self._registry: TurnToolRegistry | None = None
        self._closed = False

    @contextmanager
    def bind(self) -> Iterator[None]:
        """Bind only within a pull or close, never across a public stream yield."""
        token = _CURRENT.set(self)
        try:
            yield
        finally:
            _CURRENT.reset(token)

    def register(self, owner: LiveTurnTools, registry: TurnToolRegistry) -> None:
        """Accept an explicit orchestrator registry, never an ambient authority."""
        if self._closed:
            msg = "The response's CLI lifetime has ended"
            raise RuntimeError(msg)
        owner.bind_response_task(self._response_task)
        registry.register(owner)
        self.owner = owner
        self._registry = registry

    async def retire_attempt(self) -> None:
        """Retain queue and grant while the response driver builds its next attempt."""
        if self.owner is not None:
            await self.owner.retire_binding()

    async def close(self) -> None:
        """Close the response's owner, or leave that to its last background job, which may still run CLI calls."""
        self._closed = True
        if self.owner is None or not defer_execution_cleanup(self._close_owner, resource=self):
            await self._close_owner()

    async def _close_owner(self) -> None:
        """Finish hooks/catalog, then forget the response's owner and task."""
        try:
            if self.owner is not None:
                await self.owner.close()
        finally:
            if self.owner is not None and self._registry is not None:
                self._registry.unregister(self.owner)
            # A context copied during the response, such as a cancelled timer's, can outlive it.
            self.owner = None
            self._registry = None
            self._response_task = None


def current_cli_lifetime() -> CliTurnLifetime | None:
    """Return the response's registration scope to the attempt that binds its CLI."""
    return _CURRENT.get()


@asynccontextmanager
async def response_cli_lifetime() -> AsyncIterator[CliTurnLifetime]:
    """Scope one owner around the complete response continuation loop."""
    lifetime = CliTurnLifetime()
    with lifetime.bind():
        try:
            yield lifetime
        finally:
            await lifetime.close()


def cli_control_calls(run_id: str) -> tuple[ReportedCall, ...]:
    """Read trusted attempt controls only for the catalog of run ``run_id``."""
    lifetime = current_cli_lifetime()
    owner = lifetime.owner if lifetime is not None else None
    if owner is None or owner.catalog.run.run_id != run_id:
        return ()
    return tuple(owner.control_calls)
