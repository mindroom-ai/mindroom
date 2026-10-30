"""Shared live runtime state exposed to extracted bot collaborators."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

from mindroom.response_admission import admitted_response_decision

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.response_admission import ResponseAdmissionGate
    from mindroom.runtime_protocols import OrchestratorRuntime


class RunPhase(Enum):
    """Which work one bot generation accepts."""

    # New responses and callbacks run.
    ACCEPTING = auto()
    # A sync shutdown is preparing (restart, removal, or a generic stop): work waiting for admission is refused while
    # owned responses drain, and the next sync loop reopens unless ingestion is quiesced.
    DRAINING = auto()
    # Orderly process shutdown: nothing new starts, not even an inbox response, and nothing reopens.
    SHUTTING_DOWN = auto()


class BotRuntimeView(Protocol):
    """Live mutable bot state that extracted collaborators may consult."""

    @property
    def client(self) -> nio.AsyncClient | None: ...  # noqa: D102

    @property
    def config(self) -> Config: ...  # noqa: D102

    @property
    def runtime_paths(self) -> RuntimePaths: ...  # noqa: D102

    @property
    def agent_reply_memberships(self) -> AgentReplyMembershipIndex: ...  # noqa: D102

    @property
    def orchestrator(self) -> OrchestratorRuntime: ...  # noqa: D102

    @property
    def response_admission_gate(self) -> ResponseAdmissionGate: ...  # noqa: D102

    @property
    def runtime_started_at(self) -> float: ...  # noqa: D102

    @property
    def phase(self) -> RunPhase: ...  # noqa: D102

    def admission_fence(self) -> AbstractAsyncContextManager[None]: ...  # noqa: D102

    async def wait_for_admission_or_shutdown(self) -> bool: ...  # noqa: D102


@dataclass
class BotRuntimeState:
    """One bot generation's runtime state, reading what the orchestrator shares through the orchestrator itself."""

    orchestrator: OrchestratorRuntime
    client: nio.AsyncClient | None = None
    runtime_started_at: float = field(default_factory=time.time)
    phase: RunPhase = field(default=RunPhase.ACCEPTING, init=False)
    # Latched by process shutdown and by the fleet before it stops or removes the bot, so a late sync loop of this
    # generation cannot reopen admission.
    ingestion_quiesced: bool = field(default=False, init=False)
    # Set whenever the phase is not ACCEPTING, to wake responses waiting for admission.
    _stopping: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    @property
    def config(self) -> Config:
        """Return the config the runtime published last."""
        return self.orchestrator.config

    @property
    def runtime_paths(self) -> RuntimePaths:
        """Return the runtime's paths."""
        return self.orchestrator.runtime_paths

    @property
    def agent_reply_memberships(self) -> AgentReplyMembershipIndex:
        """Return the runtime's one room-membership reply-grant index."""
        return self.orchestrator.agent_reply_memberships

    @property
    def response_admission_gate(self) -> ResponseAdmissionGate:
        """Return the runtime's one gate deciding when responses may enter a replacement."""
        return self.orchestrator.response_admission_gate

    def mark_runtime_started(self) -> None:
        """Record the runtime start time for this bot start."""
        self.runtime_started_at = time.time()

    def begin_draining(self) -> None:
        """Refuse work waiting for admission while a sync shutdown drains what this generation owns."""
        if self.phase is RunPhase.ACCEPTING:
            self.phase = RunPhase.DRAINING
        self._stopping.set()

    def begin_process_shutdown(self) -> None:
        """Close this generation for good: nothing new starts and no sync loop reopens it."""
        self.phase = RunPhase.SHUTTING_DOWN
        self.ingestion_quiesced = True
        self._stopping.set()

    def quiesce_ingestion(self) -> None:
        """Latch admission closed across the drains of a stop that is about to happen."""
        self.ingestion_quiesced = True

    def resume(self) -> bool:
        """Accept work again for a fresh sync loop; refused once ingestion is quiesced."""
        if self.ingestion_quiesced:
            return False
        self.phase = RunPhase.ACCEPTING
        self._stopping.clear()
        return True

    async def wait_for_admission_or_shutdown(self) -> bool:
        """Return whether the runtime reopened response admission before this generation stopped accepting work."""
        if self.phase is not RunPhase.ACCEPTING:
            return False
        admission_opened = asyncio.create_task(self.response_admission_gate.wait_until_open())
        stopping = asyncio.create_task(self._stopping.wait())
        try:
            await asyncio.wait((admission_opened, stopping), return_when=asyncio.FIRST_COMPLETED)
            return self.phase is RunPhase.ACCEPTING
        finally:
            for task in (admission_opened, stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(admission_opened, stopping, return_exceptions=True)

    def admission_fence(self) -> AbstractAsyncContextManager[None]:
        """Reserve runtime-replacement admission around one authorization decision and the effects it permits."""
        return admitted_response_decision(self.response_admission_gate, self.wait_for_admission_or_shutdown)
