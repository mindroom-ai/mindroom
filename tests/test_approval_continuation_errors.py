"""An approval continuation retains the provider failure that follows its approved tool without replaying the tool.

A failed resume raises its provider error after the record lands; the team counterpart is
``tests/test_team_turn_runtime.py::test_a_failed_resume_stores_the_error_over_the_paused_row_and_ran_the_approved_tool_once``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agent_loop.run import resume_agent, run_agent
from mindroom.approval.execution import _collect_agent_continuation
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.history.request_messages import build_resume_request
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.provider import TextDelta
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.tool_system.events import CollectedStreamPresentation
from tests.agent_loop.helpers import collect, context, gated, spec
from tests.fakes.scripted_model import Await, ScriptedModel, calls, tool_call

pytestmark = pytest.mark.asyncio

if TYPE_CHECKING:
    from pathlib import Path


async def test_a_failed_resume_keeps_its_error_and_runs_the_approved_tool_once(tmp_path: Path) -> None:
    """The approved call runs once; the provider failure that follows is the run's error and reaches the caller."""
    del tmp_path
    executed: list[list[str]] = []
    stored: list[RunRecord] = []

    def run_shell_command(args: list[str]) -> str:
        """Run one command.

        Args:
            args: The command and its arguments.

        """
        executed.append(args)
        return "approved effect completed"

    async def persist(record: RunRecord) -> None:
        stored.append(record)

    async def lose_connection() -> None:
        message = "provider connection lost"
        raise ProviderError(message, provider="Synthetic")

    model = ScriptedModel(
        [
            calls(tool_call("run_shell_command", "call-1", args=["echo", "hi"])),
            [TextDelta("Partial continuation."), Await(lose_connection)],
        ],
    )
    agent = spec(model, gated(run_shell_command))
    _events, paused = await collect(
        run_agent(agent, [Message(role="user", content="Execute the approved tool")], ctx=context(), persist=persist),
    )
    assert paused.status is RunStatus.paused
    assert executed == []
    approved = ApprovalCall(
        tool_call_id="call-1",
        tool_name="run_shell_command",
        invoking_agent="code",
        expires_at_ns=2**62,
        toolkit_name="gated",
        arguments_digest=approval_arguments_digest({"args": ["echo", "hi"]}),
    )
    presentation = CollectedStreamPresentation(show_tool_calls=True)

    with pytest.raises(ProviderError, match="provider connection lost"):
        await _collect_agent_continuation(
            resume_agent(
                agent,
                paused,
                build_resume_request(paused=paused.messages, summary=None, history=[]),
                approved=[approved],
                ctx=context(),
                persist=persist,
            ),
            presentation,
            run_id="run",
            progress=None,
        )

    assert executed == [["echo", "hi"]]
    assert [record.status for record in stored] == [RunStatus.paused, RunStatus.error]
    assert stored[-1].run_id == paused.run_id
    assert [tool.result for tool in stored[-1].tools or ()] == ["approved effect completed"]
    assert presentation.final_text().endswith("Partial continuation.")
