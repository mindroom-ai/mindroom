"""Live progress text that a running tool call reports to whatever owns the call.

A dispatched call masks its tool's latest report and shows it in live reply frames; a background job stores it for
the agent; a sandbox runner forwards it to the primary. Reports reach tool code through the call's context, which
tool offloading copies into its threads; a thread a tool starts itself must capture ``report_tool_progress`` from
that context.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# Enough for the last screenful of output, small enough to show in every job summary.
_MAX_PROGRESS_CHARS = 2000
# A live reply shows this much of a running call's output.
_SHOWN_PROGRESS_CHARS = 1000
# A listener that shows a report masks credentials before cutting it, so a long credential keeps the label that
# identifies it: a report carries the shown output plus the longest private key (8192-bit, about 6.5K characters). A
# line redaction cannot delimit hides the shown output until it scrolls out of this window, so it stays small.
_REPORT_WINDOW_CHARS = 8000

_REPORTER: ContextVar[Callable[[str], None] | None] = ContextVar("tool_progress_reporter", default=None)


def cap_progress(text: str) -> str:
    """Keep only the latest characters of one report."""
    return text[-_MAX_PROGRESS_CHARS:]


def shown_progress(text: str) -> str:
    """Mask credentials in a report and keep the latest characters a live reply shows.

    Masking sees the whole report, so a long credential keeps the label that identifies it.
    """
    return redact_sensitive_text(text)[-_SHOWN_PROGRESS_CHARS:]


def cap_report(text: str) -> str:
    """Keep only the latest characters a report carries, enough for whoever shows it to mask a long credential."""
    return text[-_REPORT_WINDOW_CHARS:]


def report_tool_progress(text: str) -> None:
    """Report the call's latest progress, replacing the previous report; nothing happens when nobody listens.

    Listeners receive the report's latest characters unmasked; one that shows them masks credentials before it cuts
    them, as ``shown_progress`` does.
    """
    reporter = _REPORTER.get()
    if reporter is not None:
        reporter(cap_report(text))


@contextmanager
def tool_progress_reporter(reporter: Callable[[str], None], *, forward: bool = False) -> Iterator[None]:
    """Receive the progress of tool calls run in this context; the reporter may be called from any thread.

    With ``forward``, the reporter this one shadows receives every report too, as the call that starts a job does
    while it waits for the job.
    """
    shadowed = _REPORTER.get() if forward else None

    def report(text: str) -> None:
        reporter(text)
        if shadowed is not None:
            shadowed(text)

    token = _REPORTER.set(report)
    try:
        yield
    finally:
        _REPORTER.reset(token)
