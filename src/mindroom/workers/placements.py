"""Which conversation session runs its worker-routed tool calls in a dedicated worker of its own.

A delegated subagent that asked for its own worker binds its placement for each attempt of its turn. Every call the
attempt makes runs in a copy of the attempt's context: the tools its agent was built with, tools composed from the
Matrix runtime context, its approval resumes, and the minimal-mode CLI calls its shell commands admit. Background
script calls arrive through their gateway requests instead, so they always run on their normal workers.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.workers.models import ScriptResourceProfileName, WorkerSandboxConfig

__all__ = [
    "DedicatedWorkerPlacement",
    "bound_sandbox_placement",
    "dedicated_worker_placement",
    "placement_for_session",
]


@dataclass(frozen=True, slots=True)
class DedicatedWorkerPlacement:
    """One subagent's own worker: its pin ID, resource profile, and optional sandbox mount isolation."""

    subagent_id: str
    resource_profile: ScriptResourceProfileName | None = None
    sandbox: WorkerSandboxConfig | None = None


# The session the running attempt places and its placement, or None when it places none.
_placement: ContextVar[tuple[str, DedicatedWorkerPlacement] | None] = ContextVar(
    "dedicated_worker_placement",
    default=None,
)


@contextmanager
def dedicated_worker_placement(session_id: str, placement: DedicatedWorkerPlacement | None) -> Iterator[None]:
    """Place ``session_id``'s calls in ``placement``'s worker while the block runs; None places nothing in it."""
    token = _placement.set(None if placement is None else (session_id, placement))
    try:
        yield
    finally:
        _placement.reset(token)


def placement_for_session(session_id: str | None) -> DedicatedWorkerPlacement | None:
    """Return the placement the running attempt binds for ``session_id``, if any."""
    bound = _placement.get()
    return bound[1] if bound is not None and session_id is not None and bound[0] == session_id else None


def bound_sandbox_placement() -> tuple[str, DedicatedWorkerPlacement] | None:
    """Return the active ``(session_id, placement)`` when the running attempt requires sandboxed execution."""
    bound = _placement.get()
    return bound if bound is not None and bound[1].sandbox is not None else None
