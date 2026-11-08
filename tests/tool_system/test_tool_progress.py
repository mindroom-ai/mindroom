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


def test_output_hides_a_value_on_the_line_after_its_secret_name() -> None:
    """A secret-named key whose value may be on the next line hides that line, and the rest of the output still shows."""
    manifest = "env:\n  - name: DB_PASSWORD\n    valueFrom:\n      secretKeyRef:\n        name: db-credentials\n"
    rollout = "rollout: 1/1 pods ready"

    shown = shown_progress(manifest + rollout)

    assert "db-credentials" not in shown
    assert shown.endswith(rollout)
    assert shown != REDACTION_FAILED


def test_shown_output_is_what_a_live_reply_shows() -> None:
    """A call's shown output is the latest thousand characters, which a live reply's frame carries whole."""
    assert shown_progress("w" * 3000 + "x" * 1000) == "x" * 1000
