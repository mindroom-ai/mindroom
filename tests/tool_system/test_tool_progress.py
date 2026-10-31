"""Progress reporters that tool calls report to."""

from __future__ import annotations

from mindroom.redaction import REDACTION_FAILED
from mindroom.tool_system.progress import report_tool_progress, tool_progress_reporter


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


def test_a_report_is_redacted_before_it_is_cut_to_its_latest_characters() -> None:
    """A credential longer than a report keeps its mask when the cut drops the label that identifies it."""
    secret = "".join(f"{index:04x}" for index in range(800))
    seen: list[str] = []

    with tool_progress_reporter(seen.append):
        report_tool_progress(f"export API_KEY={secret}\nready")

    [report] = seen
    assert secret[-40:] not in report
    assert report.endswith("\nready")


def test_a_report_longer_than_redaction_accepts_still_shows_its_latest_characters() -> None:
    """A huge report, such as long compile lines and template errors together, shows its tail rather than a failure."""
    seen: list[str] = []

    with tool_progress_reporter(seen.append):
        report_tool_progress("g++ -c unit.cpp " * 6000 + "\nerror: no matching call")

    [report] = seen
    assert report != REDACTION_FAILED
    assert report.endswith("\nerror: no matching call")
