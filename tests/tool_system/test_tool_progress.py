"""Progress reporters that tool calls report to."""

from __future__ import annotations

from mindroom.redaction import REDACTION_FAILED
from mindroom.tool_system.progress import report_tool_progress, shown_progress, tool_progress_reporter


def test_a_nested_progress_reporter_keeps_its_reports_from_the_one_it_shadows() -> None:
    """A call's progress stays with the call; the reporter it shadows gets its own reports back afterwards."""
    outer: list[str] = []
    inner: list[str] = []

    with tool_progress_reporter(outer.append):
        with tool_progress_reporter(inner.append):
            report_tool_progress("inner only")
        report_tool_progress("outer only")

    assert inner == ["inner only"]
    assert outer == ["outer only"]


def test_output_redaction_cannot_delimit_shows_only_the_failure() -> None:
    """A secret-named key whose value may be on the next line hides the whole output, as the call's result preview does."""
    manifest = "env:\n  - name: DB_PASSWORD\n    valueFrom:\n      secretKeyRef:\n        name: db\n"
    rollout = "\n".join(f"rollout: {index}/100 pods ready" for index in range(1, 101))

    assert shown_progress(manifest + rollout) == REDACTION_FAILED


def test_shown_output_is_what_a_live_reply_shows() -> None:
    """A call's shown output is the latest thousand characters, which a live reply's frame carries whole."""
    assert shown_progress("w" * 3000 + "x" * 1000) == "x" * 1000
