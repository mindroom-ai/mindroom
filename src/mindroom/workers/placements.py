"""Which conversation sessions run their worker-routed tool calls in a dedicated worker of their own.

A delegated subagent that asked for its own worker binds its placement here for each attempt of its turn, under the
child's session ID, which every execution identity of that attempt carries: the tools its agent was built with, tools
composed from the Matrix runtime context, and minimal-mode CLI calls. The worker route reads it. Background scripts the
child starts outlive the attempt, so their calls run on their normal workers (``normal_worker_calls``).
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.workers.models import ScriptResourceProfileName

__all__ = ["DedicatedWorkerPlacement", "dedicated_worker_placement", "normal_worker_calls", "placement_for_session"]


@dataclass(frozen=True, slots=True)
class DedicatedWorkerPlacement:
    """One subagent's own worker: the subagent it is pinned to and the resource profile it starts with."""

    subagent_id: str
    resource_profile: ScriptResourceProfileName | None = None


_lock = threading.Lock()
_normal_worker_calls: ContextVar[bool] = ContextVar("normal_worker_calls", default=False)
# Each bound session's placement and how many attempts hold it.
_placements: dict[str, tuple[DedicatedWorkerPlacement, int]] = {}


@contextmanager
def dedicated_worker_placement(session_id: str, placement: DedicatedWorkerPlacement | None) -> Iterator[None]:
    """Place ``session_id``'s worker-routed calls in ``placement``'s worker while the block runs; None places nothing."""
    if placement is None:
        yield
        return
    with _lock:
        # A session names its subagent, whose placement never changes, so nested attempts bind the same one.
        _, holders = _placements.get(session_id, (placement, 0))
        _placements[session_id] = (placement, holders + 1)
    try:
        yield
    finally:
        with _lock:
            _, holders = _placements[session_id]
            if holders == 1:
                del _placements[session_id]
            else:
                _placements[session_id] = (placement, holders - 1)


@contextmanager
def normal_worker_calls() -> Iterator[None]:
    """Run the block's calls on their normal workers whatever their session's placement, as background scripts do.

    A script outlives the subagent attempt that started it, so its calls would otherwise move between workers.
    """
    token = _normal_worker_calls.set(True)
    try:
        yield
    finally:
        _normal_worker_calls.reset(token)


def placement_for_session(session_id: str | None) -> DedicatedWorkerPlacement | None:
    """Return the dedicated worker placement bound for ``session_id``, if any."""
    if session_id is None or _normal_worker_calls.get():
        return None
    with _lock:
        bound = _placements.get(session_id)
    return None if bound is None else bound[0]
