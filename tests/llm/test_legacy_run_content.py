"""Rows v2026.10.627 stored read with no paused notice as their content, and a failed row hands over its failure.

``v2026.10.627_rows.json`` holds the run dicts that release's agent loop and team runtime stored, unchanged: an agent
run paused on a confirmation, the same run's row after a resume that wrote no text, a run whose provider failed before
its model wrote anything, and a team run paused on its member's confirmation, with the member's own row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord
from tests.llm.agno_dict_helpers import stored_run_record

_ROWS: dict[str, dict[str, Any]] = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "legacy_run_content" / "v2026.10.627_rows.json").read_text(),
)


@pytest.mark.parametrize("name", ["agent_paused", "member_paused", "team_paused"])
def test_paused_rows_main_wrote_read_without_their_notices(name: str) -> None:
    """A paused agent, member, or team row reads with no content, its pause kept in its requirements."""
    stored = _ROWS[name]
    assert str(stored["content"]).startswith(("I have tools to execute, but ", "Team run paused."))

    run = stored_run_record(stored)

    assert run.status is RunStatus.paused
    assert run.content is None
    assert run.requirements
    assert "content" not in run.to_dict()
    if isinstance(run, TeamRunRecord):
        [member] = run.member_responses
        assert (member.status, member.content) == (RunStatus.paused, None)


def test_a_completed_row_that_carried_the_notice_hands_over_none() -> None:
    """A row whose resume wrote no text, and so kept the notice, hands its reader nothing."""
    stored = _ROWS["agent_resumed_without_text"]
    assert stored["content"] == "I have tools to execute, but I need confirmation."

    run = RunRecord.from_dict(stored)

    assert run.status is RunStatus.completed
    assert run.handover_content is None


def test_a_failed_row_main_wrote_hands_over_its_content_redacted() -> None:
    """A failed row stored without an error hands over its content, credentials redacted, and writes back unchanged."""
    stored = _ROWS["agent_failed"]
    assert (stored["content"], "error" in stored) == ("provider down: api_key=unit-test-secret", False)

    run = RunRecord.from_dict(stored)

    assert run.handover_content == "provider down: api_key=***redacted***"
    assert run.to_dict() == stored
