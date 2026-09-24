"""Whole-response ownership, separate from per-attempt prepared bindings."""

from __future__ import annotations

__all__ = ["CliTurnLifetime", "cli_control_executions", "current_cli_lifetime", "response_cli_lifetime"]

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from mindroom.agno_compat_cli_checkpoint import checkpoint_resolver

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from contextlib import AbstractAsyncContextManager

    from agno.agent import Agent
    from agno.models.response import ToolExecution
    from agno.tools.function import Function

    from mindroom.agent_cli.session import TurnToolRegistry
    from mindroom.agent_cli.turn import LiveTurnTools
    from mindroom.agent_cli.worker import CliWorkerLease
    from mindroom.agno_compat_cli_checkpoint import ProviderBatchCheckpoint

_CURRENT: ContextVar[CliTurnLifetime | None] = ContextVar("cli_turn_lifetime", default=None)


class CliTurnLifetime:
    """Keep worker/grants through continuations; retire only attempt catalogs."""

    def __init__(self) -> None:
        self.owner: LiveTurnTools | None = None
        self.grant_expires_at_ns: int | None = None
        self.continuation_count = 0
        self._provider: tuple[ProviderBatchCheckpoint, Function] | None = None
        self._response_task = asyncio.current_task()
        self._registry: TurnToolRegistry | None = None
        self._resources = AsyncExitStack()

    @contextmanager
    def bind(self) -> Iterator[None]:
        """Bind only within a pull or close, never across a public stream yield."""
        token = _CURRENT.set(self)
        try:
            with checkpoint_resolver(lambda: self._provider):
                yield
        finally:
            _CURRENT.reset(token)

    def bind_provider(self, checkpoint: ProviderBatchCheckpoint, function: Function) -> None:
        """Publish the actual prepared facade to the already-bound lazy stream."""
        checkpoint.prepare_capture()
        self._provider = checkpoint, function

    def _clear_provider(self) -> None:
        if self._provider is not None:
            self._provider[0].clear()
            self._provider = None

    async def enter_worker(self, worker: AbstractAsyncContextManager[CliWorkerLease]) -> CliWorkerLease:
        """Acquire before constructing the trusted owner, retain until turn exit."""
        return await self._resources.enter_async_context(worker)

    def register(self, owner: LiveTurnTools, registry: TurnToolRegistry) -> None:
        """Accept an explicit orchestrator registry, never an ambient authority."""
        owner.bind_response_task(self._response_task)
        registry.register(owner)
        self.owner = owner
        self._registry = registry

    async def retire_attempt(self) -> None:
        """Retain queue and worker while the response driver rebuilds its Agent."""
        self._clear_provider()
        if self.owner is not None:
            await self.owner.retire_binding()

    async def close(self) -> None:
        """Finish hooks/catalog before retiring the acquired worker lease."""
        try:
            if self.owner is not None:
                try:
                    await self.owner.close()
                finally:
                    if self._registry is not None:
                        self._registry.unregister(self.owner)
        finally:
            self._clear_provider()
            await self._resources.aclose()


def current_cli_lifetime() -> CliTurnLifetime | None:
    """Return the response's registration scope to its normal Agent builder."""
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


def cli_control_executions(agent: Agent) -> tuple[ToolExecution, ...]:
    """Read trusted attempt controls only for this exact live Agent."""
    lifetime = current_cli_lifetime()
    owner = lifetime.owner if lifetime is not None else None
    if owner is None or owner.catalog.agent is not agent:
        return ()
    return tuple(owner.control_executions)
