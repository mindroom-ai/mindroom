"""Non-sensitive phase labels for response owners retained at shutdown."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


class ResponseShutdownPhase(StrEnum):
    """Fixed response phases safe to aggregate in operational logs."""

    RESPONSE_EXECUTION = "response_execution"
    AGENT_PREPARATION = "agent_preparation"
    STREAMING_RESPONSE = "streaming_response"
    FINAL_DELIVERY = "final_delivery"
    RECOVERY_PROOF = "recovery_proof"


@dataclass(slots=True)
class ResponseShutdownPhaseTrace:
    """Track the deepest active fixed phase for one response owner."""

    _next_token: int = 0
    _active: dict[int, ResponseShutdownPhase] = field(default_factory=dict)

    @property
    def phase(self) -> str:
        """Return the deepest active phase, or the generic response fallback."""
        if not self._active:
            return ResponseShutdownPhase.RESPONSE_EXECUTION.value
        return self._active[max(self._active)].value

    @contextmanager
    def activate(self, phase: ResponseShutdownPhase) -> Iterator[None]:
        """Make ``phase`` current until its real boundary exits."""
        self._next_token += 1
        token = self._next_token
        self._active[token] = phase
        try:
            yield
        finally:
            self._active.pop(token, None)


_CURRENT_RESPONSE_SHUTDOWN_TRACE: contextvars.ContextVar[ResponseShutdownPhaseTrace | None] = contextvars.ContextVar(
    "mindroom_response_shutdown_trace",
    default=None,
)


def context_with_response_shutdown_trace(trace: ResponseShutdownPhaseTrace) -> contextvars.Context:
    """Return a child-task context bound to ``trace`` without mutating the caller."""
    context = contextvars.copy_context()
    context.run(_CURRENT_RESPONSE_SHUTDOWN_TRACE.set, trace)
    return context


@contextmanager
def response_shutdown_phase(phase: ResponseShutdownPhase) -> Iterator[None]:
    """Record one fixed phase when running under a tracked response owner."""
    trace = _CURRENT_RESPONSE_SHUTDOWN_TRACE.get()
    if trace is None:
        yield
        return
    with trace.activate(phase):
        yield


__all__ = [
    "ResponseShutdownPhase",
    "ResponseShutdownPhaseTrace",
    "context_with_response_shutdown_trace",
    "response_shutdown_phase",
]
