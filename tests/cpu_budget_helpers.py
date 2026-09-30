"""Bound one call's CPU time without billing it for garbage collection."""

from __future__ import annotations

import gc
from contextlib import contextmanager
from time import thread_time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def cpu_budget(seconds: float) -> Iterator[None]:
    """Fail when the body uses more than ``seconds`` of this thread's CPU.

    Only this thread's CPU counts, so other threads busy in the same test worker or a
    descheduled runner cannot exceed the budget. A collection of the worker's whole heap
    can also run inside the body and bill this thread, so collection is paused meanwhile.
    """
    was_enabled = gc.isenabled()
    gc.disable()
    started = thread_time()
    try:
        yield
    finally:
        elapsed = thread_time() - started
        if was_enabled:
            gc.enable()
    assert elapsed < seconds, f"used {elapsed:.2f}s of CPU, budget {seconds}s"
