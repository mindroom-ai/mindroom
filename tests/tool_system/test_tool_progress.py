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


_MANIFEST = (
    "env:\n  - name: DB_PASSWORD\n    valueFrom:\n      secretKeyRef:\n        name: db\n        key: password\n"
)
_KEY_BODY = "MIIE" + "A" * 60
_KEY = "-----BEGIN PRIVATE KEY-----\n" + "\n".join([_KEY_BODY] * 50) + "\n-----END PRIVATE KEY-----"


def test_a_line_redaction_cannot_delimit_hides_the_output_only_while_it_is_shown() -> None:
    """A manifest with `secretKeyRef:` far above the shown output does not hide the rollout below it."""
    rollout = "\n".join(f"rollout: {index}/100 pods ready" for index in range(1, 101))

    assert shown_progress(_MANIFEST + rollout).endswith("rollout: 100/100 pods ready")
    assert shown_progress(_MANIFEST + "rollout: 1/4 pods ready") == REDACTION_FAILED


def test_a_key_running_into_the_shown_output_stays_masked_when_redaction_fails_above_it() -> None:
    """Masking only the shown output still starts at a private key's first line, so its body never shows."""
    shown = shown_progress(_MANIFEST + _KEY + "\nwaiting")

    assert _KEY_BODY not in shown
    assert shown.endswith("waiting")


def test_a_value_below_a_key_above_the_shown_output_stays_hidden() -> None:
    """A secret on the shown output's first line stays hidden when its key is on the line above."""
    text = _MANIFEST + "x" * 3000 + "\npassword:\n" + "hunter2hunter2\n" + "y" * 1985

    assert "hunter2" not in shown_progress(text)


def test_a_value_above_the_shown_output_stays_hidden_when_masking_shortens_it() -> None:
    """The line above the shown output only helps masking; a shorter masked text never brings it into view."""
    shown = "DB_URL=postgres://app:" + "p" * 200 + "@db/app\n" + "z" * 1770
    text = "password:\n  value: s3cretvalue\n" + shown

    assert "s3cretvalue" not in shown_progress(text)
