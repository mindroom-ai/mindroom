"""Shared live runtime state exposed to extracted bot collaborators."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.matrix.conversation_cache import ConversationEventCache, EventCacheWriteCoordinator
    from mindroom.orchestrator import MultiAgentOrchestrator


class BotRuntimeView(Protocol):
    """Live mutable bot state that extracted collaborators may consult."""

    @property
    def client(self) -> nio.AsyncClient | None: ...  # noqa: D102

    @property
    def config(self) -> Config: ...  # noqa: D102

    @property
    def enable_streaming(self) -> bool: ...  # noqa: D102

    @property
    def orchestrator(self) -> MultiAgentOrchestrator | None: ...  # noqa: D102

    @property
    def event_cache(self) -> ConversationEventCache: ...  # noqa: D102

    @property
    def event_cache_write_coordinator(self) -> EventCacheWriteCoordinator: ...  # noqa: D102

    @property
    def runtime_started_at(self) -> float: ...  # noqa: D102


@dataclass
class BotRuntimeState:
    """Concrete mutable runtime state shared by extracted collaborators."""

    client: nio.AsyncClient | None
    config: Config
    enable_streaming: bool
    orchestrator: MultiAgentOrchestrator | None
    event_cache: ConversationEventCache | None
    event_cache_write_coordinator: EventCacheWriteCoordinator | None
    runtime_started_at: float = field(default_factory=time.time)

    def mark_runtime_started(self) -> None:
        """Advance the runtime freshness boundary for one bot start or restart."""
        self.runtime_started_at = time.time()
