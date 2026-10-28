"""Run contents older releases stored: a paused notice in place of the model's text, and a failure's text."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["legacy_failure_text", "stored_content"]

# Every wording of an agent run's paused notice Agno's runtime wrote; from v2026.10.499 only the confirmation one.
_AGENT_PAUSED_NOTICES = frozenset(
    {
        "I have tools to execute, but I need confirmation.",
        "I have tools to execute, but I need confirmation, user input, or external execution.",
        "I have tools to execute, but I need confirmation or user input.",
        "I have tools to execute, but I need confirmation or external execution.",
        "I have tools to execute, but I need user input or external execution.",
        "I have tools to execute, but I need user input.",
        "I have tools to execute, but it needs external execution.",
    },
)
# A team run's paused notice: the bare one, or this header and one line per waiting call.
_TEAM_PAUSED_NOTICE = "Team run paused."
_TEAM_PAUSED_NOTICE_HEADER = "Team run paused. The following require input:\n"


# LEGACY_COMPAT: Runs that store a paused notice as their content.
# Legacy format: a stored run dict whose ``content`` is the notice a run that paused without text of its own got:
# "I have tools to execute, but I need confirmation." for an agent run and "Team run paused. The following require
# input:" followed by one line per call waiting for confirmation for a team run, or another wording Agno's runtime
# wrote for both. A paused run held it, and so did the run's later row when its resume wrote no text.
# Last legacy release: v2026.10.627, whose ``paused_content`` (``agent_loop/run.py``) and ``team_paused_content``
# (``teams/runtime.py``) wrote it, as the Agno runtime did before them; replacement: the next release, whose paused
# runs store no content of their own and whose resumes start the content afresh.
# Handling: the content reads as none, so neither a resume nor a delegation's settlement hands the notice to a team
# leader or a delegating parent, and a row written back stores none.
# Coverage: tests/llm/test_legacy_run_content.py::test_paused_rows_main_wrote_read_without_their_notices,
#   tests/llm/test_legacy_run_content.py::test_a_completed_row_that_carried_the_notice_hands_over_none,
#   tests/llm/test_legacy_run_content.py::test_every_other_wording_agno_wrote_reads_as_no_content,
#   tests/llm/test_legacy_run_content.py::test_a_reply_that_only_opens_like_a_notice_keeps_its_content
def stored_content(data: Mapping[str, Any]) -> str | None:
    """Return the content a stored run dict holds, or None when it holds a paused notice instead of the model's text."""
    content = data.get("content")
    if not isinstance(content, str):
        return content
    if content in _AGENT_PAUSED_NOTICES or content == _TEAM_PAUSED_NOTICE:
        return None
    return None if content.startswith(_TEAM_PAUSED_NOTICE_HEADER) else content


# LEGACY_COMPAT: Failed runs that store their failure text as their content.
# Legacy format: an ``ERROR`` run dict without an ``error`` key, whose ``content`` is the unredacted text of the
# failure when its model wrote nothing, else the model's text.
# Last legacy release: v2026.10.627, whose ``_store_interrupted`` (``agent_loop/run.py``) and ``interrupt_child``
# (``delegation/recovery.py``) wrote it, as the Agno runtime did before them; replacement: the next release, which
# stores the redacted failure text under ``error`` and keeps ``content`` for the model's text.
# Handling: such a run hands over its content, credentials redacted, as its failure, which is what those releases
# handed over for it, so a delegation settled from such a row still reports why the run failed.
# Coverage: tests/llm/test_legacy_run_content.py::test_a_failed_row_main_wrote_hands_over_its_content_redacted
def legacy_failure_text(content: str | None) -> str | None:
    """Return what a failed run stored without an ``error`` hands over: its content, credentials redacted."""
    return None if content is None else redact_sensitive_text(content)
