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


def test_output_hides_what_follows_a_secret_name_that_ends_its_line() -> None:
    """A secret-named key whose value may be on a later line hides the rest of the output; what came before still shows."""
    manifest = "env:\n  - name: DB_PASSWORD\n    valueFrom:\n      secretKeyRef:\n        name: db-credentials\n"
    rollout = "\n".join(f"rollout: {index}/100 pods ready" for index in range(1, 101))

    shown = shown_progress(manifest + rollout)

    assert "db-credentials" not in shown
    assert shown.startswith("env:\n  - name: DB_PASSWORD")
    assert shown != REDACTION_FAILED
