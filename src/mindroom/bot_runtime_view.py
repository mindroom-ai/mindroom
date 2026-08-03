"""Shared live runtime state exposed to extracted bot collaborators."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from mindroom.response_admission import ResponseAdmissionGate
from mindroom.runtime_generation_lease import (
    RuntimeGenerationLease,
    acquire_runtime_generation_lease,
)

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.cache import ConversationEventCache, EventCacheWriteCoordinator
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.runtime_support import StartupThreadPrewarmRegistry


class BotRuntimeView(Protocol):
    """Live mutable bot state that extracted collaborators may consult."""

    @property
    def client(self) -> nio.AsyncClient | None: ...  # noqa: D102

    @property
    def config(self) -> Config: ...  # noqa: D102

    @property
    def runtime_paths(self) -> RuntimePaths: ...  # noqa: D102

    @property
    def enable_streaming(self) -> bool: ...  # noqa: D102

    @property
    def orchestrator(self) -> OrchestratorRuntime | None: ...  # noqa: D102

    @property
    def event_cache(self) -> ConversationEventCache: ...  # noqa: D102

    @property
    def event_cache_write_coordinator(self) -> EventCacheWriteCoordinator: ...  # noqa: D102

    @property
    def startup_thread_prewarm_registry(self) -> StartupThreadPrewarmRegistry: ...  # noqa: D102

    @property
    def response_admission_gate(self) -> ResponseAdmissionGate: ...  # noqa: D102

    @property
    def runtime_started_at(self) -> float: ...  # noqa: D102

    @property
    def runtime_generation(self) -> str: ...  # noqa: D102


@dataclass
class BotRuntimeState:
    """Concrete mutable runtime state shared by extracted collaborators."""

    client: nio.AsyncClient | None
    config: Config
    runtime_paths: RuntimePaths
    enable_streaming: bool
    orchestrator: OrchestratorRuntime | None
    event_cache: ConversationEventCache | None
    event_cache_write_coordinator: EventCacheWriteCoordinator | None
    startup_thread_prewarm_registry: StartupThreadPrewarmRegistry | None = None
    # Orchestrator-owned and shared across bots. Lives here, not on ResponseRunner,
    # so it survives the runtime rebuild after a login identity change.
    response_admission_gate: ResponseAdmissionGate = field(default_factory=ResponseAdmissionGate)
    runtime_started_at: float = field(default_factory=time.time)
    # Clock-free ownership stamp for streams created by this bot start.
    runtime_generation: str = field(default_factory=lambda: uuid4().hex)
    _runtime_generation_lease: RuntimeGenerationLease | None = field(default=None, init=False, repr=False)

    def mark_runtime_started(self) -> None:
        """Rotate the generation and hold its cross-process lease for this bot start."""
        generation = uuid4().hex
        lease = acquire_runtime_generation_lease(self.runtime_paths, generation)
        if self._runtime_generation_lease is not None:
            self._runtime_generation_lease.release()
        self.runtime_started_at = time.time()
        self.runtime_generation = generation
        self._runtime_generation_lease = lease

    def mark_runtime_stopped(self) -> None:
        """Release this runtime generation's process-held ownership lease."""
        if self._runtime_generation_lease is None:
            return
        self._runtime_generation_lease.release()
        self._runtime_generation_lease = None
