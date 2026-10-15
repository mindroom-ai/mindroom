"""Live progress text that a running tool call reports to whatever owns the call.

A background job stores the latest report for the agent; a sandbox runner forwards it to the primary. Reports
reach tool code through the call's context, which tool offloading copies into its threads; a thread a tool
starts itself must capture ``report_tool_progress`` from that context.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Enough for the last screenful of output, small enough to show in every job summary.
_MAX_PROGRESS_CHARS = 2000

_REPORTER: ContextVar[Callable[[str], None] | None] = ContextVar("tool_progress_reporter", default=None)


def cap_progress(text: str) -> str:
    """Keep only the latest characters of one report."""
    return text[-_MAX_PROGRESS_CHARS:]


def report_tool_progress(text: str) -> None:
    """Report the call's latest progress, replacing the previous report; nothing happens when nobody listens."""
    reporter = _REPORTER.get()
    if reporter is not None:
        reporter(cap_progress(text))


@contextmanager
def tool_progress_reporter(reporter: Callable[[str], None]) -> Iterator[None]:
    """Receive the progress of tool calls run in this context; the reporter may be called from any thread."""
    token = _REPORTER.set(reporter)
    try:
        yield
    finally:
        _REPORTER.reset(token)
