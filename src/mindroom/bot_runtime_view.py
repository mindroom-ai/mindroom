"""Shared live runtime state exposed to extracted bot collaborators."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from mindroom.response_admission import ResponseAdmissionGate

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.runtime_protocols import OrchestratorRuntime


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
    def response_admission_gate(self) -> ResponseAdmissionGate: ...  # noqa: D102

    @property
    def runtime_started_at(self) -> float: ...  # noqa: D102


@dataclass
class BotRuntimeState:
    """Concrete mutable runtime state shared by extracted collaborators."""

    client: nio.AsyncClient | None
    config: Config
    runtime_paths: RuntimePaths
    enable_streaming: bool
    orchestrator: OrchestratorRuntime | None
    # Orchestrator-owned and shared across bots. Lives here, not on ResponseRunner,
    # so it survives the runtime rebuild after a login identity change.
    response_admission_gate: ResponseAdmissionGate = field(default_factory=ResponseAdmissionGate)
    runtime_started_at: float = field(default_factory=time.time)

    def mark_runtime_started(self) -> None:
        """Record the runtime start time for this bot start."""
        self.runtime_started_at = time.time()
