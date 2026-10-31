"""Progress reporters that tool calls report to."""

from __future__ import annotations

from mindroom.tool_system.progress import report_tool_progress, tool_progress_reporter


def test_a_nested_progress_reporter_also_reports_to_the_one_it_shadows() -> None:
    """A job inside a waited call reports to the job and to the call, and the call keeps its own afterwards."""
    outer: list[str] = []
    inner: list[str] = []

    with tool_progress_reporter(outer.append):
        with tool_progress_reporter(inner.append):
            report_tool_progress("both")
        report_tool_progress("outer only")

    assert inner == ["both"]
    assert outer == ["both", "outer only"]
